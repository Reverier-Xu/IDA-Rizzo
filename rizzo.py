##########################################################################
# An IDAPython plugin that generates "fuzzy" function signatures that can
# be shared and applied amongst different IDBs.
#
# There are multiple sets of signatures that are generated:
#
#   o "Formal" signatures, where functions must match exactly
#   o "Fuzzy" signatures, where functions must only resemble each other
#     in terms of data/call references.
#   o String-based signatures, where functions are identified based on
#     unique string references.
#   o Immediate-based signatures, where functions are identified based
#     on immediate value references.
#
# These signatures are applied based on accuracy, that is, formal
# signatures are applied first, then string and immediate based
# signatures, and finally fuzzy signatures.
#
# Further, functions are identified based on call references. Consider,
# for example, two functions, one named 'foo', the other named 'bar'.
# The 'foo' function is fairly unique and a reliable signature is easily
# generated for it, but the 'bar' function is more difficult to reliably
# identify. However, 'foo' calls 'bar', and thus once 'foo' is identified,
# 'bar' can also be identified by association.
#
# This version of Rizzo is ported to IDA 7.4+.
#
#
# @ Craig Heffner
# @ devttys0
# @ Reverier
##########################################################################

import collections
import pickle  # http://natashenka.ca/pickle/
import time

import idaapi
import idautils
import idc
import ida_kernwin
import ida_name


class RizzoSignatures(object):
    """
    Simple wrapper class for storing signature info.
    """

    SHOW = []

    def __init__(self):
        self.fuzzy = {}
        self.formal = {}
        self.strings = {}
        self.functions = {}
        self.immediates = {}

        self.fuzzydups = set()
        self.formaldups = set()
        self.stringdups = set()
        self.immediatedups = set()

    def show(self):
        if not self.SHOW:
            return

        print('\n\nGENERATED FORMAL SIGNATURES FOR:')
        for (key, ea) in self.formal.items():
            func = RizzoFunctionDescriptor(self.formal, self.functions, key)
            if func.name in self.SHOW:
                print(func.name)

        print('\n\nGENERATED FUZZY SIGNATURES FOR:')
        for (key, ea) in self.fuzzy.items():
            func = RizzoFunctionDescriptor(self.fuzzy, self.functions, key)
            if func.name in self.SHOW:
                print(func.name)


class RizzoStringDescriptor(object):
    """
    Wrapper class for easily accessing necessary string information.
    """

    def __init__(self, string):
        self.ea = string.ea
        self.value = str(string)
        self.xrefs = [x.frm for x in idautils.XrefsTo(self.ea)]


class RizzoBlockDescriptor(object):
    """
    Code block info is stored in tuples, which minimize pickle storage space.
    This class provides more Pythonic (and sane) access to values of interest for a given block.
    """

    def __init__(self, block):
        self.formal = block[0]
        self.fuzzy = block[1]
        self.immediates = block[2]
        self.functions = block[3]

    def match(self, nblock, fuzzy=False):
        # TODO: Fuzzy matching at the block level gets close, but produces a higher number of
        #       false positives; for example, it confuses hmac_md5 with hmac_sha1.
        # return ((self.formal == nblock.formal or (fuzzy and self.fuzzy == nblock.fuzzy)) and
        return self.formal == nblock.formal and len(self.immediates) == len(nblock.immediates) and len(self.functions) == len(nblock.functions)


class RizzoFunctionDescriptor(object):
    """
    Function signature info is stored in dicts and tuples, which minimize pickle storage space.
    This class provides more Pythonic (and sane) access to values of interest for a given function.
    """

    def __init__(self, signatures, functions, key):
        self.ea = signatures[key]
        self.name = functions[self.ea][0]
        self.blocks = functions[self.ea][1]


