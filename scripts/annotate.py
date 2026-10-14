"""Rename functions/data and set comments from the command line (bnrun annotate -- ...).

Directives, one per argument (quote a comment to keep its spaces):
  R:ADDR:NAME     rename the function starting at ADDR
  D:ADDR:NAME     define/rename the data symbol at ADDR
  C:ADDR:TEXT     set the comment at ADDR (replaces it; the old text is printed)
ADDR is hex, with or without 0x. All directives are checked first (syntax, function exists,
address valid); if any is bad nothing is applied. Edits live in RAM until saved: --save (first
argument, --bndb runs only) saves the .bndb in the same job once all edits are applied, so a
client that dies meanwhile can't leave them unsaved. Example:

  bnrun --bndb /abs/x.bndb annotate -- --save R:0x10014fed0:nhv_proc "C:0x10014fed0:called per voice"
"""
import sys

import binaryninja as bn


def parse(item):
    kind, sep, rest = item.partition(":")
    addr_s, sep2, text = rest.partition(":")
    if kind not in ("R", "D", "C") or not sep:
        raise ValueError("expected R:, D: or C:")
    if not sep2 or not text:
        raise ValueError("expected %s:ADDR:%s" % (kind, "TEXT" if kind == "C" else "NAME"))
    try:
        addr = int(addr_s, 16)
    except ValueError:
        raise ValueError("bad address %r" % addr_s)
    return kind, addr, text


def check(kind, addr, text):
    if kind == "R":
        if bv.get_function_at(addr) is None:
            raise ValueError("no function starts at %#x" % addr)
    elif not bv.is_valid_offset(addr):
        raise ValueError("%#x is not a valid address in this view" % addr)


def apply(kind, addr, text):
    if kind == "R":
        old = bv.get_function_at(addr).name
        bv.define_user_symbol(bn.Symbol(bn.SymbolType.FunctionSymbol, addr, text))
        return "%#x %s -> %s" % (addr, old, text)
    if kind == "D":
        old = bv.get_symbol_at(addr)
        bv.define_user_symbol(bn.Symbol(bn.SymbolType.DataSymbol, addr, text))
        return "%#x %s -> %s" % (addr, old.name if old else "(none)", text)
    # Inside a function the comment belongs to the function (it shows in its disassembly
    # and IL); elsewhere it is a view-level comment.
    funcs = bv.get_functions_containing(addr) or []
    old = [f.get_comment_at(addr) for f in funcs] if funcs else [bv.get_comment_at(addr)]
    if funcs:
        for f in funcs:
            f.set_comment_at(addr, text)
    else:
        bv.set_comment_at(addr, text)
    was = "; ".join(o for o in old if o)
    return "%#x comment set%s" % (addr, " (was: %s)" % was if was else "")


save = argv[1:2] == ["--save"]
items = argv[2:] if save else argv[1:]
if save and "save_bndb" not in globals():
    sys.exit("annotate: --save needs a --bndb run (save a UI view from Binary Ninja)")
if not items:
    sys.exit("annotate: no directives (see the docstring: R:ADDR:NAME, D:ADDR:NAME, "
             "C:ADDR:TEXT)")
directives, errors = [], []
for item in items:
    try:
        d = parse(item)
        check(*d)
        directives.append(d)
    except ValueError as e:
        errors.append("%s: %s" % (item[:60], e))
if errors:
    for line in errors:
        print("ERROR " + line)
    sys.exit("annotate: nothing applied (%d bad directive%s)"
             % (len(errors), "" if len(errors) == 1 else "s"))
for d in directives:
    print("%s %s" % (d[0], apply(*d)))
if save:
    try:
        print("saved " + save_bndb())
    except Exception as e:  # noqa: BLE001
        sys.exit("annotate: edits applied but NOT saved (%s); retry with bnrun --bndb-save" % e)
