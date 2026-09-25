"""Script bridge, usage lock and MCP proxy for Binary Ninja, free of Binary Ninja imports.

The Binary Ninja plugin (__init__.py) supplies the script namespace (bv, bvs, binaryninja) and a
main-thread runner; this module does the HTTP, the checks, the execution, the usage lock and the
MCP proxy, so it can be tested with a plain python3.

Script bridge endpoints (bearer token + localhost checks, see ScriptBridge):

    GET  /ping     {"ok": true}
    POST /run      {"code": str, "view": str?, "main_thread": bool?, "session": str?}
                   -> {"ok", "stdout", "result", "error"}; 423 while locked by another session
    GET  /lock     lock status
    POST /lock     {"session": str, "name": str?, "purpose": str?, "ttl": seconds?}
                   take the lock, or renew it if this session already holds it
    POST /unlock   {"session": str?, "force": bool?} release it

Usage lock: the sessions sharing one Binary Ninja coordinate through it. No lock = free to use;
a lock = one session is busy. The holder is identified by its session id (e.g. Claude Code's
CLAUDE_CODE_SESSION_ID). This is coordination, not security: anyone can claim any id.
A lock expires after its TTL unless renewed; every script run and every MCP tool call by the
holder renews it.

MCP proxy (McpProxy): sits between the MCP clients and Binary Ninja's own MCP server, which the
plugin moves to a random port with a random bearer token known only to the proxy, so nothing
bypasses the lock. The proxy makes the lock visible to MCP clients:

- it adds two tools to tools/list, answered by the proxy itself and never blocked:
  bn_owner_get (who holds the lock) and bn_owner_set (take/renew it for the caller; with
  release=true give it back; release+force=true force-unlocks someone else's lock);
- while another session holds the lock, every other tools/call is answered with an error
  result naming the holder; everything else (initialize, tools/list, ...) is forwarded.

The caller's identity is the X-BN-Session header if the client sends one (Claude Code:
headersHelper printing CLAUDE_CODE_SESSION_ID, so it matches bnrun's identity), else the
MCP-Session-Id that Binary Ninja's server assigned to that connection.
"""

import hmac
import http.client
import http.server
import io
import json
import secrets
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

MAX_BODY = 1 << 20
DEFAULT_LOCK_TTL = 5 * 60
MAX_LOCK_TTL = 4 * 60 * 60
SESSION_HEADER = "X-BN-Session"
SESSION_NAME_HEADER = "X-BN-Session-Name"

# namespace_factory(view_filter) -> dict of globals for the script; raises LookupError when
# the requested view isn't open.
NamespaceFactory = Callable[[Optional[str]], Dict[str, Any]]
MainThreadRunner = Callable[[Callable[[], None]], None]
# listener(event, status) with event in "locked", "renewed", "released", "forced", "expired"
LockListener = Callable[[str, Dict[str, Any]], None]


def new_token() -> str:
    return secrets.token_hex(32)


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


def run_script(code: str, namespace: Dict[str, Any]) -> Dict[str, Any]:
    """Execute code with print() captured; the script may set `result`."""
    out = io.StringIO()

    def captured_print(*args, **kwargs):
        kwargs.setdefault("file", out)
        print(*args, **kwargs)

    namespace = dict(namespace)
    namespace["print"] = captured_print
    namespace.setdefault("__name__", "__bridge__")
    try:
        exec(compile(code, "<bridge>", "exec"), namespace)
        return {"ok": True, "stdout": out.getvalue(),
                "result": _jsonable(namespace.get("result")), "error": None}
    except BaseException:  # noqa: BLE001 -- report everything, including SystemExit
        return {"ok": False, "stdout": out.getvalue(), "result": None,
                "error": traceback.format_exc()}


