"""Binary Ninja plugin: run Python scripts in the running app from a local client (bnrun).

Off by default. Start it with Plugins > Script Bridge > Start (works without an open binary),
or set the setting "scriptbridge.autostart". Each start picks a free 127.0.0.1 port,
generates a new token and writes both to <Binary Ninja user directory>/script_bridge.json
(mode 0600); Stop removes the file. It also starts the lock-aware MCP proxy on
127.0.0.1:<scriptbridge.mcpProxyPort> in front of Binary Ninja's own MCP server, which it moves
to a random port with a random token only the proxy knows (see _secure_mcp_server); Stop puts
that server back on 24642 without a token. Both share one usage lock. See README.md.
"""

import json
import os
import socket
import threading

import binaryninja
from binaryninja import PluginCommand, Settings, log_info, log_warn
from binaryninja.mainthread import execute_on_main_thread, execute_on_main_thread_and_wait

from . import bridge_server

CONFIG_NAME = "script_bridge.json"
SPOOL_NAME = "script_bridge_spool"     # queued/running jobs, so a crash can't lose them silently
_bridge = None
_proxy = None


def _config_path() -> str:
    return os.path.join(binaryninja.user_directory(), CONFIG_NAME)


def _open_views():
    """(views in tab order, active view) from the UI. Must run on the main thread."""
    from binaryninjaui import UIContext
    views, active = [], None
    for ctx in UIContext.allContexts():
        for tab in ctx.getTabs():
            frame = ctx.getViewFrameForTab(tab)
            bv = frame.getCurrentBinaryView() if frame else None
            if bv is not None and all(bv is not v for v in views):
                views.append(bv)
    ctx = UIContext.activeContext()
    frame = ctx.getCurrentViewFrame() if ctx else None
    if frame:
        active = frame.getCurrentBinaryView()
    return views, active


def _namespace(view_filter):
    """The UI views for a script allowed to use them (serialized, session holds the lock)."""
    box = {}
    execute_on_main_thread_and_wait(lambda: box.update(zip(("views", "active"), _open_views())))
    views, active = box["views"], box["active"]
    if view_filter:
        matches = [v for v in views if view_filter in (v.file.filename or "")]
        if len(matches) != 1:
            raise LookupError("view filter %r matches %d open views: %s" % (
                view_filter, len(matches), [v.file.filename for v in views]))
        bv = matches[0]
    else:
        # The UI may report no active view (e.g. focus elsewhere); one open view is unambiguous.
        bv = active if active is not None or len(views) != 1 else views[0]
    return {"bv": bv, "bvs": views, "binaryninja": binaryninja, "bn": binaryninja}


