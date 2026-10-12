# bn-script-bridge

Run Python scripts inside the running Binary Ninja app from the command line. The Personal
licence refuses the API outside the app (`binaryninja.load()` from a plain `python3` fails
with "License is not valid"). This plugin listens on 127.0.0.1 and runs the scripts that
`bnrun` sends, with the full Python API. It also keeps a usage lock so several sessions can
share one Binary Ninja, and optionally puts a lock-aware proxy in front of Binary Ninja's
own MCP server for MCP clients (see [MCP proxy](#mcp-proxy)).

Contents: `__init__.py` (plugin glue), `bridge_server.py` (bridge, lock, MCP proxy), `bnrun`
(run scripts, manage the lock), `scripts/` (bundled scripts: annotate, info, decompile),
`bnannotate` (shorthand for renames and comments), `bnrestart` (restart Binary Ninja),
`mcp-session-header` (`headersHelper` for MCP clients), `skills/binaryninja-bridge` (Claude
Code skill).

## Install

```sh
ln -s /Users/patrick/rev/bn-script-bridge ~/Library/Application\ Support/Binary\ Ninja/plugins/bn-script-bridge
```

Then restart Binary Ninja. Plugins load only at startup; if several sessions share the
instance, coordinate the restart (see [Restarting Binary Ninja](#restarting-binary-ninja)).
`bnrun` and `bnrestart` can be run from here or symlinked onto `PATH`.

For Claude Code, install the skill (user scope shown; drop it into a project's
`.claude/skills/` instead for a single project):

```sh
ln -s /Users/patrick/rev/bn-script-bridge/skills/binaryninja-bridge ~/.claude/skills/binaryninja-bridge
```

The skill needs no MCP registration -- everything runs through `bnrun`. Pointing an MCP
client at the proxy is optional and covered in [MCP proxy](#mcp-proxy).

## Use

1. In Binary Ninja: **Plugins > Script Bridge > Start** (no open binary needed), or enable
   the setting `scriptbridge.autostart` (Settings, search "script bridge", or
   `Settings().set_bool("scriptbridge.autostart", True)` in the Python console). The log
   shows the port. Start is greyed out while the bridge runs, Stop while it doesn't.
2. From a shell:

```sh
bnrun --lock --purpose "listing functions"        # UI views need the usage lock
bnrun -e 'print(len(list(bv.functions)))'
bnrun --view firmware.ko script.py
echo 'result = [f.name for f in bv.functions][:10]' | bnrun
bnrun --unlock

bnrun --parallel --timeout 600 check.py           # own throwaway views, no lock
bnrun --bndb synthv-studio.x86_64.bndb dump.py -- 0x401000   # cached headless view, no lock
```

The script sees:

- `binaryninja` / `bn`: the API module (always);
- `bv`: the open view whose filename contains `--view`, else the view active in the UI, else
  the only open view (not the MCP server's "active" view, which is separate state); for
  `--bndb`, the cached headless view of that file instead;
- `bvs`: all views open in the UI;
- `argv` / `env`: the arguments after `--` and the `--env K=V` variables (all runs; see
  [Arguments and environment](#arguments-and-environment)).

`print()` output is returned, and so is `result` if the script sets it (JSON, else `repr`).

SCRIPT ending in `.py` is a file (`check.py`, `/abs/check.py`); anything else names a
[bundled script](#bundled-scripts) (`annotate`). No SCRIPT (or `-`) reads stdin.

Exit status: 0 ok, 1 script error or cancelled, 2 bridge unreachable or refused (including
the UI-view rules below), 3 Binary Ninja locked by another session, 130 interrupted with
Ctrl-C.

`--max-out N` keeps one-off queries readable: when stdout+result exceed N characters, bnrun
prints the first N and writes the full output to `~/.cache/bn-script-bridge/bnrun-<ts>.txt`,
with the path in the last line. Errors (stderr) are never truncated.

## Usage lock

Several sessions share one Binary Ninja. The plugin keeps one usage lock for all of them:
**no lock = free to use, a lock = one session is busy.** It is coordination, not security: the
holder is identified by its session id (Claude Code's `CLAUDE_CODE_SESSION_ID`), which anyone
could claim. A lock expires after its TTL (default 5 minutes, at most 240) unless renewed; the
holder's own script runs and MCP tool calls renew it. Lock changes appear in Binary Ninja's
log; **Plugins > Script Bridge > Release Lock** (enabled only while a lock is held) clears it
by hand, and a Binary Ninja restart clears it too.

`--session` defaults to `$CLAUDE_CODE_SESSION_ID`, `--name` to `$BN_SESSION_NAME`:

```sh
bnrun --status                                # "free" (exit 0) or "locked by ..." (exit 3)
bnrun --lock --purpose "typing ctl.bin"       # take or renew (exit 3 if another session has it)
bnrun -e '...'                                # other sessions' scripts get exit 3 meanwhile
bnrun --unlock                                # release when done
bnrun --unlock --force                        # clear another session's stale lock
```

The lock applies to both script lanes: while another session holds it, your scripts
(parallel included) are refused with exit 3.

### UI views need the lock

`bv` and `bvs` are provided only to a **serialized** script whose session holds the lock.
Otherwise:

- `bv` and `bvs` are placeholders that raise `UIViewsUnavailable` (a `RuntimeError`) with the
  reason on any use (attribute access, `len`, iteration, indexing, truth test); `repr` works.
  They are not `None`/`[]` on purpose: an empty `bvs` would silently make a script do nothing.
- `--view` and `--main-thread` are refused up front (exit 2, "take the usage lock first: bnrun
  --lock --purpose ..."). If the lock is lost while the script waits in the queue, the script
  is refused when it would start.

Scripts that only create their own views through the API (`BinaryViewType[...].create(
BinaryView.open(path))`, `BinaryView.open`, `binaryninja.load`, then `close()`) need no lock.

**Limitation:** this is enforced in the script namespace and otherwise relies on convention. A
script can still reach UI views by other paths (`binaryninjaui.UIContext`, ...); don't.

## Two lanes: serialized (default) and `--parallel`

- **Serialized** (default): one script at a time on a worker thread; later ones queue behind
  it. `--main-thread` runs it on the UI thread instead (needed for UI objects; blocks the UI
  while it runs; needs the lock).
- **`--parallel`**: runs on its own worker thread without waiting for the serialized lane, at
  most `scriptbridge.maxParallel` at a time (default 4; Settings, search "script bridge";
  changes apply at once); more wait for a slot. Binary Ninja's analysis is native and
  multithreaded, and native calls release the GIL, so several `update_analysis_and_wait()` on
  separate views do run concurrently. `--parallel` with `--main-thread` or `--view` is refused
  (exit 2). Parallel scripts get no `bv`/`bvs`, even for the lock holder, so a holder can't
  queue UI work behind itself.

**Rule of thumb:** UI views or shared state (the open tabs, the MCP active view, databases,
global registrations) → serialized + usage lock. Only your own throwaway views → `--parallel`,
no lock needed. Parallel scripts must not modify UI-opened views, switch the MCP active view or
save databases; only this convention stops them.

## Arguments and environment

```sh
bnrun script.py -- arg1 arg2 --flag          # script sees argv == ["script.py", "arg1", ...]
bnrun script.py -- max 50                    # e.g. result = argv[2] inside the script
bnrun -e 'import os; result = os.environ["K"]' --env K=V
```

Everything after a standalone `--` is the script's argv (`argv[0]` is the script filename, or
`"<bnrun>"` for `-e`/stdin); `--env K=V` (repeatable) forwards environment variables. Both are
given to every script as the bare names `argv` and `env`, and for a **serialized** run they are
also swapped into `sys.argv` / merged into `os.environ` for the script's duration, then
restored — so plain `import sys; sys.argv` and `os.environ["K"]` work. Parallel runs overlap, so
they get the bare names only (a global swap would corrupt a neighbouring run's view of them).

Corner: the restore puts back the values as they were before the script; if Binary Ninja itself
changes a forwarded key while the script runs (it doesn't), that change is overwritten.

## The `--bndb` cache: open a database once

One-shot scripts that call `binaryninja.load()` on the raw binary pay the full analysis every
time (minutes on big binaries). `--bndb` instead opens the file headlessly once, caches the
view and hands it to each script as `bv` — a `.bndb` opens instantly and carries renames; a raw
binary is analysed once (the first run) and `--bndb-save` materializes `<file>.bndb` beside it.

```sh
bnrun --bndb synthv-studio.x86_64.bndb -e 'result = len(list(bv.functions))'
bnrun --bndb synthv-studio.x86_64.bndb script.py -- extra-arg
bnrun --bndb-list                                 # what's cached (also in --status)
bnrun --bndb-save                                 # write renames etc. back into the .bndb
bnrun --bndb-close                                # free the memory (tens of MB per view)
```

`bnrun` sends the `--bndb` path (and a `--bndb-save`/`--bndb-close` PATH) as an absolute
path, so relative paths mean the current directory, not Binary Ninja's.

Rules: `--bndb` runs in the serialized lane (cached views are shared, so runs using them must
not overlap) but needs **no usage lock** — it touches no UI view — and can't be combined with
`--view`, `--main-thread` or `--parallel`. `bvs` stays a placeholder that raises with the
reason. The first open of a big `.bndb` takes a while (30k functions ≈ 85 s) — later runs are
instant; `bnrun` uses a 10-minute HTTP timeout for `--bndb` runs. While another session holds
the lock, `--bndb` runs are refused like any other (exit 3).

Management is refused (409) while a serialized script runs; `--bndb-save` of a raw binary
writes a whole `.bndb` (300 s timeout). **Stop** (or quitting Binary Ninja) closes the cache
without saving; `bnrestart` saves it first. Note the `nargs` trap: `bnrun --bndb-save foo.py`
takes `foo.py` as the PATH — management commands take no script.

## Bundled scripts

`scripts/` holds scripts that every project needs. `bnrun` runs them by name: a SCRIPT not
ending in `.py` is looked up here; one ending in `.py` is a file. Each documents its
arguments at the top.

```sh
DB=/abs/path/synthv-studio.x86_64.bndb
bnrun --bndb $DB annotate -- R:0x10014fed0:nhv_proc D:0x1002a0000:voice_table \
                             "C:0x10014fed0:called once per voice"
bnrun --bndb-save $DB                                   # the edits live in RAM until saved
bnrun --bndb $DB info -- F:0x10014fed0 S:maxFunctionSize  # function info, settings
bnrun --bndb $DB decompile -- 0x10014fed0 > nhv_proc.hlil # un-skip if needed, print HLIL
```

- **annotate**: `R:ADDR:NAME` renames the function starting at ADDR, `D:ADDR:NAME` defines or
  renames the data symbol at ADDR, `C:ADDR:TEXT` sets the comment at ADDR (a function comment
  inside a function, a view comment elsewhere; it replaces the old one, which is printed).
  ADDR is hex, `0x` optional; TEXT may contain `:`. Every directive is checked first (syntax,
  a function starting at ADDR for `R:`, a valid address otherwise); if one is bad, nothing is
  applied and the run exits 1.
- **info**: read-only. `F:ADDR` shows the function at (or containing) ADDR with its analysis
  skip state, `S:SUBSTR` lists the settings whose id contains SUBSTR with their values.
- **decompile** `ADDR [OUT]`: Binary Ninja skips huge functions (`ExceedFunctionSize`, ...)
  and their HLIL is a stub; this clears the skip, waits for analysis and prints the HLIL, or
  writes it to OUT. OUT must be absolute: scripts run inside Binary Ninja, whose working
  directory isn't yours. Redirecting stdout avoids that.

The scripts work on any view, not only `--bndb` (`--view` with the usage lock, too). For
annotations, `bnannotate` saves typing: arguments shaped like `R:`/`D:`/`C:` (and everything
after `--`) are directives, all others go to `bnrun`; `--save` runs `bnrun --bndb-save` when
the edits succeeded.

```sh
bnannotate --bndb $DB --save R:0x10014fed0:nhv_proc "C:0x10014fed0:called once per voice"
```

Writing your own: print errors (the bridge returns `print()` output, not `sys.stderr`) and end
a failed run with `sys.exit("message")`. The bridge reports that as the error, with exit
status 1 and no traceback; `sys.exit()` / `sys.exit(0)` count as success.

## Cancelling scripts

```sh
bnrun --status                    # lock, one "running script '<first line>' for 42s (...)" line
                                  # per running script (", parallel" marks that lane) and
                                  # "queued: N serialized, M parallel"
bnrun --timeout 60 script.py      # the bridge cancels the script after 60 s of running
bnrun --cancel                    # cancel this session's running scripts (both lanes)
bnrun --cancel --force            # cancel all running scripts (also other sessions')
```

- **Ctrl-C** (or SIGTERM, e.g. a tool timeout killing `bnrun`) while `bnrun` waits cancels that
  script inside Binary Ninja too, then exits with 130. Each run carries a random run id, so this
  works without a session id and only ever hits its own script (also while still queued).
- `--cancel` cancels the calling session's running scripts. If it has none, the lock holder (or
  anyone with `--force`) cancels all running scripts. Nothing running: exit 1; not allowed:
  exit 3. Each script runs in its own thread, so a cancel or `--timeout` only hits its target.
- A cancelled script's reply has `"error": "cancelled"`, `"cancelled_by"` (session, lock holder,
  force, its client, or `timeout after Ns`) and the `print()` output captured so far.
- How: the bridge raises `ScriptCancelled` (a `BaseException`, so `except Exception` doesn't
  swallow it) in the script's thread with `PyThreadState_SetAsyncExc`; with `--main-thread`
  that is the UI thread. A cancel that would land after the script finished is cleared, so it
  never reaches other code on that thread.
- **Limitation:** Python delivers it only between bytecodes. A script blocked in a long native
  call (`bv.update_analysis_and_wait()`, `time.sleep()`, one big API call) stops only when that
  call returns. Write long jobs as bounded loops over small steps (and print progress). A
  script that catches `BaseException` and carries on can't be cancelled. Changes made before
  the cancel stay (no rollback).

## Restarting Binary Ninja

```sh
bnrestart [--force] [--save-new | --discard] [--no-reopen] [--wait SECONDS]
```

Loads changed plugin code by restarting the app. It restarts the GUI for every session sharing
it, so coordinate. Refuses (exit 3) while another session holds the usage lock or a script is
running or queued in either lane (`--force` overrides). It takes the lock itself (as
`$CLAUDE_CODE_SESSION_ID`, else `bnrestart-<pid>`) to list and save the open views, since only
the lock holder sees them; the restart clears the lock.

Unsaved changes (analysis alone marks a view modified):

- views that already have a `.bndb` are saved into it;
- other modified views make it refuse, unless `--save-new` (create `<file>.bndb` next to the
  binary) or `--discard` (quit without saving: SIGTERM, then SIGKILL after 15 s; the Qt UI
  ignores AppleScript's `saving no` and would block on its own save prompt).

Otherwise it quits via AppleScript, relaunches with the previously open files (their `.bndb`
where one exists; `--no-reopen` skips that) and waits until the bridge answers. Exit 0 ok,
1 did not quit / did not come back, 2 bridge unreachable, 3 refused.

The app is relaunched in the background (`open -g`) and restores its own window geometry. New
launches go to the active macOS Space; to keep Binary Ninja on its own desktop, assign it once:
Dock → right-click Binary Ninja → Options → Assign To → This Desktop.

## MCP proxy

The plugin runs a lock-aware MCP proxy on 127.0.0.1:24643 (`scriptbridge.mcpProxyPort`) in
front of Binary Ninja's own MCP server; MCP clients connect to the proxy. This is optional:
the Claude Code skill does not use it (no `bn_*` MCP tools exist any more -- everything goes
through `bnrun`); it exists for other MCP clients. It needs Binary Ninja's own MCP server
running: enable `ui.mcp.enabled` (Settings, search "MCP") once, or every MCP call answers 502.
To point Claude Code at it (local scope shown; `-s user` for all projects):

```sh
claude mcp remove binaryninja -s local
claude mcp add-json -s local binaryninja '{"type": "http", "url": "http://127.0.0.1:24643/mcp",
  "headersHelper": "/Users/patrick/rev/bn-script-bridge/mcp-session-header"}'
```

The proxy:

- forwards everything to Binary Ninja's MCP server (replies and streams unchanged);
- adds two tools to `tools/list`, answered by the proxy and never blocked:
  - `bn_owner_get`: who holds the lock (`{locked: false}` when free);
  - `bn_owner_set`: take or renew the lock for the caller (`name`, `purpose`, `ttl_minutes`);
    `release: true` gives it back; `release: true, force: true` force-unlocks another
    session's lock;
- filters `tools/list` through the `scriptbridge.mcpTools` allowlist (see below) and answers a
  call of a filtered-out tool locally with a `tool ... is not in the scriptbridge.mcpTools
  allowlist` error; the call never reaches Binary Ninja;
- while another session holds the lock, answers every other `tools/call` itself with an error
  naming the holder and purpose; the call never reaches Binary Ninja.

### Tool allowlist (`scriptbridge.mcpTools`)

Binary Ninja's MCP server advertises 70+ tools; that many definitions bloat an MCP client's
context and degrade its tool choice. The proxy filters `tools/list` down to the patterns in
`scriptbridge.mcpTools` (Settings, search "script bridge"): comma-separated `fnmatch` globs,
read live. The default keeps the interactive-RE set (decompile, disassembly, IL, xrefs,
function/symbol/string lists and searches, memory read, comments, view info); empty = every
upstream tool passes. `bn_owner_get`/`bn_owner_set` always pass. Everything else stays
reachable through `bnrun` scripts, which is the more capable path anyway.

```sh
# examples (Settings > Script Bridge > "MCP tools advertised to MCP clients")
bn_function_*,bn_memory_read,bn_symbol_rename
bn_*                                     # everything (not recommended)
```

So that nothing bypasses the proxy, the plugin moves Binary Ninja's server to a random free
port with a random bearer token, both known only to the proxy: it sets `ui.mcp.port` and
`ui.mcp.token` and restarts the server with **Plugins > MCP > Stop/Start Server** (the server
applies the settings only when it restarts). Every bridge start picks new values; open MCP
connections drop once and clients reconnect. **Stop** puts the server back on 24642 without a
token. If Binary Ninja quits with the bridge running, the random port and token stay in the
settings until the next start replaces them; to use Binary Ninja without the plugin, reset
`ui.mcp.port` to 24642 and clear `ui.mcp.token` (Settings, search "MCP"). If the proxy isn't
running (bridge stopped), MCP calls fail to connect: start the bridge.

### Session identity

The caller's identity is the `X-BN-Session` header, else the `MCP-Session-Id` that Binary
Ninja assigned to the connection. Claude Code sends the header through the `headersHelper`
`mcp-session-header` (see [MCP proxy](#mcp-proxy)), so a session's MCP calls and its `bnrun`
scripts count as the same holder. The helper runs on every connect and reconnect, so changing
the lock never needs a restart.

Claude Code doesn't reliably pass `CLAUDE_CODE_SESSION_ID` to the helper, so it falls back to
walking up its parent processes to the Claude Code process and reading
`~/.claude/sessions/<pid>.json` (an undocumented Claude Code file with `sessionId` and `name`;
the name is also sent as `X-BN-Session-Name` unless `$BN_SESSION_NAME` is set). Each run logs
its source to `~/.cache/bn-mcp-session-header.log`; `source=none` means the identity fell back
to the MCP connection id, and the session's own MCP calls then don't count as its lock.

## HTTP API

All endpoints need the token (see [Security](#security)).

- `GET /ping`.
- `GET /run` → `{"running": <serialized run or null>, "queued": N, "parallel": [<run>, ...],
  "parallel_queued": M, "max_parallel": K, "bndb": [{path, name}, ...]}`; each run has
  `session`, `name`, `label`, `main_thread`, `parallel`, `timeout`, `bndb`, `state`,
  `running_for`, `cancel_requested`.
- `GET /lock`: the lock, plus the same keys as `GET /run`.
- `POST /run`: runs a script (`sys.exit()` ends it: no status or 0 is success, a string is
  the `error`, another value `"script exited with status N"`); takes `"timeout"` (seconds), `"run_id"`, `"parallel"`,
  `"bndb"` (path), `"args"` (list of str) and `"env"` (str→str). Answers 400 for parallel
  with main_thread/view, bndb with view/main_thread/parallel, a bad `args`/`env` type or a
  missing `--bndb` file; 409 for view/main_thread without the lock.
- `POST /lock`, `POST /unlock`: take/renew and release the lock.
- `POST /cancel {"session", "run_id", "force"}` → `"cancelled"` (the first run hit, the
  serialized one if any) and `"cancelled_runs"` (all).
- `POST /bndb {"action": "save"|"close", "path"?}` → `{"ok", "action", "results": [{path,
  ok, error?}]}` for the cached views (all when `path` is omitted); 404 when the path isn't
  cached, 409 while a serialized script runs, 423 while another session holds the lock.

## Security

Anyone who can reach the port with the token can run arbitrary code as you, inside Binary
Ninja. The bridge is off unless started; it binds 127.0.0.1 only; every start generates a new
32-byte token, written with the port to
`~/Library/Application Support/Binary Ninja/script_bridge.json` (mode 0600) and removed on
Stop. Requests must carry `Authorization: Bearer <token>`, a `Host` of `127.0.0.1`/`localhost`
(against DNS rebinding) and no `Origin` header (browsers send one on cross-origin POSTs). Other
local processes running as your user can read the token file, as they could read your files
anyway.

## Tests

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```

`tests/test_bridge.py` covers `bridge_server.py` (bridge, lock, cancellation and timeouts
including a fake main-thread runner, the parallel lane and its slot limit, the UI-view rules,
args/env forwarding, the `--bndb` cache with fake hooks, the MCP proxy against a fake upstream
including the tool allowlist), `bnrun` (including Ctrl-C/SIGTERM and `--max-out`), the
bundled scripts and `bnannotate` against a fake BinaryView, and `mcp-session-header`, with a fake namespace and a fake home directory. The plugin glue
(`__init__.py`: UI view lookup, settings, menu commands, moving and restarting Binary Ninja's
MCP server, the real `--bndb` open/save/close) only runs inside Binary Ninja and is not
covered.
