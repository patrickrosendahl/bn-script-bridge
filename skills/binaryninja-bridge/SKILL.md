---
name: binaryninja-bridge
description: Use whenever analysing a binary (ELF, Mach-O, PE, .class, firmware) in the running Binary Ninja app -- symbols/functions/strings, disassembly/decompilation/IL, xrefs, annotating renames/comments/types. All through the `bnrun` script bridge (full Python API inside the app; no `bn_*` MCP tools). Trigger on "load this into Binary Ninja", "what does function X do", "find where this string is used" -- any binary analysis, not only when Binary Ninja is named. Full recipes, gotchas, restarts, project-type notes: `reference.md` next to this file.
---

# Binary Ninja via the `bnrun` script bridge

`BNRUN=/Users/patrick/dev/bn-script-bridge/bnrun` (not on PATH; `bnrestart` beside it; setup notes in that repo's README) runs a Python script **inside the running Binary Ninja app** (Personal licence: no headless API) and returns its output. One script batches many tool calls.

```sh
$BNRUN --status                                # free / locked by <session>: <purpose>
$BNRUN -e 'result = ...'                       # inline code (file or stdin too)
```

In-script: `bv` (target view), `bvs` (all UI views), `bn`/`binaryninja` (API). `print()` output is returned; set `result` for structured output (JSON, else `repr`). Exit: 0 ok / 1 script error / 2 bridge refused or unreachable / 3 locked by another session / 130 interrupted.

## Usage lock (UI views are shared — take it before touching them)
```sh
$BNRUN --lock --purpose "decompiling foo"      # take/renew (ttl 5 min; your scripts renew)
$BNRUN --unlock
```
- `bv`/`bvs`/`--view`/`--main-thread` need the lock (refused with exit 2 without it).
- `--parallel` (own throwaway views: `binaryninja.load(path)` … `close()`, no UI/shared state, max 4) needs none — can't combine with `--view`/`--main-thread`.
- Locked by another session → don't use BN; message them via `SendMessage`. Stale lock: `--unlock --force` or `--cancel --force` only after asking.
- `--timeout N` on every long run (cancellation lands only between bytecodes).

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