class Rizzo(object):
    """
    Workhorse class which performs the primary logic and functionality.
    """

    DEFAULT_SIGNATURE_FILE = 'rizzo.sig'

    def __init__(self, sigfile=None):
        if sigfile:
            self.sigfile = sigfile
        else:
            self.sigfile = self.DEFAULT_SIGNATURE_FILE

        # Useful for quickly identifying string xrefs from individual instructions
        self.strings = {}
        for string in idautils.Strings():
            self.strings[string.ea] = RizzoStringDescriptor(string)

        start = time.time()
        self.signatures = self.generate()
        end = time.time()

        print('[Rizzo] Generated %d formal signatures and %d fuzzy signatures for %d functions in %.2f seconds.' % (len(self.signatures.formal), len(self.signatures.fuzzy), len(self.signatures.functions), (end - start)))

    def save(self):
        print('[Rizzo] Saving signatures to %s...' % self.sigfile)
        fp = open(self.sigfile, 'wb')
        pickle.dump(self.signatures, fp)
        fp.close()
        print('[Rizzo] saved.')

    def load(self):
        print('[Rizzo] Loading signatures from %s...' % self.sigfile)
        fp = open(self.sigfile, 'rb')
        sigs = pickle.load(fp)
        fp.close()
        print('[Rizzo] loaded.')
        return sigs

    @staticmethod
    def sighash(value):
        return hash(str(value)) & 0xFFFFFFFF

    def block(self, block):
        """
        Returns a tuple: ([formal, block, signatures], [fuzzy, block, signatures], set([unique, immediate, values]), [called, function, names])
        """
        formal = []
        fuzzy = []
        functions = []
        immediates = []

        ea = block.start_ea
        insn = idaapi.insn_t()
        while ea < block.end_ea:
            idaapi.decode_insn(insn, ea)

            # Get a list of all data/code references from the current instruction
            drefs = [x for x in idautils.DataRefsFrom(ea)]
            crefs = [x for x in idautils.CodeRefsFrom(ea, False)]

            # Add all instruction mnemonics to the formal block hash
            formal.append(idc.print_insn_mnem(ea))

            # If this is a call instruction, be sure to note the name of the function
            # being called. This is used to apply call-based signatures to functions.
            #
            # For fuzzy signatures, we can't use the actual name or EA of the function,
            # but rather just want to note that a function call was made.
            #
            # Formal signatures already have the call instruction mnemonic, which is more
            # specific than just saying that a call was made.
            if idaapi.is_call_insn(ea):
                for cref in crefs:
                    func_name = idc.get_name(cref, ida_name.GN_VISIBLE)
                    if not func_name:
                        continue
                    functions.append(func_name)
                    fuzzy.append('funcref')
            # If there are data references from the instruction, check to see if any of them
            # are strings. These are looked up in the pre-generated strings dictionary.
            #
            # String values are easily identifiable, and are used as part of both the fuzzy
            # and the formal signatures.
            #
            # It is more difficult to determine if non-string values are constants or not;
            # for both fuzzy and formal signatures, just use "data" to indicate that some data
            # was referenced.
            elif drefs:
                for dref in drefs:
                    if dref in self.strings:
                        formal.append(self.strings[dref].value)
                        fuzzy.append(self.strings[dref].value)
                    else:
                        formal.append('dataref')
                        fuzzy.append('dataref')
            # If there are no data or code references from the instruction, use every operand as
            # part of the formal signature.
            #
            # Fuzzy signatures are only concerned with interesting immediate values, that is, values
            # that are greater than 65,535, are not memory addresses, and are not displayed as
            # negative values.
            elif not drefs and not crefs:
                for n in range(0, len(idaapi.insn_t().ops)):
                    opnd_text = idc.print_operand(ea, n)
                    formal.append(opnd_text)

                    if idaapi.insn_t().ops[n].type != idaapi.o_imm or opnd_text.startswith('-'):
                        continue

                    if idaapi.insn_t().ops[n].value < 0xFFFF:
                        continue

                    if idaapi.get_full_flags(idaapi.insn_t().ops[n].value) != 0:
                        continue

                    fuzzy.append(str(idaapi.insn_t().ops[n].value))
                    immediates.append(idaapi.insn_t().ops[n].value)

            ea = idc.next_head(ea)

        return self.sighash(''.join(formal)), self.sighash(''.join(fuzzy)), immediates, functions

    def function(self, func):
        """
        Returns a list of blocks.
        """
        blocks = []

        for block in idaapi.FlowChart(func):
            blocks.append(self.block(block))

        return blocks

    def generate(self):
        signatures = RizzoSignatures()

        # Generate unique string-based function signatures
        for (ea, string) in self.strings.items():
            # Only generate signatures on reasonably long strings with one xref
            if len(string.value) < 8 or len(string.xrefs) != 1:
                continue

            func = idaapi.get_func(string.xrefs[0])
            if not func:
                continue

            strhash = self.sighash(string.value)

            # Check for and remove string duplicate signatures (the same
            # string can appear more than once in an IDB).
            # If no duplicates, add this to the string signature dict.
            if strhash in signatures.strings:
                del signatures.strings[strhash]
                signatures.stringdups.add(strhash)
            elif strhash not in signatures.stringdups:
                signatures.strings[strhash] = func.start_ea

        # Generate formal, fuzzy, and immediate-based function signatures
        for ea in idautils.Functions():
            func = idaapi.get_func(ea)
            if not func:
                continue

            # Generate a signature for each block in this function
            blocks = self.function(func)

            # Build function-wide formal and fuzzy signatures by simply
            # concatenating the individual function block signatures.
            formal = self.sighash(''.join([str(e) for (e, f, i, c) in blocks]))
            fuzzy = self.sighash(''.join([str(f) for (e, f, i, c) in blocks]))

            # Add this signature to the function dictionary.
            signatures.functions[func.start_ea] = (idc.get_name(func.start_ea, ida_name.GN_VISIBLE), blocks)

            # Check for and remove formal duplicate signatures.
            # If no duplicates, add this to the formal signature dict.
            if formal in signatures.formal:
                del signatures.formal[formal]
                signatures.formaldups.add(formal)
            elif formal not in signatures.formaldups:
                signatures.formal[formal] = func.start_ea

            # Check for and remove fuzzy duplicate signatures.
            # If no duplicates, add this to the fuzzy signature dict.
            if fuzzy in signatures.fuzzy:
                del signatures.fuzzy[fuzzy]
                signatures.fuzzydups.add(fuzzy)
            elif fuzzy not in signatures.fuzzydups:
                signatures.fuzzy[fuzzy] = func.start_ea

            # Check for and remove immediate duplicate signatures.
            # If no duplicates, add this to the immediate signature dict.
            for (e, f, immediates, c) in blocks:
                for immediate in immediates:
                    if immediate in signatures.immediates:
                        del signatures.immediates[immediate]
                        signatures.immediatedups.add(immediate)
                    elif immediate not in signatures.immediatedups:
                        signatures.immediates[immediate] = func.start_ea

        # These need not be maintained across function calls,
        # and only add to the size of the saved signature file.
        signatures.fuzzydups = set()
        signatures.formaldups = set()
        signatures.stringdups = set()
        signatures.immediatedups = set()

        # DEBUG
        signatures.show()

        return signatures

    def match(self, extsigs):
        fuzzy = {}
        formal = {}
        strings = {}
        immediates = {}

        # Match formal function signatures
        start = time.time()
        for (extsig, ext_func_ea) in extsigs.formal.items():
            if extsig not in self.signatures.formal:
                continue
            newfunc = RizzoFunctionDescriptor(extsigs.formal, extsigs.functions, extsig)
            curfunc = RizzoFunctionDescriptor(self.signatures.formal, self.signatures.functions, extsig)
            formal[curfunc] = newfunc
        end = time.time()
        print('[Rizzo] Found %d formal matches in %.2f seconds.' % (len(formal), (end - start)))

        # Match fuzzy function signatures
        start = time.time()
        for (extsig, ext_func_ea) in extsigs.fuzzy.items():
            if extsig not in self.signatures.fuzzy:
                continue
            curfunc = RizzoFunctionDescriptor(self.signatures.fuzzy, self.signatures.functions, extsig)
            newfunc = RizzoFunctionDescriptor(extsigs.fuzzy, extsigs.functions, extsig)
            # Only accept this as a valid match if the functions have the same number of basic code blocks
            if len(curfunc.blocks) == len(newfunc.blocks):
                fuzzy[curfunc] = newfunc
        end = time.time()
        print('[Rizzo] Found %d fuzzy matches in %.2f seconds.' % (len(fuzzy), (end - start)))

        # Match string based function signatures
        start = time.time()
        for (extsig, ext_func_ea) in extsigs.strings.items():
            if extsig not in self.signatures.strings:
                continue
            curfunc = RizzoFunctionDescriptor(self.signatures.strings, self.signatures.functions, extsig)
            newfunc = RizzoFunctionDescriptor(extsigs.strings, extsigs.functions, extsig)
            strings[curfunc] = newfunc
        end = time.time()
        print('[Rizzo] Found %d string matches in %.2f seconds.' % (len(strings), (end - start)))

        # Match immediate baesd function signatures
        start = time.time()
        for (extsig, ext_func_ea) in extsigs.immediates.items():
            if extsig not in self.signatures.immediates:
                continue
            curfunc = RizzoFunctionDescriptor(self.signatures.immediates, self.signatures.functions, extsig)
            newfunc = RizzoFunctionDescriptor(extsigs.immediates, extsigs.functions, extsig)
            immediates[curfunc] = newfunc
        end = time.time()
        print('[Rizzo] Found %d immediate matches in %.2f seconds.' % (len(immediates), (end - start)))

        # Return signature matches in the order we want them applied
        # The second tuple of each match is set to True if it is a fuzzy match, e.g.:
        #
        #   ((match, fuzzy), (match, fuzzy), ...)
        return (formal, False), (strings, False), (immediates, False), (fuzzy, True)

    def rename(self, ea, name):
        # Don't rely on the name in curfunc, as it could have already been renamed
        curname = idc.get_name(ea, ida_name.GN_VISIBLE)
        # Don't rename if the name is a special identifier, or if the ea has already been named
        # TODO: What's a better way to check for reserved name prefixes?
        if curname.startswith('sub_') and name.split('_')[0] not in {'sub', 'loc', 'unk', 'dword', 'word', 'byte'}:
            # Don't rename if the name already exists in the IDB
            if idc.get_name_ea_simple(name) == idc.BADADDR:
                if idc.set_name(ea, name, ida_name.SN_CHECK):
                    idc.set_func_attr(ea, idc.FUNCATTR_FLAGS, (idc.get_func_attr(ea, idc.FUNCATTR_FLAGS) | idc.FUNC_LIB))
                    # print "%s  =>  %s" % (curname, name)
                    return 1
        # else:
        #    print "WARNING: Attempted to rename '%s' => '%s', but '%s' already exists!" % (curname, name, name)
        return 0

    def apply(self, extsigs):
        count = 0

        start = time.time()

        # This applies formal matches first, then fuzzy matches
        for (match, fuzzy) in self.match(extsigs):
            # Keeps track of all function names that we've identified candidate functions for
            rename = {}

            for (curfunc, newfunc) in match.items():
                if newfunc.name not in rename:
                    rename[newfunc.name] = []

                # Attempt to rename this function
                rename[newfunc.name].append(curfunc.ea)

                bm = {}
                duplicates = set()

                # Search for unique matching code blocks inside this function
                for nblock in newfunc.blocks:
                    nblock = RizzoBlockDescriptor(nblock)
                    for cblock in curfunc.blocks:
                        cblock = RizzoBlockDescriptor(cblock)

                        if cblock.match(nblock, fuzzy):
                            if cblock in bm:
                                del bm[cblock]
                                duplicates.add(cblock)
                            elif cblock not in duplicates:
                                bm[cblock] = nblock

                # Rename known function calls from each unique identified code block
                for (cblock, nblock) in bm.items():
                    for n in range(0, len(cblock.functions)):
                        ea = idc.get_name_ea_simple(cblock.functions[n])
                        if ea != idc.BADADDR:
                            if nblock.functions[n] in rename:
                                rename[nblock.functions[n]].append(ea)
                            else:
                                rename[nblock.functions[n]] = [ea]

                # Rename the identified functions
                for (name, candidates) in rename.items():
                    if candidates:
                        winner = collections.Counter(candidates).most_common(1)[0][0]
                        count += self.rename(winner, name)

        end = time.time()
        print('[Rizzo] Renamed %d functions in %.2f seconds.' % (count, (end - start)))


