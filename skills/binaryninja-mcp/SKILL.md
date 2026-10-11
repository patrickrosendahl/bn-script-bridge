---
name: binaryninja-mcp
description: Use whenever analysing a binary (ELF, Mach-O, PE, firmware, app frameworks) in the running Binary Ninja app -- selecting the right view, listing symbols/functions/strings, reading disassembly/decompilation/IL, tracing callers/xrefs, and annotating findings (comments, renames, types). All of it runs through the `bnrun` script bridge (the full Binary Ninja Python API inside the running app); there are no `bn_*` MCP tools any more. Trigger for "load this into Binary Ninja", "look at this binary in BN", "what does function X do", "which API does this app call", "find where this string is used" -- not only when Binary Ninja is named. Covers bnrun mechanics, the shared usage lock across sessions, and the Personal-licence constraints; which binaries a project analyses and where findings go are in that project's CLAUDE.md and a `project-notes.md` next to this file if present.
---

# Using Binary Ninja via the `bnrun` script bridge

Binary Ninja analysis in this setup runs **entirely through `bnrun`**, which
executes a Python script *inside the running Binary Ninja desktop app* (full
API) and returns its output. There are **no `bn_*` MCP tools** — they were
removed; everything they did is a one- or few-line Python snippet here, and
one script can batch what used to be many tool calls.

`bnrun` lives at **`/Users/patrick/dev/bn-script-bridge/bnrun`** (not on
`PATH` — use the full path, or alias it). `bnrestart` is beside it.
Setup/security notes: `/Users/patrick/dev/bn-script-bridge/README.md`.

## Why a bridge (Personal licence)
`import binaryninja` works in a plain `python3` (append `/Applications/Binary
Ninja.app/Contents/Resources/python` to `sys.path`), but `binaryninja.load()`
fails `License is not valid` — no headless API. The app itself holds the
licence, so scripts must run *inside* it, which is what the bridge does.
Nothing to build; the API ships in the app.

## Is it up?
It autostarts (`scriptbridge.autostart`). Check:
```sh
/Users/patrick/dev/bn-script-bridge/bnrun --status      # free / locked by ... + running scripts
```
Exit 2 "not found"/"unreachable" = the bridge isn't running: ask the user to
start it (**Plugins > Script Bridge > Start**); don't assume it's broken.
"Connection refused" means the same. Plugin code changes load only on an app
restart (`bnrestart`), which hits every session sharing the instance —
coordinate first.

## bnrun basics
```sh
BNRUN=/Users/patrick/dev/bn-script-bridge/bnrun
$BNRUN -e 'result = [bv.file.filename for bv in bvs]'   # inline code
$BNRUN script.py                                        # file, or stdin
```
Inside a script you have: `bv` (the target view), `bvs` (all open UI views),
`bn`/`binaryninja` (the API module). `print(...)` output comes back; set
`result = ...` for structured output (JSON if serialisable, else `repr`).
Flags: `--view SUBSTRING` picks the open view whose filename contains it
(needs the lock); `--json` raw response; `--timeout N` cancels after N s;
`--main-thread` for UI objects (`binaryninjaui`; blocks the UI, needs lock).
Exit 0 ok / 1 script error (traceback on stderr) / 2 bridge refused or
unreachable / 3 locked by someone else / 130 interrupted.

## The usage lock (do this before touching UI views or editing)
One session at a time may hold Binary Ninja. Identity is
`$CLAUDE_CODE_SESSION_ID` (shown as `$BN_SESSION_NAME`).
```sh
$BNRUN --status                       # free, or "locked by X: purpose" + running scripts
$BNRUN --lock --purpose "decompiling foo"   # take/renew (default ttl 5 min; your scripts renew it)
$BNRUN --unlock                       # release when done ( --force releases a stale one -- ask first)
```
- **UI views need the lock.** `bv`, `bvs`, `--view`, `--main-thread` only work
  while you hold it; without it they are refused (exit 2 / `UIViewsUnavailable`).