class UsageLock:
    """Which session is using Binary Ninja right now. Thread-safe; times come from `clock`."""

    def __init__(self, clock: Callable[[], float] = time.time,
                 listener: Optional[LockListener] = None):
        self.clock = clock
        self.listener = listener or (lambda event, status: None)
        self.mutex = threading.Lock()
        self.holder: Optional[str] = None
        self.name = ""
        self.purpose = ""
        self.since = 0.0
        self.expires = 0.0
        self.ttl = DEFAULT_LOCK_TTL

    def _expire(self) -> None:
        if self.holder is not None and self.clock() >= self.expires:
            status = self._status()
            self.holder = None
            self.listener("expired", status)

    def _status(self) -> Dict[str, Any]:
        if self.holder is None:
            return {"locked": False}
        now = self.clock()
        return {"locked": True, "session": self.holder, "name": self.name,
                "purpose": self.purpose, "held_for": int(now - self.since),
                "expires_in": max(0, int(self.expires - now))}

    def _is_holder(self, session: Optional[str]) -> bool:
        return (self.holder is not None and isinstance(session, str)
                and hmac.compare_digest(session.encode(), self.holder.encode()))

    def status(self) -> Dict[str, Any]:
        with self.mutex:
            self._expire()
            return self._status()

    def acquire(self, session: str, name: str = "", purpose: str = "",
                ttl: Optional[float] = None) -> Tuple[bool, Dict[str, Any]]:
        """(True, status) if taken or renewed by the holder, else (False, holder's status)."""
        ttl = min(max(float(ttl or DEFAULT_LOCK_TTL), 1.0), MAX_LOCK_TTL)
        with self.mutex:
            self._expire()
            now = self.clock()
            if self.holder is None:
                self.holder, self.since = session, now
                self.name, self.purpose = name, purpose
                event = "locked"
            elif self._is_holder(session):
                self.name, self.purpose = name or self.name, purpose or self.purpose
                event = "renewed"
            else:
                return False, self._status()
            self.ttl, self.expires = ttl, now + ttl
            status = self._status()
        self.listener(event, status)
        return True, status

    def release(self, session: Optional[str] = None,
                force: bool = False) -> Tuple[bool, Dict[str, Any]]:
        """(True, status before release) if released or already free, else (False, status)."""
        with self.mutex:
            self._expire()
            status = self._status()
            if self.holder is None:
                return True, status
            is_holder = self._is_holder(session)
            if not (is_holder or force):
                return False, status
            self.holder = None
            event = "released" if is_holder else "forced"
        self.listener(event, status)
        return True, status

    def check_use(self, session: Optional[str]) -> Tuple[bool, Dict[str, Any]]:
        """(allowed, status): free, or used by the holder (which renews the lock)."""
        with self.mutex:
            self._expire()
            if self.holder is None:
                return True, self._status()
            if not self._is_holder(session):
                return False, self._status()
            self.expires = self.clock() + self.ttl
            return True, self._status()


def describe(status: Dict[str, Any]) -> str:
    if not status.get("locked"):
        return "free"
    who = status.get("name") or status["session"][:8]
    return "locked by %s%s (held %ds, expires in %ds; session %s)" % (
        who, (": " + status["purpose"]) if status.get("purpose") else "",
        status["held_for"], status["expires_in"], status["session"])


def _localhost_only(handler: http.server.BaseHTTPRequestHandler) -> Optional[str]:
    """Reject non-local peers, foreign Host headers (DNS rebinding) and browser requests."""
    if handler.client_address[0] != "127.0.0.1":
        return "peer is not 127.0.0.1"
    host = (handler.headers.get("Host") or "").rsplit(":", 1)[0]
    if host not in ("127.0.0.1", "localhost"):
        return "bad Host header"
    if handler.headers.get("Origin") is not None:
        return "Origin header not allowed"
    return None