def RizzoBuild(sigfile=None):
    print('[Rizzo] Building Rizzo signatures, this may take a few minutes...')
    start = time.time()
    r = Rizzo(sigfile)
    r.save()
    end = time.time()
    print('[Rizzo] Built signatures in %.2f seconds' % (end - start))


def RizzoApply(sigfile=None):
    print('[Rizzo] Applying Rizzo signatures, this may take a few minutes...')
    start = time.time()
    r = Rizzo(sigfile)
    s = r.load()
    r.apply(s)
    end = time.time()
    print('[Rizzo] Signatures applied in %.2f seconds' % (end - start))


class RizzoActionHandlerLoad(idaapi.action_handler_t):
    def __init__(self):
        idaapi.action_handler_t.__init__(self)

    def activate(self, ctx):
        fname = ida_kernwin.ask_file(0, '*.riz', 'Load signature file')
        if fname:
            RizzoApply(fname)
        return 0

    def update(self, ctx):
        return idaapi.AST_ENABLE_ALWAYS


class RizzoActionHandlerProduce(idaapi.action_handler_t):
    def __init__(self):
        idaapi.action_handler_t.__init__(self)

    def activate(self, ctx):
        fname = ida_kernwin.ask_file(1, '*.riz', 'Save signature file as')
        if fname:
            if '.' not in fname:
                fname += ".riz"
            RizzoBuild(fname)
        return 0

    def update(self, ctx):
        return idaapi.AST_ENABLE_ALWAYS


