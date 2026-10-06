# bn-script-bridge

Run Python scripts inside the running Binary Ninja app from the command line. The Personal
licence refuses the API outside the app (`binaryninja.load()` from a plain `python3` fails with
"License is not valid"), and the Binary Ninja MCP server only offers a fixed tool set. This
plugin listens on 127.0.0.1 and runs the scripts that `bnrun` sends, with the full Python API.

## Install

```sh
ln -s /Users/patrick/dev/bn-script-bridge ~/Library/Application\ Support/Binary\ Ninja/plugins/bn-script-bridge
```

Then restart Binary Ninja. Plugins load only at startup; if several sessions share the
instance, coordinate the restart. `bnrun` can be run from here or symlinked onto `PATH`.

## Use

1. In Binary Ninja: **Plugins > Script Bridge > Start** (no open binary needed), or enable
   the setting `scriptbridge.autostart` (Settings, search "script bridge", or
   `Settings().set_bool("scriptbridge.autostart", True)` in the Python console). The log
   shows the port. The menu shows the state: Start is greyed out while the bridge runs,
   Stop while it doesn't.
2. From a shell:

```sh
bnrun --lock --purpose "listing functions"        # UI views need the usage lock
bnrun -e 'print(len(list(bv.functions)))'
bnrun --view cx20707.ko script.py
echo 'result = [f.name for f in bv.functions][:10]' | bnrun
bnrun --unlock

bnrun --parallel --timeout 600 tests/check_my_classes.py   # own throwaway views, no lock
```

The script sees:

- `binaryninja` / `bn`: the API module (always);
- `bv`: the open view whose filename contains `--view`, else the view active in the UI, else
  the only open view (not the MCP server's "active" view, which is separate state);
- `bvs`: all views open in the UI.

`print()` output is returned, and so is `result` if the script sets it (JSON, else `repr`).
Exit status: 0 ok, 1 script error or cancelled, 2 bridge unreachable or refused (including the
UI-view rules below), 3 Binary Ninja locked by someone else (see below), 130 interrupted with
Ctrl-C.

### UI views need the usage lock

A UI tab is only usable while holding the usage lock: `bv` and `bvs` are provided only to a
**serialized** script whose session (`--session`, default `$CLAUDE_CODE_SESSION_ID`) holds the
lock. Otherwise:

- `bv` and `bvs` are placeholders that raise `UIViewsUnavailable` (a `RuntimeError`) with the
  reason on any use (attribute access, `len`, iteration, indexing, truth test); `repr` works.
  They are not `None`/`[]` on purpose: an empty `bvs` would silently make a script do nothing.
- `--view` and `--main-thread` are refused up front (exit 2, "take the usage lock first: bnrun
  --lock --purpose ..."). If the lock is lost while the script waits in the queue, the script
  is refused when it would start.

Scripts that only create their own views through the API (`BinaryViewType[...].create(
BinaryView.open(path))`, `BinaryView.open`, `binaryninja.load`, then `close()`) need no lock.

**Limitation:** this is enforcement at the level of the script namespace, backed by
convention. A script can still reach UI views by other paths (`binaryninjaui.UIContext`, ...);
don't.

### Two lanes: serialized (default) and `--parallel`

- **Serialized** (default): one script at a time on a worker thread, later ones queue behind
  it. `--main-thread` runs it on the UI thread instead (needed for UI objects; it blocks the UI
  while it runs; needs the lock).
- **`--parallel`** (`"parallel": true`): runs on its own worker thread without waiting for the
  serialized lane, next to the serialized script and to other parallel ones, at most
  `scriptbridge.maxParallel` (default 4, Settings, search "script bridge"; changes apply at once)
  at a time; more wait for a slot. Binary Ninja's analysis is native and multithreaded, and
  native calls release the GIL, so several `update_analysis_and_wait()` on separate views do run
  concurrently. `--parallel` with `--main-thread` or `--view` is refused (exit 2); parallel
  scripts get no `bv`/`bvs`, even for the lock holder (so a holder can't queue UI work behind
  itself; its parallel scripts still run).

**Rule of thumb:** UI views or shared state (the open tabs, the MCP active view, databases,
global registrations) → serialized + usage lock. Only your own throwaway views → `--parallel`,
no lock needed. Parallel scripts must not modify UI-opened views, switch the MCP active view or
save databases; nothing but this convention stops a parallel script from doing so.

The usage lock still applies to both lanes: while another session holds it, your scripts
(parallel included) are refused with exit 3.

## Cancelling scripts

```sh
bnrun --status                    # lock, plus one "running script '<first line>' for 42s (...)"
                                  # line per running script (", parallel" marks that lane) and
                                  # "queued: N serialized, M parallel"; "no script running"
                                  # only when both lanes are idle
bnrun --timeout 60 script.py      # the bridge cancels the script after 60 s of running
bnrun --cancel                    # cancel this session's running scripts (both lanes)
bnrun --cancel --force            # cancel all running scripts (also other sessions')
```

- **Ctrl-C** (or SIGTERM, e.g. a tool timeout killing `bnrun`) while `bnrun` waits cancels that
  script inside Binary Ninja too, then exits with 130. Each run carries a random run id, so this
  works without a session id and only ever hits its own script (also while still queued).
- `--cancel` cancels the running scripts (serialized and parallel) of the calling session
  (`--session`). If it has none, the usage-lock holder, or anyone with `--force`, cancels all
  running scripts. Nothing running: exit 1 ("no script is running"); not allowed: exit 3. Each
  script runs in its own thread, so a cancel or `--timeout` only hits the script it targets.
- A cancelled script's reply has `"error": "cancelled"`, `"cancelled_by"` (session, lock holder,
  force, its client, or `timeout after Ns`) and the `print()` output captured so far.
- How: the bridge raises `ScriptCancelled` (a `BaseException`, so a script's `except Exception`
  doesn't swallow it) in the thread running the script, with `PyThreadState_SetAsyncExc`; with
  `--main-thread` that is Binary Ninja's UI thread. A cancel that would land after the script
  finished is cleared, so it never reaches other code on that thread.
- **Limitation:** Python delivers it only between bytecodes. A script blocked in a long native
  call (`bv.update_analysis_and_wait()`, `time.sleep()`, one big API call) stops only when that
  call returns. Write long jobs as bounded loops over small steps (and print progress), not as one
  huge native call. A script that catches `BaseException` and carries on can't be cancelled.
  Whatever the script changed before the cancel stays changed (no rollback).

Endpoints: `GET /run` → `{"running": <serialized run or null>, "queued": N, "parallel":
[<run>, ...], "parallel_queued": M, "max_parallel": K}` (`GET /lock` includes the same keys);
each run status has `session`, `name`, `label`, `main_thread`, `parallel`, `timeout`, `state`,
`running_for`, `cancel_requested`. `POST /cancel {"session", "run_id", "force"}` answers
`"cancelled"` (the first run hit, the serialized one if any) and `"cancelled_runs"` (all).
`POST /run` takes `"timeout"` (seconds), `"run_id"` and `"parallel"`; it answers 400 for
parallel + main_thread/view and 409 for view/main_thread without the lock. Cancellation needs
this plugin version loaded: restart Binary Ninja after updating.

## Restarting Binary Ninja

    bnrestart [--force] [--save-new | --discard] [--no-reopen] [--wait SECONDS]

Loads changed plugin code by restarting the app. Refuses (exit 3) while another session holds
the usage lock or a script is running or queued in either lane (`--force` overrides). It takes
the usage lock itself (as `$CLAUDE_CODE_SESSION_ID`, else `bnrestart-<pid>`) to list and save the
open views, since only the lock holder sees them; the restart clears the lock. Unsaved changes — note that analysis
alone marks a view modified:
- views that already have a `.bndb` are saved into it;
- other modified views make it refuse, unless `--save-new` (create `<file>.bndb` next to the
  binary) or `--discard` (quit without saving: SIGTERM, then SIGKILL after 15 s — the Qt UI
  ignores AppleScript's `saving no` and would block on its own save prompt).

Otherwise it quits via AppleScript, relaunches with the previously open files (their `.bndb` where
one exists; `--no-reopen` skips that) and waits until the bridge answers. Exit 0 ok, 1 did not
quit / did not come back, 2 bridge unreachable, 3 refused. It restarts the GUI for every session
sharing it, so coordinate.

The app is relaunched in the background (`open -g`) and Binary Ninja restores its own window
geometry. Which macOS Space (desktop) it opens on is up to macOS — new launches go to the active
Space. To keep it on its own desktop, assign it once: Dock → right-click Binary Ninja → Options →
Assign To → This Desktop.

## Usage lock (coordinating sessions)

Several sessions share one Binary Ninja. The plugin keeps one usage lock for all of them:
**no lock = free to use, a lock = one session is busy.** It is coordination, not security: the
holder is identified by its session id (Claude Code's `CLAUDE_CODE_SESSION_ID`), which anyone
could claim. A lock expires after its TTL (default 5 minutes, at most 240) unless renewed; the
holder's own script runs and MCP tool calls renew it. Lock changes appear in Binary Ninja's
log; **Plugins > Script Bridge > Release Lock** (enabled only while a lock is held) clears it by
hand, and a Binary Ninja restart clears it too.

From a shell (`--session` defaults to `$CLAUDE_CODE_SESSION_ID`, `--name` to `$BN_SESSION_NAME`):

```sh
bnrun --status                                # "free" (exit 0) or "locked by ..." (exit 3)
bnrun --lock --purpose "typing ctl.bin"       # take or renew (exit 3 if another session has it)
bnrun -e '...'                                # other sessions' scripts get exit 3 meanwhile
bnrun --unlock                                # release when done
bnrun --unlock --force                        # clear another session's stale lock
```

### MCP proxy

The plugin also runs a lock-aware MCP proxy on 127.0.0.1:24643 (`scriptbridge.mcpProxyPort`)
in front of Binary Ninja's own MCP server. MCP clients connect to the proxy. So that nothing
bypasses it, the plugin moves Binary Ninja's server to a random free port with a random bearer
token, both known only to the proxy: it sets `ui.mcp.port` and `ui.mcp.token` and restarts the
server with Binary Ninja's own **Plugins > MCP > Stop/Start Server** commands. (The settings
alone do nothing at runtime; the server applies them only when it (re)starts.) Every start of
the bridge picks new values; the restart drops open MCP connections once, and clients
reconnect. **Stop** puts the server back on 24642 without a token. If Binary Ninja quits with
the bridge running, the random port and token stay in the settings; the next start replaces
them. Without the plugin, reset `ui.mcp.port` to 24642 and clear `ui.mcp.token` (Settings,
search "MCP"). The proxy:

- forwards everything to Binary Ninja's MCP server (replies and streams unchanged);
- adds two tools to `tools/list`, answered by the proxy and never blocked:
  - `bn_owner_get`: who holds the lock (`{locked: false}` when free);
  - `bn_owner_set`: take or renew the lock for the caller (`name`, `purpose`, `ttl_minutes`);
    `release: true` gives it back; `release: true, force: true` force-unlocks another
    session's lock;
- while another session holds the lock, answers every other `tools/call` itself with an error
  result naming the holder and purpose; the call never reaches Binary Ninja.

The caller's identity is the `X-BN-Session` header, else the `MCP-Session-Id` that Binary
Ninja assigned to the connection. Claude Code sends the header through a `headersHelper`
(`mcp-session-header`), so a session's MCP calls and its `bnrun` scripts count as the same
holder. Claude Code doesn't reliably pass `CLAUDE_CODE_SESSION_ID` to the helper, so it falls
back to walking up its parent processes to the Claude Code process and reading
`~/.claude/sessions/<pid>.json` (an undocumented Claude Code file with `sessionId` and `name`;
the name is also used as `X-BN-Session-Name` unless `$BN_SESSION_NAME` is set). Each run logs
its source to `~/.cache/bn-mcp-session-header.log`; `source=none` means the identity fell back
to the MCP connection id, and the session's own MCP calls then don't count as its lock.
Claude Code config (local scope shown; `-s user` for all projects):

```sh
claude mcp remove binaryninja -s local
claude mcp add-json -s local binaryninja '{"type": "http", "url": "http://127.0.0.1:24643/mcp",
  "headersHelper": "/Users/patrick/dev/bn-script-bridge/mcp-session-header"}'
```

The helper runs on every connect and reconnect, so changing the lock never needs a restart.
If the proxy isn't running (bridge stopped), MCP calls fail to connect: start the bridge.

## Security

Anyone who can reach the port with the token can run arbitrary code as you, inside Binary
Ninja. The bridge is off unless started; it binds 127.0.0.1 only; every start generates a new
32-byte token, written with the port to `~/Library/Application Support/Binary Ninja/script_bridge.json`
(mode 0600) and removed on Stop. Requests must carry `Authorization: Bearer <token>`, a
`Host` of `127.0.0.1`/`localhost` (against DNS rebinding) and no `Origin` header (browsers
send one on cross-origin POSTs). Other local processes running as your user can read the
token file, as they could read your files anyway.

## Tests

`python3 -m unittest discover -s tests -p 'test_*.py'` runs `tests/test_bridge.py` against `bridge_server.py` (bridge, lock, cancellation and timeouts including a fake main-thread
runner, the parallel lane and its slot limit, the UI-view rules, MCP proxy against a fake
upstream), `bnrun` (including Ctrl-C/SIGTERM) and
`mcp-session-header` with a fake namespace and a fake home directory. The plugin glue
(`__init__.py`: UI view lookup, settings, menu commands, moving and restarting Binary Ninja's
MCP server) only runs inside Binary Ninja and is not covered.