def _reply_json(handler: http.server.BaseHTTPRequestHandler, status: int, body: Any) -> None:
    data = json.dumps(body).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class ScriptBridge:
    """Token-authenticated script runner and lock API (see the module docstring)."""

    def __init__(self, token: str, namespace_factory: NamespaceFactory,
                 main_thread_runner: Optional[MainThreadRunner] = None, port: int = 0,
                 lock: Optional[UsageLock] = None):
        self.token = token
        self.namespace_factory = namespace_factory
        self.main_thread_runner = main_thread_runner or (lambda fn: fn())
        self.lock = lock or UsageLock()
        self.run_mutex = threading.Lock()    # one script at a time
        bridge = self

        class Handler(http.server.BaseHTTPRequestHandler):
            server_version = "bn-script-bridge"

            def log_message(self, fmt, *args):
                pass

            def _refused(self) -> Optional[str]:
                reason = _localhost_only(self)
                if reason:
                    return reason
                auth = self.headers.get("Authorization") or ""
                if not auth.startswith("Bearer ") or not hmac.compare_digest(
                        auth[7:].encode(), bridge.token.encode()):
                    return "bad token"
                return None

            def _body(self) -> Optional[Dict[str, Any]]:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > MAX_BODY:
                    _reply_json(self, 413, {"ok": False, "error": "body missing or too large"})
                    return None
                try:
                    req = json.loads(self.rfile.read(length))
                    if not isinstance(req, dict):
                        raise ValueError("body must be a JSON object")
                    return req
                except ValueError as e:
                    _reply_json(self, 400, {"ok": False, "error": "bad request: %s" % e})
                    return None

            def do_GET(self):
                reason = self._refused()
                if reason:
                    return _reply_json(self, 403, {"ok": False, "error": reason})
                if self.path == "/ping":
                    return _reply_json(self, 200, {"ok": True})
                if self.path == "/lock":
                    return _reply_json(self, 200, {"ok": True, "lock": bridge.lock.status()})
                _reply_json(self, 404, {"ok": False, "error": "not found"})

            def do_POST(self):
                reason = self._refused()
                if reason:
                    return _reply_json(self, 403, {"ok": False, "error": reason})
                if self.path not in ("/run", "/lock", "/unlock"):
                    return _reply_json(self, 404, {"ok": False, "error": "not found"})
                req = self._body()
                if req is None:
                    return
                if self.path == "/lock":
                    return self._lock(req)
                if self.path == "/unlock":
                    return self._unlock(req)
                code = req.get("code")
                if not isinstance(code, str):
                    return _reply_json(self, 400, {"ok": False,
                                                   "error": "bad request: code must be a string"})
                allowed, status = bridge.lock.check_use(req.get("session"))
                if not allowed:
                    return _reply_json(self, 423, {"ok": False, "lock": status,
                                                   "error": "Binary Ninja is " + describe(status)})
                _reply_json(self, 200, bridge.execute(code, req.get("view"),
                                                      bool(req.get("main_thread"))))

            def _lock(self, req):
                session = req.get("session")
                if not isinstance(session, str) or not session.strip():
                    return _reply_json(self, 400, {"ok": False, "error": "bad request: session "
                                                   "must be a non-empty string"})
                try:
                    ttl = float(req["ttl"]) if req.get("ttl") is not None else None
                except (TypeError, ValueError):
                    return _reply_json(self, 400, {"ok": False, "error": "bad request: bad ttl"})
                ok, status = bridge.lock.acquire(session.strip(), str(req.get("name") or ""),
                                                 str(req.get("purpose") or ""), ttl)
                if not ok:
                    return _reply_json(self, 409, {"ok": False, "lock": status,
                                                   "error": "Binary Ninja is " + describe(status)})
                _reply_json(self, 200, {"ok": True, "lock": status})

            def _unlock(self, req):
                ok, status = bridge.lock.release(req.get("session"), bool(req.get("force")))
                if not ok:
                    return _reply_json(self, 409, {"ok": False, "lock": status,
                                                   "error": "not the holder: Binary Ninja is "
                                                            + describe(status)})
                _reply_json(self, 200, {"ok": True, "released": status})

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.httpd.server_address[1]

    def execute(self, code: str, view: Optional[str], main_thread: bool) -> Dict[str, Any]:
        with self.run_mutex:
            try:
                namespace = self.namespace_factory(view)
            except LookupError as e:
                return {"ok": False, "stdout": "", "result": None, "error": str(e)}
            if not main_thread:
                return run_script(code, namespace)
            box: Dict[str, Any] = {}
            self.main_thread_runner(lambda: box.update(run_script(code, namespace)))
            return box

    def start(self) -> None:
        threading.Thread(target=self.httpd.serve_forever, name="bn-script-bridge",
                         daemon=True).start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# Headers not forwarded in either direction (hop-by-hop, or recomputed by the proxy).
_HOP_HEADERS = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "te",
                "trailer", "upgrade", "host", "content-length"}


OWNER_TOOLS = [
    {
        "name": "bn_owner_get",
        "description": "Who is using Binary Ninja right now (the usage lock shared by all sessions). "
                       "Returns {locked: false} when free, else the holder's session, name, "
                       "purpose and expiry. Check it before using Binary Ninja.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "bn_owner_set",
        "description": "Take (or renew) the Binary Ninja usage lock for this session before "
                       "switching views, editing or saving; while it is held, other sessions' "
                       "Binary Ninja tool calls are refused. It expires after ttl_minutes "
                       "(default 5) unless renewed; your own tool calls renew it. "
                       "release=true gives it back when done; release=true with force=true "
                       "force-unlocks another session's lock (only for stale locks, after "
                       "asking the holder or the user).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Your session name, for others to read."},
                "purpose": {"type": "string", "description": "What you are doing."},
                "ttl_minutes": {"type": "number", "description": "Minutes until it expires "
                                                                 "(default 5, max 240)."},
                "release": {"type": "boolean", "description": "Give the lock back."},
                "force": {"type": "boolean", "description": "With release: force-unlock "
                                                            "another session's lock."},
            },
            "additionalProperties": False,
        },
    },
]
OWNER_TOOL_NAMES = {t["name"] for t in OWNER_TOOLS}


