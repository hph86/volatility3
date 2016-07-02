if __name__ == "__main__":
    import os
    import sys

    sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

import struct

from volatility.framework import interfaces, layers, validity, configuration
from volatility.framework.configuration import depresolver

PAGE_SIZE = 0x1000


def scan(ctx, layer_name, tests):
    """Scans through layer_name at context and returns the best-guess layer type and a single best-guess DTB

       It should be noted that this is automagical and therefore not the guaranteed correct response
       The UI should always provide the user an opportunity to specify the appropriate types and DTB values themselves
    """
    hits = {}
    for offset in range(ctx.memory[layer_name].minimum_address,
                        ctx.memory[layer_name].maximum_address - PAGE_SIZE,
                        PAGE_SIZE):
        for test in tests:
            val = test.run(offset, ctx, layer_name)
            if val:
                hits[test.layer_type] = sorted(hits.get(test.layer_type, []) + [val])
    # Don't reduce the tuple until after all the sorting's complete
    for test in tests:
        hits[test.layer_type] = [x for _, x in hits.get(test.layer_type, [])]
    return hits


class DtbTest(validity.ValidityRoutines):
    super_bit = 2

    def __init__(self, layer_type = None, ptr_size = None, ptr_struct = None, ptr_reference = None, mask = None):
        self.layer_type = self._check_class(layer_type, interfaces.layers.TranslationLayerInterface)
        self.ptr_size = self._check_type(ptr_size, int)
        self.ptr_struct = self._check_type(ptr_struct, str)
        self.ptr_reference = self._check_type(ptr_reference, int)
        self.mask = self._check_type(mask, int)

    def unpack(self, value):
        return struct.unpack("<" + self.ptr_struct, value)[0]

    def run(self, page_offset, ctx, layer_name):
        value = ctx.memory.read(layer_name, page_offset + (self.ptr_reference * self.ptr_size),
                                self.ptr_size)
        ptr = self.unpack(value)
        # The value *must* be present (bit 0) since it's a mapped page
        # It's almost always writable (bit 1)
        # It's occasionally Super, but not reliably so, haven't checked when/why not
        # The top 3-bits are usually ignore (which in practice means 0
        # Need to find out why the middle 3-bits are usually 6 (0110)
        if ptr != 0 and (ptr & self.mask == page_offset) & (ptr & 0xFF1 == 0x61):
            dtb = (ptr & self.mask)
            return self.second_pass(dtb, ctx, layer_name)

    def second_pass(self, dtb, ctx, layer_name):
        data = ctx.memory.read(layer_name, dtb, PAGE_SIZE)
        usr_count, sup_count = 0, 0
        for i in range(0, PAGE_SIZE, self.ptr_size):
            val = self.unpack(data[i:i + self.ptr_size])
            if val & 0x1:
                sup_count += 0 if (val & 0x4) else 1
                usr_count += 1 if (val & 0x4) else 0
        # print(hex(dtb), usr_count, sup_count, usr_count + sup_count)
        # We sometimes find bogus DTBs at 0x16000 with a very low sup_count and 0 usr_count
        if usr_count or sup_count > 5:
            return (usr_count, -sup_count), dtb


class DtbTest32bit(DtbTest):
    def __init__(self):
        DtbTest.__init__(self,
                         layer_type = layers.intel.Intel,
                         ptr_size = 4,
                         ptr_struct = "I",
                         ptr_reference = 0x300,
                         mask = 0xFFFFF000)


class DtbTest64bit(DtbTest):
    def __init__(self):
        DtbTest.__init__(self,
                         layer_type = layers.intel.Intel32e,
                         ptr_size = 8,
                         ptr_struct = "Q",
                         ptr_reference = 0x1ED,
                         mask = 0x3FFFFFFFFFF000)


class DtbTestPae(DtbTest):
    def __init__(self):
        DtbTest.__init__(self,
                         layer_type = layers.intel.IntelPAE,
                         ptr_size = 8,
                         ptr_struct = "Q",
                         ptr_reference = 0x3,
                         mask = 0x3FFFFFFFFFF000)

    def second_pass(self, dtb, ctx, layer_name):
        dtb -= 0x4000
        data = ctx.memory.read(layer_name, dtb, PAGE_SIZE)
        val = self.unpack(data[3 * self.ptr_size: 4 * self.ptr_size])
        if (val & self.mask == dtb + 0x4000) and (val & 0xFFF == 0x001):
            return val, dtb