def _same_file(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.realpath(a) == os.path.realpath(b)


def _ui_open_files():
    """Every path the UI holds open: each file context's filename and, for each tab's view,
    its filename and original binary. Must run on the main thread."""
    from binaryninjaui import FileContext
    names = []
    for fc in FileContext.getOpenFileContexts() or []:
        try:
            names.append(fc.getFilename())
        except Exception:
            pass
    for bv in _open_views()[0]:
        for attr in ("filename", "original_filename"):
            try:
                names.append(getattr(bv.file, attr))
            except Exception:
                pass
    return [n for n in names if n]


def _open_elsewhere(path):
    """A message naming where `path` is already open in Binary Ninja (a cached --bndb view,
    or an MCP/UI file context), or None when it isn't. Opening a .bndb twice gives it two
    database handles; both look fully writable, but then saves fail on BOTH lanes until
    one of the two closes without saving -- edits on that side are lost, and a headless open
    of a database a UI tab holds can deadlock inside Binary Ninja's core."""
    abs_path = os.path.abspath(path)
    if _bridge is not None and abs_path in _bridge.bndb_views:
        return "%s is already open as a bnrun --bndb cached view" % path
    box = {}

    def look():
        try:
            box["names"] = _ui_open_files()
        except Exception as e:      # headless / UI not up yet: nothing to compare against
            box["error"] = e
    try:
        execute_on_main_thread_and_wait(look)   # binaryninjaui objects belong to the UI thread
    except Exception as e:
        box["error"] = e
    if "error" in box:
        log_warn("Script bridge: couldn't check the UI for %s being open (%r); opening anyway"
                 % (path, box["error"]))
    for name in box.get("names") or []:
        if _same_file(name, abs_path):
            return "%s is already open in Binary Ninja (a UI tab or MCP item)" % path
    return None


def _mcp_open_guard(path):
    """The proxy's open_guard (see McpProxy.__init__): refuse bn_open_item_open for files
    that are already open, keeping one handle per .bndb."""
    refusal = _open_elsewhere(path)
    if refusal is None:
        return None
    return (refusal + "; opening it twice would deadlock saves on both lanes. Reuse the "
            "open view (bn_open_item_list, or the cached bnrun --bndb view), or close it "
            "first (bnrun --bndb-close, or the file's tab/MCP item) -- without saving.")


def _bndb_open(path, progress=None):
    """Open a headless cached view for a --bndb run (a .bndb opens its database without
    updating the analysis; a raw binary is analysed once). Runs on a thread of the bridge,
    like a --parallel script's own view. Refuses (raises, naming where) when the file is already open elsewhere, for the same
    one-handle-per-.bndb rule as the MCP proxy's open_guard."""
    busy = _open_elsewhere(path)
    if busy:
        log_warn("bnrun --bndb refused: " + busy)
        raise RuntimeError(busy + "; a second handle on it can deadlock Binary Ninja. Use "
                           "that view (bnrun --view, with the lock) or close it first")
    if path.lower().endswith(".bndb"):
        # Never update the analysis when opening a database: what it saved is there at once,
        # and a re-analysis of a big one (a kernelcache) can take 20+ minutes. Scripts that
        # want fresh analysis call bv.update_analysis_and_wait() themselves. progress (from
        # the bridge) reports the load and returns False to abort it (--timeout, --cancel).
        return binaryninja.load(path, update_analysis=False, progress_func=progress)
    return binaryninja.load(path)      # a raw binary: analysed once, load waits for it


def _bndb_save(bv):
    f = bv.file
    if f.has_database:
        return f.save_auto_snapshot()
    return bv.create_database(f.filename + ".bndb")   # raw binary: materialize a .bndb


def _bndb_close(bv):
    bv.file.close()


def _log_lock(event, status):
    log_info("Script bridge lock %s: %s" % (event, bridge_server.describe(status)))


def _locked() -> bool:
    return _bridge is not None and _bridge.lock.status().get("locked", False)


def release_lock(*_args):
    """Menu action for stale locks: release whoever holds the usage lock."""
    if _bridge is not None:
        _bridge.lock.release(force=True)


DEFAULT_MCP_PORT = 24642


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _restart_mcp_server() -> bool:
    """Stop and start Binary Ninja's GUI MCP server (its own Plugins > MCP commands), which
    applies ui.mcp.port and ui.mcp.token. Main thread only. False if the UI isn't up yet."""
    from binaryninjaui import UIActionHandler, UIContext
    contexts = UIContext.allContexts()
    ctx = UIContext.activeContext() or (contexts[0] if contexts else None)
    window = ctx.mainWindow() if ctx else None
    if window is None:
        return False
    handler = UIActionHandler.actionHandlerFromWidget(window)
    if handler is None:
        return False
    if handler.isValidAction("MCP\\Stop Server"):
        handler.executeAction("MCP\\Stop Server")
    if handler.isValidAction("MCP\\Start Server"):
        handler.executeAction("MCP\\Start Server")
    return handler.isValidAction("MCP\\Stop Server")     # i.e. running now


def _move_mcp_server(port: int, token: str, then=None, attempts: int = 60) -> None:
    """Point Binary Ninja's MCP server at port/token and restart it; retry until the UI is up.

    Settings changes alone do nothing at runtime (tested 2026-09-25); the server applies them
    only when (re)started. Restarting drops every open MCP connection; clients reconnect."""
    def attempt():
        settings = Settings()
        settings.set_integer("ui.mcp.port", port)
        settings.set_string("ui.mcp.token", token)
        if not settings.get_bool("ui.mcp.enabled"):
            log_warn("ui.mcp.enabled is off: Binary Ninja's MCP server isn't running")
            return
        if _restart_mcp_server():
            if then:
                then()
            return
        if attempts > 1:
            threading.Timer(1.0, lambda: _move_mcp_server(port, token, then, attempts - 1)).start()
        else:
            log_warn("Couldn't restart Binary Ninja's MCP server (no main window)")
    execute_on_main_thread(attempt)


def _secure_mcp_server() -> None:
    """Move Binary Ninja's MCP server to a random port with a random token, known only to the
    proxy, so MCP clients can't bypass the lock by connecting to it directly."""
    port, token = _free_port(), bridge_server.new_token()

    def switch_proxy():
        if _proxy is not None:
            _proxy.upstream_port, _proxy.upstream_token = port, token
            log_info("Binary Ninja MCP server moved to a private port; MCP clients use the "
                     "proxy on 127.0.0.1:%d/mcp" % _proxy.port)
    _move_mcp_server(port, token, switch_proxy)


def start(*_args):
    global _bridge, _proxy
    if _bridge is not None:
        log_warn("Script bridge already running on 127.0.0.1:%d" % _bridge.port)
        return
    token = bridge_server.new_token()
    lock = bridge_server.UsageLock(listener=_log_lock)
    _bridge = bridge_server.ScriptBridge(
        token, _namespace, execute_on_main_thread_and_wait, lock=lock,
        max_parallel=lambda: Settings().get_integer("scriptbridge.maxParallel"),
        base_namespace={"binaryninja": binaryninja, "bn": binaryninja},
        bndb_open=_bndb_open, bndb_save=_bndb_save, bndb_close=_bndb_close,
        spool_dir=os.path.join(binaryninja.user_directory(), SPOOL_NAME),
        bndb_modified=lambda bv: bv.file.modified)
    if _bridge.lost:
        log_warn("Script bridge: %d job(s) lost when Binary Ninja last stopped (never rerun on "
                 "their own): bnrun --jobs, then --requeue ID|all or --forget ID|all"
                 % len(_bridge.lost))
    try:
        settings = Settings()
        _proxy = bridge_server.McpProxy(lock, settings.get_integer("ui.mcp.port"),
                                        settings.get_integer("scriptbridge.mcpProxyPort"),
                                        upstream_token=settings.get_string("ui.mcp.token"),
                                        allowed_tools=lambda: Settings().get_string(
                                            "scriptbridge.mcpTools"),
                                        open_guard=_mcp_open_guard)
        _proxy.start()
        log_info("MCP proxy listening on 127.0.0.1:%d/mcp" % _proxy.port)
        _secure_mcp_server()
    except OSError as e:
        _proxy = None
        log_warn("MCP proxy not started: %s" % e)
    path = _config_path()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"port": _bridge.port, "token": token}, f)
    os.chmod(path, 0o600)
    _bridge.start()
    log_info("Script bridge listening on 127.0.0.1:%d (config %s)" % (_bridge.port, path))


