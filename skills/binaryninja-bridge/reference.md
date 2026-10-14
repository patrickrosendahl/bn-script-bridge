# binaryninja-bridge reference

Cold material; SKILL.md covers the hot path. Read the section you need.

## Full bnrun surface
- Flags: `--view SUBSTR` · `--bndb PATH` (cached headless view) · `-- ARGS` / `--env K=V` (script's `argv`/`env`) · `--max-out N` · `--timeout N` (bridge cancels after N s) · `--json` (raw response) · `--main-thread` (UI objects via `binaryninjaui`; blocks the UI; lock) · `--parallel` (own throwaway views; refuses with `--view`/`--main-thread`) · `--lock --purpose` / `--unlock` [`--force`] / `--status` [`--json`] / `--cancel` [`--force`] · `--detach` / `--wait RUN_ID` / `--jobs` [`--json`] / `--requeue RUN_ID…|all` [`--force`] / `--forget RUN_ID…|all` · `--session`/`--name` (default `$CLAUDE_CODE_SESSION_ID` / `$BN_SESSION_NAME`).
- Exit codes: 0 ok · 1 script error or cancelled (traceback on stderr) · 2 bridge refused/unreachable (incl. gave up after 5 min unreachable while waiting) or UI-view rules violated · 3 locked by another session · 4 job lost · 130 Ctrl-C.
- Jobs: bnrun submits (`/run` with `async`), then long-polls `/result` -- no request is held open for the run and there is no client-side timeout. Resubmitting a run id the bridge knows answers that job (never runs twice). Finished jobs keep their reply for 1 h (`--wait`, `--jobs`). The bridge spools queued/running jobs to `<BN user dir>/script_bridge_spool/`; ones left there when a bridge starts become **lost** (`lost/`), reported in `--status`, `--jobs`, `--wait` (exit 4) and Binary Ninja's log, until `--requeue` (as new jobs of the requeuing session; `--force` for ones that were running) or `--forget`. `--status` lines carry `job <first 8 of run id>` and full `--bndb` paths; `--status --json` / `--jobs --json` are the stable formats for watchers (`jobs`, `queued`, `parallel_queued`, `lost`).
- Lock ttl 5 min (max 240); the holder's own script runs renew it. `--cancel` hits this session's running scripts; the lock holder (or anyone with `--force`) cancels all — ask before touching others'. Ctrl-C/SIGTERM while `bnrun` waits cancels that script inside BN too (per-run run id, only ever its own).
- **Cancellation lands only between Python bytecodes** (`PyThreadState_SetAsyncExc`, a `BaseException` so `except Exception` doesn't swallow it): a script blocked in a long native call (`update_analysis_and_wait`, `time.sleep`, one big API call) stops only when it returns. Write long jobs as bounded loops over small steps with progress prints and `--timeout`. Partial edits before a cancel stay (no rollback); a script that catches `BaseException` and carries on can't be cancelled.
- SCRIPT ending in `.py` is a file; anything else names a bundled script in `scripts/` (unknown name: exit 2 with the list). Each documents its arguments at the top.
- Writing scripts: errors go through `print()` (the bridge returns `print()` output, not `sys.stderr`); end a failed run with `sys.exit("message")` -- exit 1, message on stderr, no traceback (`sys.exit()`/`sys.exit(0)` = success). `--bndb` and `--bndb-save`/`--bndb-close` paths are made absolute by `bnrun`, other paths aren't.
- **Hung `--bndb` open** (status `opening the bndb ... for Ns` and climbing; typically the file is also open in a UI tab, though the plugin now refuses that case when it can see it): the open counts against `--timeout` and `--cancel` works during it. Either one abandons the open: the job ends cancelled, the serialized lane is freed, and the path shows `stuck opening:` in `--status`. Further `--bndb` runs on that file are refused until the open finishes (its view is then cached) or BN restarts. The native open itself can't be stopped, so always pass `--timeout` with a big `--bndb`.
- `bnrun` hanging = a script (maybe another session's) is running or queued: `--status`, then `--cancel` (own) or ask before `--cancel --force`. Killing your waiting `bnrun` (other than Ctrl-C/SIGTERM, which cancel) leaves the job running: `--jobs` finds it, `--wait` collects it.

## The `--bndb` cache
- A `.bndb` opens its database (renames included); a raw binary path is analysed once and `--bndb-save` then creates `<file>.bndb` beside it. `bnrun` waits for `--bndb` runs as long as they take (see Jobs); `--bndb-save` of a raw binary writes a whole database (300 s).
- `--bndb-save`/`--bndb-close` take an optional PATH (all cached views without one) and are refused while a serialized script runs. Trap: `--bndb-save foo.py` takes `foo.py` as the PATH -- management commands run no script.
- **One handle per `.bndb`**: opening a database twice (cached view + UI tab or MCP open) gives two handles that both look writable, but then saves fail on both until one closes unsaved. The bridge refuses a `--bndb` open of a file already open in the UI, and the MCP proxy refuses opening a cached one. Close the other side first.
- Stop/quit closes the cache **unsaved**; `bnrestart` saves it first. While another session holds the lock, `--bndb` runs are refused (exit 3) like any other.

## Disassembly, per line
`fn.instructions` yields `(tokens, length)` tuples per the API version — if the shape is off, probe it in a throwaway script (`result = type(...)`, `dir(...)`) rather than guessing; the real API is right there:
```sh
$BNRUN --view f -e 'fn = bv.get_function_at(0x401000); result = "\n".join(f"{hex(i.address)}  {i}" for b in fn.instructions for i in [b])'  # see note
```

## Tracing technique
1. Start from BN, not your own instruction scanner: decompile the function of interest, then walk `get_code_refs(fn.start)` / `fn.callers` up the chain.
2. The vendor's own strings name things — find a log/error/URL/config string, follow its code refs to the using function, name the function after it. `get_code_refs` on the string start often finds literal-pool/ADRP+ADD globals that a plain data-xref misses.
3. Comment both the function start (summary) and the specific call site.
4. Prove naming hypotheses against runtime data when you can (captured traffic, files, traces).
5. "Stripped" ≠ nameless: the dynamic symbol table survives stripping, so imported libc/library functions keep real names (`autoDefined`); only the binary's own internals are `sub_XXXX`. Check `bv.functions` before hand-identifying imports.

## Project-type notes
- **Mach-O apps (iOS `.app` bundles):** the main executable and each `Frameworks/*.framework/<name>` are separate open items; Swift/ObjC class names survive in symbols/strings (`_TtC...`) — symbol/string search beats anonymous `sub_` functions.
- **Java (`.class`/`.jar`):** BN can't open a `.jar` — unpack it next to the JAR into a like-named dir (`unzip -o -q foo.jar -d foo`) and open the `.class` files; keep `files.container.excludedTransforms = ["Universal"]` set (`0xCAFEBABE` clashes with fat Mach-O); switch the view to "JVM Class". The `binary-jvm` plugin (`/Users/patrick/dev/binary-jvm`) is installed.

## Restarting Binary Ninja (`bnrestart`, beside `bnrun`)
Plugin code changes load only on an app restart — `bnrestart` saves existing `.bndb`s, quits (AppleScript), relaunches (`open -g`, reopens previous files) and waits for the bridge. It restarts the GUI **for every session sharing the instance** — coordinate first. Refuses (exit 3) while another session holds the lock or scripts run/queue (`--force` overrides). Modified views without a `.bndb` need `--save-new` (create one next to the binary) or `--discard` (quit without saving). Pin BN to its macOS Space (Dock → Options → Assign To → This Desktop) so relaunches don't steal focus.

## Concurrent sessions
Open views and the active view are **shared across everything talking to that Binary Ninja instance** — not per-session. Before a session of work, `--status` and list open views; if it's unclear whether someone else is using it, ask over `SendMessage`/`ListAgents` rather than switching views or editing. The lock is coordination, not security, but it can't be bypassed (the plugin moves BN's own server to a random port/token), so always go through `bnrun`. Check before saving someone else's mid-edit `.bndb`.

## Gotchas
- BN can mis-type integer constants (ioctl codes, flags) as string-table pointers — they show as nonsense strings in calls; treat as ints, note it.
- `set_comment_at` REPLACES a comment — read the existing one and carry it over.
- Don't hand-decode a large `bv.read` blob — compute in-script (struct/int.from_bytes).
- Unsaved work lives in RAM — `save_auto_snapshot()` (or `--bndb-save` for a cached view) before it's lost.
- A cancel doesn't interrupt `binaryninja.load` of a big `.bndb` either (native call); it fires when the load returns.
