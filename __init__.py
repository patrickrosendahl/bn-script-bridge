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
    _bridge = bridge_server.ScriptBridge(token, _namespace, execute_on_main_thread_and_wait,
                                         lock=lock)
    try:
        settings = Settings()
        _proxy = bridge_server.McpProxy(lock, settings.get_integer("ui.mcp.port"),
                                        settings.get_integer("scriptbridge.mcpProxyPort"),
                                        upstream_token=settings.get_string("ui.mcp.token"))
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
