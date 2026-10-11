---
name: binaryninja-bridge
description: Use whenever analysing any binary (ELF, Mach-O, PE, .class, firmware) in the running Binary Ninja app, through the `bnrun` script bridge (full Binary Ninja Python API inside the app; no `bn_*` MCP tools) -- trigger on any binary analysis, even when Binary Ninja is not named. Environment details: `reference.md` next to this file.
---

# Binary Ninja via the `bnrun` script bridge

The Binary Ninja Python API itself needs no teaching — this setup's specifics only:

- **`BNRUN=/Users/patrick/dev/bn-script-bridge/bnrun`** (not on PATH) runs a Python script *inside the running Binary Ninja app* (Personal licence = no headless API outside it). `-e 'code'`, a script file, or stdin; one script batches many calls. `print()` output and `result` (JSON, else `repr`) come back. Exit: 0 ok / 1 script error / 2 bridge refused or unreachable / 3 locked by another session / 130 interrupted.
- **Script namespace:** `bv` (target view), `bvs` (all UI views), `bn`/`binaryninja`. Flags: `--view SUBSTR` (open view by filename), `--timeout N` (pass it on every long run), `--parallel` (own throwaway views only, no lock, max 4), `--main-thread` (UI objects).
- **Usage lock — UI views are shared across all sessions.** `--lock --purpose "…"` before touching UI views; `bv`/`bvs`/`--view`/`--main-thread` are refused (exit 2) without it; `--unlock` when done (ttl 5 min, your own script runs renew it). Locked by another session → message them (`SendMessage`) instead of fighting; `--force` on unlock/cancel only after asking. `--parallel` scripts (own `binaryninja.load(path)` … `close()`, no UI/shared state) need no lock.
- **Views:** an open item usually has a parsed view (`ELF`/`Mach-O`/`PE`) beside a `Raw` — never read from `Raw`. `bv.update_analysis_and_wait()` before trusting results. `bvs` empty → open the file in the BN UI first.
- **Bridge down (exit 2 / "unreachable"):** BN not running or the bridge stopped — ask the user to start it (**Plugins > Script Bridge > Start**); don't assume breakage. `bnrun` hanging = a script (maybe another session's) is running or queued: `--status`, then `--cancel`.
- **Edits live in RAM until `save_auto_snapshot()`; `.bndb`s are tens of MB** — save once per batch, commit one only where the project's rules say to.
- **No `bn_*` MCP tools exist** — everything is a bnrun script.

**On demand:** `reference.md` next to this file (full flag/exit surface, cancellation limits, `bnrestart`, project-type notes, gotchas). **This project's binaries, conventions and finding destinations:** its CLAUDE.md, plus a `project-notes.md` next to this file if present.