# noinspection PyMethodMayBeStatic
class RizzoPlugin(idaapi.plugin_t):
    flags = 0
    comment = 'Function signature'
    help = ''
    wanted_name = 'Rizzo'
    wanted_hotkey = ''

    NAME = 'rizzo.py'

    def __init__(self):
        print("\n================================================================================")
        print("[Rizzo] Rizzo plugin by @devttys0, @Craig Heffner, @Reverier-Xu for IDA 7.4+")
        print("[Rizzo] Loading Rizzo...")
        self.menu_context_load_action = idaapi.action_desc_t('rizzo:load', 'Rizzo signature file...', RizzoActionHandlerLoad())
        self.menu_context_produce_action = idaapi.action_desc_t('rizzo:produce', 'Rizzo signature file...', RizzoActionHandlerProduce())
        idaapi.register_action(self.menu_context_load_action)
        idaapi.register_action(self.menu_context_produce_action)
        print("[Rizzo] Rizzo is Ready!")

    def init(self):
        idaapi.attach_action_to_menu('File/Load file/Rizzo signature file...', 'rizzo:load', idaapi.SETMENU_APP)
        idaapi.attach_action_to_menu('File/Produce file/Rizzo signature file...', 'rizzo:produce', idaapi.SETMENU_APP)
        return idaapi.PLUGIN_KEEP

    def term(self):
        idaapi.detach_action_from_menu('File/Load file/Rizzo signature file...', 'rizzo:load')
        idaapi.detach_action_from_menu('File/Produce file/Rizzo signature file...', 'rizzo:produce')
        return None

    def run(self, arg):
        return None

# def rizzo_script(self):
# 	idaapi.IDAPython_ExecScript(self.script, globals())


def PLUGIN_ENTRY():
    return RizzoPlugin()
