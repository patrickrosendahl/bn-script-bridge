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
bnrun -e 'print(len(list(bv.functions)))'
bnrun --view cx20707.ko script.py
echo 'result = [f.name for f in bv.functions][:10]' | bnrun
```

The script sees:

- `bv`: the open view whose filename contains `--view`, else the view active in the UI, else
  the only open view (not the MCP server's "active" view, which is separate state);
- `bvs`: all open views; `binaryninja` / `bn`: the API module.

`print()` output is returned, and so is `result` if the script sets it (JSON, else `repr`).
Scripts run one at a time on a worker thread; `--main-thread` runs on the UI thread (needed
for UI objects; it blocks the UI while it runs); later scripts queue behind the running one.
Exit status: 0 ok, 1 script error or cancelled, 2 bridge unreachable or refused, 3 Binary Ninja
locked by someone else (see below), 130 interrupted with Ctrl-C.

## Cancelling scripts

```sh
bnrun --status                    # lock, plus "running script '<first line>' for 42s (session ...)"
bnrun --timeout 60 script.py      # the bridge cancels the script after 60 s of running
bnrun --cancel                    # cancel the running script (this session's, or as lock holder)
bnrun --cancel --force            # cancel another session's script
```

- **Ctrl-C** (or SIGTERM, e.g. a tool timeout killing `bnrun`) while `bnrun` waits cancels that
  script inside Binary Ninja too, then exits with 130. Each run carries a random run id, so this
  works without a session id and only ever hits its own script (also while still queued).
- `--cancel` is allowed for the session that started the script (`--session`) or the usage-lock
  holder; `--force` for anyone. Nothing running: exit 1 ("no script is running"); not allowed:
  exit 3.
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

Endpoints: `GET /run` (running script and queue length; `GET /lock` includes it too) and
`POST /cancel {"session", "run_id", "force"}`; `POST /run` takes `"timeout"` (seconds) and
`"run_id"`. Cancellation needs this plugin version loaded: restart Binary Ninja after updating.

## Restarting Binary Ninja

    bnrestart [--force] [--no-save] [--no-reopen] [--wait SECONDS]

Loads changed plugin code by restarting the app. Refuses (exit 3) while another session holds
the usage lock or a script is running (`--force` overrides). Views with unsaved changes are
saved first — into their `.bndb`, or a new `<file>.bndb` next to the binary (`--no-save` refuses
instead) — so the quit never blocks on a save dialog. It quits via AppleScript, relaunches with
the previously open files (their `.bndb` where one exists; `--no-reopen` skips that) and waits
until the bridge answers. Exit 0 ok, 1 did not quit / did not come back, 2 bridge unreachable,
3 refused. It restarts the GUI for every session sharing it, so coordinate.

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
runner, MCP proxy against a fake upstream), `bnrun` (including Ctrl-C/SIGTERM) and
`mcp-session-header` with a fake namespace and a fake home directory. The plugin glue
(`__init__.py`: UI view lookup, settings, menu commands, moving and restarting Binary Ninja's
MCP server) only runs inside Binary Ninja and is not covered.