class SelfReferentialTest(object):
    def __init__(self):
        self.ptr_struct = "Q"
        self.ptr_size = 8
        self.layer_type = layers.intel.Intel32e
        self.mask = 0x3FFFFFFFFFF000

    def run(self, page_offset, ctx, layer_name):
        data = ctx.memory.read(layer_name, page_offset, PAGE_SIZE)
        response = None
        for i in range(0, PAGE_SIZE, self.ptr_size):
            value = struct.unpack("<" + self.ptr_struct, data[i:i + self.ptr_size])[0] & self.mask
            if value == page_offset and value != 0:
                response = (i // self.ptr_size, page_offset)
                print(hex(response[1]), hex(response[0]))
        return response


class PageMapOffsetHelper(interfaces.configuration.HierachicalVisitor):
    def __init__(self, context):
        self.ctx = self._check_type(context, interfaces.context.ContextInterface)
        self.tests = dict([(test.layer_type, test) for test in [DtbTest32bit(), DtbTest64bit(), DtbTestPae()]])

    def branch_leave(self, node, config_path):
        """Ensure we're called on internal nodes as well as external"""
        self(node, config_path)
        return True

    def __call__(self, node, config_path):
        if isinstance(node, depresolver.RequirementTreeChoice):
            useful = []
            for candidate in node.candidates:
                if candidate in self.tests:
                    useful.append(self.tests[candidate])
            if useful:
                depresolver.DependencyResolver().validate_dependencies(node.candidates[useful[0].layer_type], self.ctx,
                                                                       config_path)
                prefix = config_path + configuration.CONFIG_SEPARATOR
                memory_layer = self.ctx.config.get(prefix + "memory_layer", None)
                page_table_offset = self.ctx.config.get(prefix + "page_map_offset", None)
                if page_table_offset is None and memory_layer is not None:
                    hits = scan(self.ctx, memory_layer, useful)
                    for test in useful:
                        if hits.get(test.layer_type, []):
                            self.ctx.config[prefix + "page_map_offset"] = hits[test.layer_type][0]
                        else:
                            # Delete the node rather than fixing the constraints,
                            # since the requirements haven't changed, but some of the candidates are no longer valid
                            # If the constraints were global across the tree, then tagging the constraints may be more useful
                            del node.candidates[test.layer_type]
        return True


if __name__ == '__main__':
    import argparse

    from volatility.framework.symbols import native
    from volatility.framework import contexts

    parser = argparse.ArgumentParser()
    parser.add_argument("filenames", metavar = "FILE", nargs = "+", action = "store", help = "FILE to read for testing")
    parser.add_argument("--32bit", action = "store_false", dest = "bit32", help = "Disable 32-bit run")
    parser.add_argument("--64bit", action = "store_false", dest = "bit64", help = "Disable 64-bit run")
    parser.add_argument("--pae", action = "store_false", help = "Disable pae run")
    parser.add_argument("--generic", action = "store_true", help = "Enable generic scan")

    args = parser.parse_args()

    nativelst = native.x86NativeTable
    ctx = contexts.Context(nativelst)
    for filename in args.filenames:
        data = layers.physical.FileLayer(ctx,
                                         'config' + str(args.filenames.index(filename)),
                                         'data' + str(args.filenames.index(filename)),
                                         filename = filename)
        ctx.memory.add_layer(data)

    tests = []
    if args.bit32:
        tests.append(DtbTest32bit())
    if args.bit64:
        tests.append(DtbTest64bit())
    if args.pae:
        tests.append(DtbTestPae())
    if args.generic:
        tests.append(SelfReferentialTest())

    if tests:
        for i in range(len(args.filenames)):
            print("[*] Scanning " + args.filenames[i] + "...")
            hits = scan(ctx, "data" + str(i), tests)
            for key in tests:
                arch_hits = hits.get(key.layer_type, [])
                if arch_hits:
                    print("   ", key.layer_type.__name__ + ": " + repr([hex(x) for x in sorted(arch_hits)]))
            guesses = []
            for key in hits:
                guesses.append((len(hits[key]), key.__name__, hits[key]))
            num, arch, dtbs = max(guesses)
            if num:
                print("[!] OS Guess:", arch, "with DTB", hex(dtbs[0]))
            else:
                print("[X] No DTBs found")
            print()
    else:
        print("[X] No tests selected")