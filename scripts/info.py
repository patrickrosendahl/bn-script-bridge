"""Read-only queries (bnrun info -- ...): functions and Binary Ninja settings.

  F:ADDR       the function starting at (or else containing) ADDR: name, range, analysis
               skipped and why
  S:SUBSTR     settings whose id contains SUBSTR (case-insensitive), with their values
ADDR is hex, with or without 0x. S: needs no view; F: needs one (--bndb or --view).
"""
import json
import sys

import binaryninja as bn


def function_info(addr_s):
    addr = int(addr_s, 16)
    funcs = [bv.get_function_at(addr)] if bv.get_function_at(addr) else \
        list(bv.get_functions_containing(addr) or [])
    if not funcs:
        return ["no function at %#x" % addr]
    return ["%s @ %#x..%#x skipped=%s reason=%s" % (
        f.name, f.start, f.highest_address, f.analysis_skipped,
        getattr(f.analysis_skip_reason, "name", f.analysis_skip_reason)) for f in funcs]


def settings_info(substr):
    settings = bn.Settings()
    lines = []
    for key in sorted(settings.keys()):
        if substr.lower() in key.lower():
            try:
                value = json.loads(settings.get_json(key))
            except Exception as e:  # noqa: BLE001 -- show what failed, keep listing
                value = "<%s>" % e
            lines.append("%s = %s" % (key, json.dumps(value)))
    return lines or ["no setting matches %r" % substr]


if not argv[1:]:
    sys.exit("info: no queries (F:ADDR, S:SUBSTR)")
failed = 0
for item in argv[1:]:
    kind, sep, rest = item.partition(":")
    try:
        if kind == "F" and sep:
            lines = function_info(rest)
        elif kind == "S" and sep:
            lines = settings_info(rest)
        else:
            raise ValueError("expected F:ADDR or S:SUBSTR")
    except ValueError as e:
        failed += 1
        lines = ["ERROR %s" % e]
    for line in lines:
        print("%s %s" % (item[:60], line))
if failed:
    sys.exit("info: %d bad quer%s" % (failed, "y" if failed == 1 else "ies"))
