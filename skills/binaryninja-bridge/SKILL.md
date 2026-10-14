---
name: binaryninja-bridge
description: Use whenever analysing a binary (ELF, Mach-O, PE, .class, firmware) in the running Binary Ninja app -- symbols/functions/strings, disassembly/decompilation/IL, xrefs, annotating renames/comments/types. All through the `bnrun` script bridge (full Python API inside the app; no `bn_*` MCP tools). Trigger on "load this into Binary Ninja", "what does function X do", "find where this string is used" -- any binary analysis, not only when Binary Ninja is named. Full recipes, gotchas, restarts, project-type notes: `reference.md` next to this file.
---

# Binary Ninja via the `bnrun` script bridge

`BNRUN=/Users/patrick/dev/bn-script-bridge/bnrun` (not on PATH; `bnrestart` beside it; setup notes in that repo's README) runs a Python script **inside the running Binary Ninja app** (Personal licence: no headless API) and returns its output. One script batches many tool calls.

```sh
$BNRUN --status                                # free / locked by <session>: <purpose>; running/queued/lost jobs
$BNRUN --status --json                         # same, machine-readable -- parse this, never the prose
$BNRUN -e 'result = ...'                       # inline code (file or stdin too)
```

In-script: `bv` (target view), `bvs` (all UI views), `bn`/`binaryninja` (API), `argv`/`env` (from `-- ARGS` / `--env K=V`; serialized runs also get `sys.argv`/`os.environ`). `print()` output is returned; set `result` for structured output (JSON, else `repr`). Exit: 0 ok / 1 script error / 2 bridge refused or unreachable / 3 locked by another session / 4 job lost / 130 interrupted.

SCRIPT ending in `.py` is your file; anything else is a **bundled script** (next section). Scripts run inside Binary Ninja, whose cwd isn't yours: print and redirect, or pass absolute paths. Parameterize instead of splicing strings: `$BNRUN dump.py -- 0x401000 50` → `int(argv[1], 16)`. Big output: `--max-out N` (full text goes to a file under `~/.cache/bn-script-bridge/`, path in the last line).

## Jobs: bnrun never abandons a submitted script
Every run is a job: `bnrun` submits it, gets a run id and polls until it finishes -- no client timeout (also not while queued behind someone's long run); only `--timeout N` (counted from the script's start) stops it. Bridge briefly unreachable → it retries for 5 min, then exits 2 printing the run id. **Don't resubmit a batch whose bnrun died -- find its job instead** (resubmitting double-applies):
```sh
$BNRUN --detach --bndb $DB annotate -- R:...   # prints the run id, returns at once (long/big batches)
$BNRUN --wait RUN_ID                           # output + exit status of that job (Ctrl-C only stops waiting)
$BNRUN --jobs                                  # this session's jobs (full run ids, done ones for 1 h) + lost ones
```
**Lost jobs** (BN force-quit/crashed/Stopped while they were queued or running) stay on disk, shown as `lost: N` in `--status`; `--wait` on one exits 4. Nothing reruns on its own: `--requeue RUN_ID|all` reruns never-started ones (and waits); a job that was *running* needs `--force` -- check first, it may be half-applied or be what wedged BN. `--forget RUN_ID|all` drops them.

## `--bndb` fast path (no lock, no re-analysis)
```sh
$BNRUN --bndb /path/target.bndb -e 'result = len(list(bv.functions))'
$BNRUN --bndb-save            # write renames etc. into the .bndb -- after each batch of edits
$BNRUN --bndb-list            # cached views (also in --status); --bndb-close frees them
```
Opens the file headlessly once, caches it and hands it to every later run as `bv` -- instant after the first open (big `.bndb` ~1 min, not a hang), no usage lock, carries renames. Prefer it over `binaryninja.load()` per script. Serialized; not with `--view`/`--main-thread`/`--parallel`; `bvs` unavailable. Details: `reference.md`.

## Usage lock (UI views are shared — take it before touching them)
```sh
$BNRUN --lock --purpose "decompiling foo"      # take/renew (ttl 5 min; your scripts renew)
$BNRUN --unlock
```
- `bv`/`bvs`/`--view`/`--main-thread` need the lock (refused with exit 2 without it).
- `--parallel` (own throwaway views: `binaryninja.load(path)` … `close()`, no UI/shared state, max 4) needs none — can't combine with `--view`/`--main-thread`.
- Locked by another session → don't use BN; message them via `SendMessage`. Stale lock: `--unlock --force` or `--cancel --force` only after asking.
- `--timeout N` on every long run (cancellation lands only between bytecodes).

## Bundled scripts (`/Users/patrick/dev/bn-script-bridge/scripts/`) -- use these, don't rewrite them
```sh
$BNRUN --bndb $DB annotate -- R:0x401000:parse_header D:0x405000:cfg_table "C:0x401020:token refresh race"
$BNRUN --bndb $DB info -- F:0x401000 S:maxFunctionSize    # function + analysis-skip state; settings
$BNRUN --bndb $DB decompile -- 0x401000 > /tmp/f.hlil     # un-skips huge functions, prints full HLIL
BNANNOTATE=/Users/patrick/dev/bn-script-bridge/bnannotate
$BNANNOTATE --bndb $DB --save R:0x401000:parse_header "C:0x401020:..."   # annotate + --bndb-save
```
`annotate`: `R:` renames the function **starting** at ADDR, `D:` a data symbol, `C:` sets a comment (replaces it; the old text is printed). All directives are validated first: one bad → nothing applied, exit 1. Works on `--view` too (with the lock). `bnannotate`: directive-shaped args go to annotate, the rest to bnrun; `--save` only after a successful batch.

## Views
One open item usually has a parsed view (`ELF`/`Mach-O`/`PE`) and a `Raw` — never read from `Raw`. `--view SUBSTR` picks by filename; without it, `bv` is the UI-active view. Wait for analysis idle (`bv.update_analysis_and_wait()`) before trusting results.
```sh
$BNRUN -e 'result = [(bv.file.filename, bv.view_type, len(list(bv.functions))) for bv in bvs]'
```
`bvs` empty → open the file in the BN UI first. Exit 2 / unreachable → BN not running or the bridge stopped: ask the user to start it (**Plugins > Script Bridge > Start**) — don't assume breakage.

## Core recipes
```sh
$BNRUN --view f -e 'result = [(hex(fn.start), fn.name) for fn in bv.functions if "parse" in fn.name.lower()]'
$BNRUN --view f -e 'result = [(hex(s.address), s.full_name) for s in bv.get_symbols() if "crypt" in s.full_name.lower()]'
$BNRUN --view f -e 'fn = bv.get_functions_by_name("main")[0]; result = str(fn.hlil)'            # or .mlil
$BNRUN --view f -e 'fn = bv.get_functions_by_name("send")[0]; result = sorted({c.function.name for c in bv.get_code_refs(fn.start)})'  # callers; fn.callees too
$BNRUN --view f -e 'result = [(hex(s.start), s.value) for s in bv.strings if "http" in s.value.lower()]'
$BNRUN --view f -e 'import struct; result = struct.unpack("<I", bv.read(0x402000, 4))[0]'      # decode in-script, never hand-transcribe blobs
```
Recipe shape off (API drift) → probe it (`result = type(x)`, `dir(x)`) instead of guessing.

## Annotate + save
Batches of renames/comments: `annotate` / `bnannotate` above. By hand:
```sh
$BNRUN --lock --purpose rename --view f -e '
from binaryninja import Symbol, SymbolType
bv.define_user_symbol(Symbol(SymbolType.FunctionSymbol, 0x401000, "parse_header")); result = "ok"'
$BNRUN --lock --view f -e 'fn = bv.get_function_at(0x401000); fn.set_comment_at(0x401020, "token refresh race"); result = "ok"'   # REPLACES: read existing first
$BNRUN --lock --view f -e 'bv.file.save_auto_snapshot(); result = bv.file.filename'
```
Edits live in RAM until `save_auto_snapshot()` (into the `.bndb` next to the binary; a fresh item needs `bv.create_database(path)`); `.bndb`s are tens of MB — save per batch, commit only where the project does. Match the naming style already in the database; `update_analysis_and_wait()` after a batch if later reads depend on it.

## Cold material → `reference.md` (next to this file)
Full flag/exit surface, cancellation limits · disassembly recipe · tracing technique · project-type notes (Mach-O apps, Java `.jar`) · `bnrestart` (plugin reload; coordinate across sessions) · concurrent-session rules · gotchas. Which binaries a project analyses and where findings go: that project's CLAUDE.md and a `project-notes.md` next to this file if present.