- **Own throwaway views need no lock** — use `--parallel`: a script that does
  its *own* `BinaryViewType[...].create(BinaryView.open(path))` /
  `binaryninja.load(path)` then `close()`, touching no UI view, the active
  view, or databases. Up to `scriptbridge.maxParallel` (4) run at once.
  `--parallel` can't combine with `--view`/`--main-thread`.
- Locked by another session → don't use BN; message them over `SendMessage`.
  A `--cancel` stops your running scripts; the lock holder (or `--force`) can
  cancel others' — ask first. Cancellation lands between Python bytecodes, so
  bound long jobs as loops over small steps and pass `--timeout`.

## Picking the view
The UI-active view and the view you want can differ. A single open item
usually has a parsed view (`ELF`/`Mach-O`/`PE` — the one you want) and a `Raw`
view. With the lock, `--view SUBSTRING` selects by filename; without `--view`,
`bv` is the UI-active view (or the only open one). `bvs` empty = nothing open
— open the file in the BN UI first. Inspect:
```sh
$BNRUN -e 'result = [(bv.file.filename, bv.view_type, len(list(bv.functions))) for bv in bvs]'
$BNRUN --view myfile -e 'result = {"type": bv.view_type, "entry": hex(bv.entry_point), "analysis": str(bv.analysis_info.state)}'
```
`bv.view_type == "Raw"` means switch to the parsed one. Wait for analysis idle
(`bv.update_analysis_and_wait()` in a script, or check `analysis_info.state`)
before trusting results.

## Common recipes
```sh
# Find functions/symbols by name (substring, case-insensitive)
$BNRUN --view f -e 'result = [(hex(fn.start), fn.name) for fn in bv.functions if "parse" in fn.name.lower()]'
$BNRUN --view f -e 'result = [(hex(s.address), s.full_name) for s in bv.get_symbols() if "crypt" in s.full_name.lower()]'

# Decompile (HLIL) / disassemble / MLIL a function by name or address
$BNRUN --view f -e 'fn = bv.get_functions_by_name("main")[0]; result = str(fn.hlil)'
$BNRUN --view f -e 'fn = bv.get_function_at(0x401000); result = "\n".join(f"{hex(i.address)}  {i}" for b in fn.instructions for i in [b])'  # see note
$BNRUN --view f -e 'fn = bv.get_functions_by_name("main")[0]; result = str(fn.mlil)'

# Callers / callees / xrefs to an address
$BNRUN --view f -e 'fn = bv.get_functions_by_name("send")[0]; result = sorted({c.function.name for c in bv.get_code_refs(fn.start)})'
$BNRUN --view f -e 'fn = bv.get_functions_by_name("main")[0]; result = [cs.function.name for cs in fn.callees]'

# Read bytes (returns bytes; decode in-script, do not hand-transcribe base64)
$BNRUN --view f -e 'import struct; result = struct.unpack("<I", bv.read(0x402000, 4))[0]'

# Strings
$BNRUN --view f -e 'result = [(hex(s.start), s.value) for s in bv.strings if "http" in s.value.lower()]'
```
`fn.instructions` yields `(tokens, length)` tuples per the API version — if a
recipe shape is off, probe it in a throwaway script (`result = type(...)` /
`dir(...)`) rather than guessing; the real API is right there.

## Annotating (record findings in the database)
```sh
# First definition of a name at an address (supersedes the auto sub_XXXX symbol)
$BNRUN --lock --purpose rename --view f -e '
from binaryninja import Symbol, SymbolType
bv.define_user_symbol(Symbol(SymbolType.FunctionSymbol, 0x401000, "parse_header")); result = "ok"'

# Comment at a call site / function (set_comment_at REPLACES; read existing first)
$BNRUN --lock --view f -e 'fn = bv.get_function_at(0x401000); fn.set_comment_at(0x401020, "token refresh race"); result = "ok"'

# Save the .bndb (next to the binary) once a batch of edits is worth keeping
$BNRUN --lock --view f -e 'bv.file.save_auto_snapshot(); result = bv.file.filename'
```
- Renames/comments/types live only in the running instance's RAM until saved.
  `save_auto_snapshot()` persists into the existing `.bndb` (written **next to
  the binary**); a fresh item without one needs `bv.create_database(path)`.
  `.bndb`s are tens of MB — save once after a batch; commit one only where the
  project does (check its CLAUDE.md/.gitignore), never by default.