def owner_tool(lock: UsageLock, session: Optional[str], name: str,
               args: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    """Run bn_owner_get / bn_owner_set for `session`: (ok, payload)."""
    if name == "bn_owner_get":
        status = lock.status()
        return True, {"lock": status, "text": describe(status), "you": session}
    if not session:
        return False, {"text": "no session identity (no X-BN-Session header and no "
                               "MCP-Session-Id); can't take or release the lock"}
    if args.get("release"):
        ok, status = lock.release(session, bool(args.get("force")))
        if not ok:
            return False, {"lock": status, "text": "not the holder: Binary Ninja is %s "
                                                   "(use force=true only for a stale lock)"
                                                   % describe(status)}
        return True, {"released": status,
                      "text": ("released (%s)" % describe(status)) if status.get("locked")
                      else "was already free"}
    try:
        ttl = float(args["ttl_minutes"]) * 60 if args.get("ttl_minutes") is not None else None
    except (TypeError, ValueError):
        return False, {"text": "bad ttl_minutes"}
    ok, status = lock.acquire(session, str(args.get("name") or ""),
                              str(args.get("purpose") or ""), ttl)
    if not ok:
        return False, {"lock": status, "text": "Binary Ninja is " + describe(status)}
    return True, {"lock": status, "text": describe(status)}


def _tool_calls(message: Any) -> List[Any]:
    """JSON-RPC ids of the tools/call requests in a message or batch (None-id calls included)."""
    items = message if isinstance(message, list) else [message]
    return [m.get("id") for m in items
            if isinstance(m, dict) and m.get("method") == "tools/call"]


def blocked_reply(message: Any, text: str) -> Any:
    """JSON-RPC responses that answer every request in `message` with a tool error result."""
    items = message if isinstance(message, list) else [message]
    replies = [{"jsonrpc": "2.0", "id": m.get("id"),
                "result": {"content": [{"type": "text", "text": text}], "isError": True}}
               for m in items if isinstance(m, dict) and "id" in m and "method" in m]
    return replies if isinstance(message, list) else (replies[0] if replies else None)


def _is_tools_list(body: bytes) -> bool:
    try:
        message = json.loads(body)
    except ValueError:
        return False
    return isinstance(message, dict) and message.get("method") == "tools/list"


def add_owner_tools(message: Any) -> Any:
    """A tools/list response with OWNER_TOOLS appended (other messages unchanged)."""
    tools = ((message or {}).get("result") or {}).get("tools") if isinstance(message, dict) \
        else None
    if isinstance(tools, list) and not any(t.get("name") in OWNER_TOOL_NAMES for t in tools):
        tools.extend(OWNER_TOOLS)
    return message


class McpProxy:
    """Lock-aware pass-through to Binary Ninja's MCP server (see the module docstring)."""

    def __init__(self, lock: UsageLock, upstream_port: int, port: int = 0,
                 upstream_host: str = "127.0.0.1", timeout: float = 600.0,
                 upstream_token: Optional[str] = None):
        self.lock = lock
        # Where Binary Ninja's MCP server listens and its bearer token. The plugin moves that
        # server to a random port with a random token that only the proxy knows, so MCP
        # clients can't bypass the lock; clients' own Authorization headers are dropped.
        # Both can change while the proxy runs (the plugin sets them after restarting it).
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.upstream_token = upstream_token
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            server_version = "bn-mcp-proxy"
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass

            def _forward(self, body: Optional[bytes], add_tools: bool = False) -> None:
                headers = {k: v for k, v in self.headers.items()
                           if k.lower() not in _HOP_HEADERS
                           and k.lower() not in (SESSION_HEADER.lower(),
                                                 SESSION_NAME_HEADER.lower(), "authorization")}
                headers["Host"] = "%s:%d" % (proxy.upstream_host, proxy.upstream_port)
                if proxy.upstream_token:
                    headers["Authorization"] = "Bearer " + proxy.upstream_token
                if body is not None:
                    headers["Content-Length"] = str(len(body))
                conn = http.client.HTTPConnection(proxy.upstream_host, proxy.upstream_port,
                                                  timeout=timeout)
                try:
                    conn.request(self.command, self.path, body=body, headers=headers)
                    resp = conn.getresponse()
                except OSError as e:
                    conn.close()
                    return _reply_json(self, 502, {"error": "Binary Ninja MCP server "
                                                            "unreachable: %s" % e})
                try:
                    if add_tools and resp.status == 200:
                        return self._forward_tools_list(resp)
                    if resp.status == 400 and self.headers.get("Mcp-Session-Id"):
                        return self._forward_bad_request(resp)
                    self.send_response(resp.status, resp.reason)
                    for k, v in resp.getheaders():
                        if k.lower() not in _HOP_HEADERS:
                            self.send_header(k, v)
                    length = resp.getheader("Content-Length")
                    if length is not None:
                        self.send_header("Content-Length", length)
                    # Unknown length (e.g. an SSE stream): the body ends when we close.
                    self.send_header("Connection", "close")
                    self.close_connection = True
                    self.end_headers()
                    while True:
                        chunk = resp.read1(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except OSError:
                    pass    # client went away
                finally:
                    conn.close()

            def _forward_tools_list(self, resp) -> None:
                """Relay a tools/list reply (JSON or SSE) with OWNER_TOOLS appended."""
                data = resp.read()
                ctype = resp.getheader("Content-Type") or ""
                if "text/event-stream" in ctype:
                    lines = data.decode().split("\n")
                    for i, line in enumerate(lines):
                        if line.startswith("data:"):
                            lines[i] = "data: " + json.dumps(add_owner_tools(
                                json.loads(line[5:].strip())))
                    data = "\n".join(lines).encode()
                else:
                    data = json.dumps(add_owner_tools(json.loads(data))).encode()
                self.send_response(resp.status, resp.reason)
                for k, v in resp.getheaders():
                    if k.lower() not in _HOP_HEADERS:
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
                self.wfile.write(data)

            def _forward_bad_request(self, resp) -> None:
                """Relay a 400, but turn "unknown session" into 404.

                Binary Ninja answers a stale MCP-Session-Id (e.g. after its MCP server was
                restarted, which the plugin does on every bridge start) with 400 "Missing or
                invalid MCP-Session-Id". The MCP spec says 404, which makes clients
                re-initialize on their own; with 400 they stay broken until reconnected."""
                data = resp.read()
                if b"MCP-Session-Id" in data:
                    return _reply_json(self, 404, {"error": "unknown MCP session (Binary Ninja's "
                                                            "MCP server restarted); re-initialize"})
                self.send_response(resp.status, resp.reason)
                for k, v in resp.getheaders():
                    if k.lower() not in _HOP_HEADERS:
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _session(self) -> Optional[str]:
                return (self.headers.get(SESSION_HEADER)
                        or self.headers.get("Mcp-Session-Id") or None)

            def _owner_call(self, message: Any) -> bool:
                """Answer bn_owner_* calls here. True if the request was handled."""
                if not (isinstance(message, dict) and message.get("method") == "tools/call"):
                    return False
                params = message.get("params") or {}
                if params.get("name") not in OWNER_TOOL_NAMES:
                    return False
                args = params.get("arguments") or {}
                if params.get("name") == "bn_owner_set" and not args.get("name"):
                    args = dict(args, name=self.headers.get(SESSION_NAME_HEADER) or "")
                ok, payload = owner_tool(proxy.lock, self._session(), params["name"], args)
                _reply_json(self, 200, {"jsonrpc": "2.0", "id": message.get("id"), "result": {
                    "content": [{"type": "text", "text": json.dumps(payload, indent=1)}],
                    "structuredContent": payload, "isError": not ok}})
                return True

            def _gate(self, body: bytes) -> bool:
                """True if the request was answered here (owner tools, or blocked by the lock)."""
                try:
                    message = json.loads(body)
                except ValueError:
                    return False
                if self._owner_call(message):
                    return True
                if not _tool_calls(message):
                    return False
                allowed, status = proxy.lock.check_use(self._session())
                if allowed:
                    return False
                text = ("Binary Ninja is %s. Wait until it is free (bn_owner_get), or ask the "
                        "holder; don't use Binary Ninja meanwhile." % describe(status))
                reply = blocked_reply(message, text)
                if reply is None:   # notifications only: nothing to answer
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    _reply_json(self, 200, reply)
                return True

            def _handle(self, with_body: bool) -> None:
                reason = _localhost_only(self)
                if reason:
                    return _reply_json(self, 403, {"error": reason})
                body = None
                if with_body:
                    length = int(self.headers.get("Content-Length") or 0)
                    if length > MAX_BODY:
                        return _reply_json(self, 413, {"error": "body too large"})
                    body = self.rfile.read(length)
                    if self.command == "POST" and self._gate(body):
                        return
                self._forward(body, add_tools=self.command == "POST" and _is_tools_list(body))

            def do_POST(self):
                self._handle(True)

            def do_GET(self):
                self._handle(False)

            def do_DELETE(self):
                self._handle(False)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.httpd.server_address[1]

    def start(self) -> None:
        threading.Thread(target=self.httpd.serve_forever, name="bn-mcp-proxy",
                         daemon=True).start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