def stop(*_args):
    global _bridge, _proxy
    if _bridge is None:
        return
    if _proxy is not None:
        _proxy.stop()
        _proxy = None
        # Without the proxy, give direct MCP access back on the default port.
        _move_mcp_server(DEFAULT_MCP_PORT, "")
    _bridge.stop()
    _bridge = None
    try:
        os.remove(_config_path())
    except FileNotFoundError:
        pass
    log_info("Script bridge stopped")


Settings().register_group("scriptbridge", "Script Bridge")
Settings().register_setting("scriptbridge.autostart", json.dumps({
    "title": "Start script bridge at launch",
    "type": "boolean",
    "default": False,
    "description": "Listen on 127.0.0.1 for token-authenticated scripts from bnrun "
                   "(bn-script-bridge). Anyone with the token can run code in Binary Ninja.",
    "ignore": ["SettingsProjectScope", "SettingsResourceScope"],
}))
Settings().register_setting("scriptbridge.maxParallel", json.dumps({
    "title": "Maximum parallel scripts",
    "type": "number",
    "default": bridge_server.DEFAULT_MAX_PARALLEL,
    "minValue": 1,
    "maxValue": 64,
    "description": "How many `bnrun --parallel` scripts (scripts that only work on their own "
                   "views) may run at once; more wait for a slot. Serialized scripts are not "
                   "counted. Read whenever a parallel script waits, so changes apply at once.",
    "ignore": ["SettingsProjectScope", "SettingsResourceScope"],
}))
Settings().register_setting("scriptbridge.mcpProxyPort", json.dumps({
    "title": "Lock-aware MCP proxy port",
    "type": "number",
    "default": 24643,
    "minValue": 1024,
    "maxValue": 65535,
    "description": "Port of the MCP proxy started with the script bridge. MCP clients connect "
                   "here instead of to Binary Ninja's MCP server (ui.mcp.port) so the usage "
                   "lock applies; it adds the tools bn_owner_get and bn_owner_set.",
    "ignore": ["SettingsProjectScope", "SettingsResourceScope"],
}))
Settings().register_setting("scriptbridge.mcpTools", json.dumps({
    "title": "MCP tools advertised to MCP clients",
    "type": "string",
    "default": bridge_server.DEFAULT_MCP_TOOLS,
    "description": "Comma-separated fnmatch patterns (e.g. bn_function_*,bn_memory_read) "
                   "naming the Binary Ninja MCP tools the proxy advertises and forwards; "
                   "everything else is hidden and refused locally. Empty: all tools pass "
                   "(plus bn_owner_get / bn_owner_set, which always pass). Read live. Use "
                   "fewer tools to keep MCP clients' context small.",
    "ignore": ["SettingsProjectScope", "SettingsResourceScope"],
}))


def _register_menu():
    """Plugins menu entries that work without an open binary (PluginCommand needs one)."""
    try:
        from binaryninjaui import Menu, UIAction, UIActionHandler
    except ImportError:     # headless: no UI, keep view-bound commands as a fallback
        PluginCommand.register("Script Bridge\\Start", "Listen for bnrun scripts on 127.0.0.1",
                               start)
        PluginCommand.register("Script Bridge\\Stop", "Stop the script bridge", stop)
        return
    # Each entry is enabled only when it applies, so the menu shows the state: Start while
    # stopped, Stop while running, Release Lock while someone holds the usage lock.
    for name, action, is_valid in (
            ("Script Bridge\\Start", start, lambda _ctx: _bridge is None),
            ("Script Bridge\\Stop", stop, lambda _ctx: _bridge is not None),
            ("Script Bridge\\Release Lock", release_lock, lambda _ctx: _locked())):
        UIAction.registerAction(name)
        UIActionHandler.globalActions().bindAction(name, UIAction(action, is_valid))
        Menu.mainMenu("Plugins").addAction(name, "Script Bridge")


_register_menu()

if Settings().get_bool("scriptbridge.autostart"):
    start()
