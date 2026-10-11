# binaryninja-bridge reference

Cold environment details; the SKILL.md covers the hot path. Read the section you need.

## Full bnrun surface
- Flags: `--view SUBSTR` · `--timeout N` (bridge cancels after N s) · `--json` (raw response) · `--main-thread` (UI objects via `binaryninjaui`; blocks the UI; lock) · `--parallel` (own throwaway views; refuses with `--view`/`--main-thread`) · `--lock --purpose` / `--unlock` [`--force`] / `--status` / `--cancel` [`--force`] · `--session`/`--name` (default `$CLAUDE_CODE_SESSION_ID` / `$BN_SESSION_NAME`).
- Exit codes: 0 ok · 1 script error or cancelled (traceback on stderr) · 2 bridge refused/unreachable or UI-view rules violated · 3 locked by another session · 130 Ctrl-C.
- Lock ttl 5 min (max 240); the holder's own script runs renew it. `--cancel` hits this session's running scripts; the lock holder (or anyone with `--force`) cancels all — ask before touching others'. Ctrl-C/SIGTERM while `bnrun` waits cancels that script inside BN too (per-run run id, only ever its own).
- **Cancellation lands only between Python bytecodes** (`PyThreadState_SetAsyncExc`, a `BaseException`): a script blocked in a long native call (`update_analysis_and_wait`, `time.sleep`, one big API call) stops only when it returns. Write long jobs as bounded loops over small steps with progress prints and `--timeout`. Partial edits before a cancel stay (no rollback).
- `bnrun` hanging = a script (maybe another session's) is running or queued: `--status`, then `--cancel` (own) or ask before `--cancel --force`.
- A recipe/shape assumption off (API drift) → probe it in a throwaway script (`type(x)`, `dir(x)`); the real API is right there. BN can mis-type integer constants (ioctl codes, flags) as string-table pointers — nonsense strings in calls are ints, not strings.

## Restarting Binary Ninja (`bnrestart`, beside `bnrun`)
Plugin code changes load only on an app restart — `bnrestart` saves existing `.bndb`s, quits (AppleScript), relaunches (`open -g`, reopens previous files) and waits for the bridge. It restarts the GUI **for every session sharing the instance** — coordinate first. Refuses (exit 3) while another session holds the lock or scripts run/queue (`--force` overrides). Modified views without a `.bndb` need `--save-new` (create one next to the binary) or `--discard` (quit without saving). Pin BN to its macOS Space (Dock → Options → Assign To → This Desktop) so relaunches don't steal focus.

## Concurrent sessions
Open views and the active view are **shared across everything talking to that Binary Ninja instance** — not per-session. Before a session of work, `--status` and list open views; if it's unclear whether someone else is using it, ask over `SendMessage`/`ListAgents` rather than switching views or editing. The lock is coordination, not security, but it can't be bypassed (the plugin moves BN's own server to a random port/token), so always go through `bnrun`. Check before saving someone else's mid-edit `.bndb`.

## Project-type notes
- **Java (`.class`/`.jar`):** BN can't open a `.jar` — unpack it next to the JAR into a like-named dir (`unzip -o -q foo.jar -d foo`) and open the `.class` files; keep `files.container.excludedTransforms = ["Universal"]` set (`0xCAFEBABE` clashes with fat Mach-O); switch the view to "JVM Class". The `binary-jvm` plugin (`/Users/patrick/dev/binary-jvm`) is installed.
- **iOS `.app` bundles:** each `Frameworks/*.framework/<name>` is a separate open item from the main executable — check `bvs` for all of them before concluding something isn't there.
