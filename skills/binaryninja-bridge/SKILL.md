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
Every run is a job: `bnrun` submits it, gets a run id and polls until it finishes -- no client timeout (also not while queued behind someone's long run); only `--timeout N` stops it (counted once the job's turn comes, including its `--bndb` open). Bridge briefly unreachable → it retries for 5 min, then exits 2 printing the run id. A `bnrun` that seems to hang is waiting on a queue: `--status`. **Don't resubmit a batch whose bnrun died -- find its job instead** (resubmitting double-applies):
```sh
$BNRUN --detach --bndb $DB annotate -- R:...   # prints the run id, returns at once (long/big batches)
$BNRUN --wait RUN_ID                           # output + exit status of that job (Ctrl-C only stops waiting)
$BNRUN --jobs                                  # this session's jobs (full run ids, done ones for 1 h) + lost ones
$BNRUN --cancel RUN_ID                         # exactly that job (also queued); backgrounded bnruns share your session id
```
**Lost jobs** (BN force-quit/crashed/Stopped while they were queued or running) stay on disk, shown as `lost: N` in `--status`; `--wait` on one exits 4. Nothing reruns on its own: `--requeue RUN_ID|all` reruns never-started ones (and waits); a job that was *running* needs `--force` -- check first, it may be half-applied or be what wedged BN. `--forget RUN_ID|all` drops them. A job that *finished* before a crash still answers `--wait`/`--jobs` as done (outcome kept on disk for 1 h) -- check that before deciding anything was lost.
These need the plugin from 2026-10-14 or later (loaded at a BN restart); against an older one `bnrun` falls back to one held-open request (still no client timeout), `--detach`/`--wait` are refused, and `annotate --save` / `bnannotate --save` on a `--bndb` run fail with "--save needs a --bndb run" (no `save_bndb`): restart BN, or edit without `--save` and run `--bndb-save` after.

## `--bndb` fast path (no lock, no re-analysis)
```sh
$BNRUN --bndb /path/target.bndb -e 'result = len(list(bv.functions))'
$BNRUN --bndb-save            # write renames etc. into the .bndb (or save_bndb() inside the editing job -- preferred)
$BNRUN --bndb-list            # cached views (also in --status), "(unsaved changes)" = edits a crash would lose; --bndb-close frees them
```
Opens the file headlessly once, caches it and hands it to every later run as `bv`. A `.bndb` opens **without updating its analysis** (saved functions/names/types are there; IL is computed per function on demand) -- call `bv.update_analysis_and_wait()` in the script only if you need fresh analysis (it can take 20+ min on a kernelcache). Otherwise instant after the first open (big `.bndb` ~1 min, not a hang), no need to take the lock, carries renames. Prefer it over `binaryninja.load()` per script. Serialized; not with `--view`/`--main-thread`/`--parallel`; `bvs` unavailable. Scripts get `save_bndb()`: save in the same job as the edits, so a dead client can't leave them unsaved.
- **Always `--timeout` on a big first open.** `--status` shows `opening the bndb (N% loaded) ... for Ns` while it opens; for a `.bndb`, `--timeout`/`--cancel` abort the load itself. It climbing with no CPU = starved by another heavy load (or a double open). `--timeout`/`--cancel` give up waiting: the job ends cancelled and the lane frees, while the native open goes on (`still opening: PATH`; runs on that file are refused until it finishes, then it is cached). Details: `reference.md`.

## Lanes and heavy loads
- Default lane is serialized (one at a time). `--parallel` (own throwaway views: `bv = binaryninja.load(path)` … `bv.file.close()` -- `BinaryView` has no `close()`; no UI/shared state, max 4) runs beside it, needs no lock of its own, can't combine with `--view`/`--main-thread`.
- **Lanes don't isolate BN's native core**: a heavy parallel load (+ analysis + `create_database`) can starve or deadlock a big `--bndb` first open (2026-10-14: 51 MB kernelcache vs 1.23 GB `.bndb`, both wedged). Before either: `--status`, announce to the sessions sharing BN, `--timeout` on both. Huge raw binary → `--detach --bndb RAW --timeout N -e 'result = len(list(bv.functions))'` (load + analyse + cache, one job), then `--bndb-save` -- not one blocking parallel load-analyse-save script.

