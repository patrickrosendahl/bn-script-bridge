"""Print a function's HLIL, re-analysing it first if Binary Ninja skipped it
(bnrun decompile -- ADDR [OUT]).

Binary Ninja skips analysis of huge functions (analysis_skip_reason e.g. ExceedFunctionSize /
ExceedFunctionAnalysisTime); their HLIL is then a stub. This clears the skip, waits for
analysis and prints the HLIL, or writes it to OUT and prints one status line:
  bnrun --bndb /abs/x.bndb decompile -- 0x10014fed0 > fn.hlil      # HLIL on stdout
  bnrun --bndb /abs/x.bndb decompile -- 0x10014fed0 /abs/fn.hlil   # written by Binary Ninja
OUT must be absolute: the script runs inside Binary Ninja, whose working directory isn't
yours (redirecting stdout avoids the question). Clearing the skip is a change to the view;
it persists only if the view is saved.
"""
import os
import sys

if len(argv) not in (2, 3):
    sys.exit("decompile: usage: decompile -- ADDR [ABSOLUTE_OUT]")
try:
    addr = int(argv[1], 16)
except ValueError:
    sys.exit("decompile: bad address %r" % argv[1])
out = argv[2] if len(argv) == 3 else None
if out is not None and not os.path.isabs(out):
    sys.exit("decompile: OUT must be an absolute path (Binary Ninja's working directory is "
             "not yours), or omit it and redirect stdout")

f = bv.get_function_at(addr)
if f is None:
    sys.exit("decompile: no function starts at %#x" % addr)
skipped_before = f.analysis_skipped
if skipped_before:
    reason = getattr(f.analysis_skip_reason, "name", f.analysis_skip_reason)
    f.analysis_skipped = False
    bv.update_analysis_and_wait()
    f = bv.get_function_at(addr)
    if f.analysis_skipped:
        sys.exit("decompile: %s is still skipped after un-skipping (%s)" % (f.name, reason))
text = str(f.hlil)
if out is None:
    print(text)
else:
    with open(out, "w") as fh:
        fh.write(text + "\n")
    print("%s: wrote %s (%d lines%s)" % (f.name, out, text.count("\n") + 1,
                                         ", was skipped: %s" % reason if skipped_before else ""))