- Match the naming style already in the database. Mutations request async
  analysis; call `bv.update_analysis_and_wait()` after a batch if later reads
  depend on it.

## Tracing technique
1. Start from BN, not your own instruction scanner: decompile the function of
   interest, then walk `get_code_refs(fn.start)` / `fn.callers` up the chain.
2. The vendor's own strings name things — find a log/error/URL/config string,
   follow its code refs to the using function, name the function after it.
   `get_code_refs` on the string start often finds literal-pool/ADRP+ADD
   globals that a plain data-xref misses.
3. Comment both the function start (summary) and the specific call site.
4. Prove naming hypotheses against runtime data when you can (captured traffic,
   files, traces).
5. "Stripped" ≠ nameless: the dynamic symbol table survives stripping, so
   imported libc/library functions keep real names (`autoDefined`); only the
   binary's own internals are `sub_XXXX`. Check `bv.functions` before hand-
   identifying imports.

## Project-type notes
- **Mach-O apps (iOS `.app` bundles):** the main executable and each
  `Frameworks/*.framework/<name>` are separate open items; Swift/ObjC class
  names survive in symbols/strings (`_TtC...`), so symbol/string search beats
  anonymous `sub_` functions.
- **Java (`.class`/`.jar`):** BN can't open a `.jar` — unpack it next to the
  JAR into a like-named dir (`unzip -o -q foo.jar -d foo`) and open the
  `.class` files; keep `files.container.excludedTransforms = ["Universal"]`
  set (`0xCAFEBABE` clashes with fat Mach-O); switch the view to "JVM Class".
  The `binary-jvm` plugin (`/Users/patrick/dev/binary-jvm`) is installed.

## Concurrent sessions
The open views and the active view are **shared across everything talking to
that Binary Ninja instance** — not per-session. Before a session of work,
`--status` and list open views; if it's unclear whether someone else is using
it, ask over `SendMessage`/`ListAgents` rather than switching views or editing.
The lock is coordination, not security, but it can't be bypassed (the plugin
moves BN's own server to a random port/token), so always go through `bnrun`.

## Gotchas
- `bnrun` exit 2 / "unreachable" → BN not running or Script Bridge stopped; ask
  the user to start it, don't assume breakage.
- UI views (`bv`/`bvs`/`--view`/`--main-thread`) need the lock; own throwaway
  views use `--parallel` and no lock.
- Switch to the parsed view (not `Raw`) before analysis reads.
- Unsaved work lives in RAM — `save_auto_snapshot()` before it's lost; check
  with concurrent sessions before saving someone else's mid-edit `.bndb`.
- `set_comment_at` REPLACES a comment — read the existing one and carry it over.
- Don't hand-decode a large `bv.read` blob — compute in-script (struct/int.from_bytes).
- BN can mis-type integer constants (ioctl codes, flags) as string-table
  pointers — they show as nonsense strings in calls; treat as ints, note it.
- `bnrun` hangs = a script (maybe another session's) is running/queued:
  `--status`, then `--cancel` (own) or ask before `--cancel --force`. A cancel
  doesn't interrupt a native call in progress (`update_analysis_and_wait`); it
  fires when the call returns. A cancelled script exits 1; partial edits stay.

## Project specifics
Which binaries this project analyses, its naming conventions, and where
findings are written (wiki pages, tickets) are **not here** — read this
project's **CLAUDE.md**, and a **`project-notes.md`** next to this skill if one
exists. Write reverse-engineering *findings* there; this skill is tool
mechanics only.