## Usage lock: take it only for the UI views
**Take the lock only when the script touches what's open in the BN window** -- the shared UI state:
- `bv`/`bvs` *without* `--bndb` (the UI tabs), `--view SUBSTR`, `--main-thread`;
- editing or saving a database that is open in a UI tab (`save_auto_snapshot()` on a UI view);
- MCP `bn_*` tools that switch the active view, edit or save (`bn_owner_set` takes the same lock).

**No lock for** `--bndb` runs (own cached headless view, incl. edits + `save_bndb()`), `--parallel` scripts on their own `binaryninja.load()` views, or `--status`/`--jobs`/`--wait`. Prefer these: most analysis never needs the lock. `bnrestart` takes it by itself.

**Someone else holds it?** Your `--bndb` and `--parallel` runs (and `--bndb-save`/`--bndb-close`) still go ahead; only runs that could use the UI views wait (exit 3). An **exclusive** lock (`--lock --exclusive`, e.g. `bnrestart` while saving and quitting) refuses those too. Going ahead next to a holder shares BN's native core with it: keep heavy loads announced (see Lanes).
**Hold it briefly:** a held lock still makes other sessions' UI work wait, so take it right before the UI work and release it right after -- never across a long `--bndb`/`--parallel` batch.
```sh
$BNRUN --lock --purpose "renaming in the UI tab of foo"   # take/renew (ttl 5 min; your runs renew it)
$BNRUN --view foo -e '...'                                # the UI work
$BNRUN --unlock                                           # release right after, before anything long
```
- Without the lock, `bv`/`bvs` raise `UIViewsUnavailable` and `--view`/`--main-thread` are refused (exit 2).
- Need the UI views while another session holds the lock → wait or message them via `SendMessage` (the refusal shows holder, purpose and expiry), or do the work through `--bndb` instead. Stale lock: `--unlock --force` or `--cancel --force` only after asking.
- `--timeout N` on every long run (cancellation lands only between bytecodes; a long native call ignores it until it returns).

## Bundled scripts (`/Users/patrick/dev/bn-script-bridge/scripts/`) -- use these, don't rewrite them
```sh
$BNRUN --bndb $DB annotate -- R:0x401000:parse_header D:0x405000:cfg_table "C:0x401020:token refresh race"
$BNRUN --bndb $DB info -- F:0x401000 S:maxFunctionSize    # function + analysis-skip state; settings
$BNRUN --bndb $DB decompile -- 0x401000 > /tmp/f.hlil     # un-skips huge functions, prints full HLIL
BNANNOTATE=/Users/patrick/dev/bn-script-bridge/bnannotate
$BNANNOTATE --bndb $DB --save R:0x401000:parse_header "C:0x401020:..."   # annotate + save, one job
```
`annotate`: `R:` renames the function **starting** at ADDR, `D:` a data symbol, `C:` sets a comment (replaces it; the old text is printed). All directives are validated first: one bad → nothing applied, exit 1. Works on `--view` too (with the lock). `bnannotate`: directive-shaped args go to annotate, the rest to bnrun; `--save` saves the `.bndb` inside the same job after a successful batch (`annotate -- --save ...`), so a dead client can't orphan the save; it does nothing on a failed batch.

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
`fn.hlil` on a function BN skipped (`fn.analysis_skipped`, huge ones) forces its analysis in one native call that ignores `--timeout` until it returns (529 blocks: >120 s). In loops, skip those (`if fn.analysis_skipped: continue`); for one you need, use the bundled `decompile` with a generous `--timeout`.
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
