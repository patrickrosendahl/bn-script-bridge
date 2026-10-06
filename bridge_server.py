"""Script bridge, usage lock and MCP proxy for Binary Ninja, free of Binary Ninja imports.

The Binary Ninja plugin (__init__.py) supplies the UI views (bv, bvs), the base namespace
(binaryninja), the scriptbridge.maxParallel setting and a main-thread runner; this module does the HTTP, the checks, the execution, the usage lock and the
MCP proxy, so it can be tested with a plain python3.

Script bridge endpoints (bearer token + localhost checks, see ScriptBridge):

    GET  /ping     {"ok": true}
    POST /run      {"code": str, "view": str?, "main_thread": bool?, "parallel": bool?,
                    "session": str?, "timeout": seconds?, "run_id": str?}
                   -> {"ok", "stdout", "result", "error"}; 423 while locked by another session;
                   400 for parallel + main_thread / parallel + view; 409 for view or main_thread
                   without holding the usage lock (see "UI views" below).
                   A cancelled script answers error "cancelled" (plus "cancelled_by") with the
                   stdout captured so far.
    GET  /run      {"ok", "running": serialized script status or null, "queued": int,
                    "parallel": [status, ...], "parallel_queued": int, "max_parallel": int}
    POST /cancel   {"session": str?, "run_id": str?, "force": bool?} cancel running scripts
                   (or, with run_id, that run even if still queued); 409 if nothing to cancel,
                   403 if not allowed (see ScriptBridge.cancel)
    GET  /lock     lock status (+ the keys of GET /run)

Two lanes: serialized scripts (the default) run one at a time behind run_mutex; parallel
scripts ("parallel": true) don't take run_mutex and run concurrently with the serialized one
and with each other, at most max_parallel at once (more wait for a slot). Each script runs in
its own HTTP handler thread (or, with main_thread, on the main thread), so cancel/timeout target
that thread only. Parallel is for scripts that only create, analyse and close their own views.

UI views: the namespace's `bv`/`bvs` (the views open in the UI) are only provided to a
serialized script whose session holds the usage lock. Otherwise they are NoUIViews placeholders
that raise UIViewsUnavailable on use; "view" and "main_thread" are refused without the lock, and
"view" is refused for parallel scripts. This is namespace-level enforcement backed by convention:
a script can still reach UI views through binaryninjaui or other API paths.
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

Cancellation: a cancel raises ScriptCancelled (a BaseException, so `except Exception` in a
script doesn't swallow it) in the thread executing the script, through
PyThreadState_SetAsyncExc. Python delivers it between bytecodes only: a script blocked in a long
native call (bv.update_analysis_and_wait(), time.sleep(), a big C++ API call) stops when that
call returns. The bridge clears a cancel that would land after the script finished, so it never
leaks into whatever the thread (e.g. Binary Ninja's main thread) runs next.
"""

import ctypes
import hmac
import http.client
import http.server
import io
import json
import secrets
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

MAX_BODY = 1 << 20
DEFAULT_LOCK_TTL = 5 * 60
MAX_LOCK_TTL = 4 * 60 * 60
SESSION_HEADER = "X-BN-Session"
SESSION_NAME_HEADER = "X-BN-Session-Name"
DEFAULT_MAX_PARALLEL = 4

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


class ScriptCancelled(BaseException):
    """Raised inside a running script to cancel it (BaseException: `except Exception` misses it)."""


class UIViewsUnavailable(RuntimeError):
    """Raised when a script uses `bv`/`bvs` without being allowed UI views."""


class NoUIViews:
    """Stands in for `bv`/`bvs` when the script may not use UI views: any use raises."""

    def __init__(self, name: str, reason: str):
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_reason", reason)

    def _refuse(self, *_args, **_kwargs):
        raise UIViewsUnavailable("`%s` is not available: %s" % (self._name, self._reason))

    def __getattr__(self, attr):
        if attr.startswith("__"):
            raise AttributeError(attr)
        self._refuse()

    __setattr__ = __iter__ = __len__ = __getitem__ = __bool__ = __contains__ = __call__ = _refuse

    def __repr__(self):
        return "<no UI views: %s>" % self._reason


NEED_LOCK = ("UI views (bv, bvs, --view, --main-thread) need the usage lock; take the usage "
             "lock first: bnrun --lock --purpose '...' (scripts that only create their own "
             "views with BinaryViewType[...].create / BinaryView.open / binaryninja.load need "
             "no lock)")
PARALLEL_NO_UI = ("parallel scripts get no UI views (bv, bvs, --view); UI work goes through "
                  "the serialized lane: run without --parallel while holding the usage lock")


_set_async_exc_fn = None


def _async_exc_function():
    """PyThreadState_SetAsyncExc from the running interpreter (cached)."""
    global _set_async_exc_fn
    if _set_async_exc_fn is None:
        candidates = [lambda: ctypes.pythonapi]
        # Embedded interpreters (Binary Ninja) may load libpython without exporting its symbols
        # globally; then look in the library itself.
        import os
        import sys
        import sysconfig
        for path in (os.path.join(sys.base_prefix, "Python"),
                     os.path.join(sysconfig.get_config_var("LIBDIR") or "",
                                  sysconfig.get_config_var("LDLIBRARY") or "")):
            candidates.append(lambda path=path: ctypes.PyDLL(path))
        for candidate in candidates:
            try:
                _set_async_exc_fn = candidate().PyThreadState_SetAsyncExc
                break
            except (OSError, AttributeError):
                continue
        else:
            raise RuntimeError("PyThreadState_SetAsyncExc not found; can't cancel scripts")
    return _set_async_exc_fn


def set_async_exc(ident: int, exc: Optional[type]) -> int:
    """Raise exc in thread `ident` at its next bytecode; exc=None clears a pending one.
    Returns the number of threads affected (0: no such thread)."""
    return _async_exc_function()(ctypes.c_ulong(ident),
                                 ctypes.py_object(exc) if exc is not None else None)


def run_script(code: str, namespace: Dict[str, Any],
               out: Optional[io.StringIO] = None) -> Dict[str, Any]:
    """Execute code with print() captured into `out`; the script may set `result`."""
    out = out if out is not None else io.StringIO()

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
    except ScriptCancelled:
        return {"ok": False, "stdout": out.getvalue(), "result": None, "error": "cancelled"}
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

    def holds(self, session: Optional[str]) -> bool:
        """True if `session` holds the lock right now (no renewal)."""
        with self.mutex:
            self._expire()
            return self._is_holder(session)

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


def _label(code: str) -> str:
    """First meaningful line of a script, for status displays."""
    for line in code.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line if len(line) <= 60 else line[:57] + "..."
    return "<empty>"


class Run:
    """One /run request: its identity and the cancellation handshake with the executing thread.

    executing/sending/exc_sent are plain attributes; each assignment is atomic under the GIL.
    The canceller (holding ScriptBridge.state) sets sending, re-checks executing and only then
    raises; the executing thread clears executing, waits while sending, and clears any exception
    that was sent (see ScriptBridge._run_guarded)."""

    def __init__(self, code: str, session: Optional[str], name: str, main_thread: bool,
                 timeout: Optional[float], run_id: str, parallel: bool = False):
        self.run_id = run_id
        self.session = session
        self.name = name
        self.label = _label(code)
        self.main_thread = main_thread
        self.parallel = parallel
        self.finished = False
        self.timeout = timeout
        self.queued_at = time.time()
        self.started: Optional[float] = None
        self.thread: Optional[int] = None
        self.executing = False
        self.sending = False
        self.exc_sent = False
        self.cancel_by: Optional[str] = None
        self.timer: Optional[threading.Timer] = None

    def status(self) -> Dict[str, Any]:
        now = time.time()
        return {"session": self.session, "name": self.name, "label": self.label,
                "main_thread": self.main_thread, "parallel": self.parallel,
                "timeout": self.timeout,
                "state": "running" if self.started else "starting",
                "running_for": int(now - self.started) if self.started else 0,
                "cancel_requested": self.cancel_by}


def describe_run(status: Optional[Dict[str, Any]]) -> str:
    if not status:
        return "no script running"
    who = status.get("name") or (status.get("session") or "")[:8] or "no session"
    extra = [who]
    if status.get("parallel"):
        extra.append("parallel")
    if status.get("main_thread"):
        extra.append("main thread")
    if status.get("timeout"):
        extra.append("timeout %gs" % status["timeout"])
    if status.get("cancel_requested"):
        extra.append("cancel requested: %s" % status["cancel_requested"])
    return "running script %r for %ds (%s)" % (status["label"], status["running_for"],
                                               ", ".join(extra))


def describe_runs(reply: Dict[str, Any]) -> str:
    """All running scripts of a GET /run (or /lock) reply, one per line; "no script running"
    only when nothing runs or waits in either lane."""
    runs = ([reply["running"]] if reply.get("running") else []) + list(reply.get("parallel") or [])
    lines = [describe_run(r) for r in runs]
    queued, pqueued = reply.get("queued") or 0, reply.get("parallel_queued") or 0
    if queued or pqueued:
        lines.append("queued: %d serialized, %d parallel" % (queued, pqueued))
    return "\n".join(lines) or describe_run(None)


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
                 lock: Optional[UsageLock] = None,
                 max_parallel: Union[int, Callable[[], int]] = DEFAULT_MAX_PARALLEL,
                 base_namespace: Optional[Dict[str, Any]] = None):
        """namespace_factory(view) gives the UI views (bv, bvs, ...) and is only called for
        scripts allowed to use them; base_namespace (e.g. the binaryninja module) is what every
        script gets. max_parallel: int, or a callable read whenever a parallel script waits."""
        self.token = token
        self.namespace_factory = namespace_factory
        self.base_namespace = dict(base_namespace or {})
        self.main_thread_runner = main_thread_runner or (lambda fn: fn())
        self.lock = lock or UsageLock()
        self.max_parallel = max_parallel
        self.run_mutex = threading.Lock()    # serialized lane: one script at a time
        self.state = threading.Lock()        # guards current, queued, parallel*, cancelled_ids
        self.slot_free = threading.Condition(self.state)   # a parallel slot or cancel happened
        self.current: Optional[Run] = None   # the serialized run
        self.queued = 0                      # serialized runs waiting for run_mutex
        self.parallel_runs: List[Run] = []   # parallel runs holding a slot
        self.parallel_queued = 0             # parallel runs waiting for a slot
        self.cancelled_ids: Dict[str, str] = {}   # run_id -> by, for runs not started yet
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
                    return _reply_json(self, 200, dict(bridge.run_status(), ok=True,
                                                       lock=bridge.lock.status()))
                if self.path == "/run":
                    return _reply_json(self, 200, dict(bridge.run_status(), ok=True))
                _reply_json(self, 404, {"ok": False, "error": "not found"})

            def do_POST(self):
                reason = self._refused()
                if reason:
                    return _reply_json(self, 403, {"ok": False, "error": reason})
                if self.path not in ("/run", "/lock", "/unlock", "/cancel"):
                    return _reply_json(self, 404, {"ok": False, "error": "not found"})
                req = self._body()
                if req is None:
                    return
                if self.path == "/lock":
                    return self._lock(req)
                if self.path == "/unlock":
                    return self._unlock(req)
                if self.path == "/cancel":
                    return _reply_json(self, *bridge.cancel(
                        req.get("session"), req.get("run_id"), bool(req.get("force"))))
                code = req.get("code")
                if not isinstance(code, str):
                    return _reply_json(self, 400, {"ok": False,
                                                   "error": "bad request: code must be a string"})
                view, main_thread = req.get("view"), bool(req.get("main_thread"))
                parallel = bool(req.get("parallel"))
                if parallel and (main_thread or view):
                    return _reply_json(self, 400, {"ok": False, "error": "bad request: "
                                                   + ("parallel scripts can't run on the main "
                                                      "thread (--parallel with --main-thread)"
                                                      if main_thread else PARALLEL_NO_UI)})
                allowed, status = bridge.lock.check_use(req.get("session"))
                if not allowed:
                    return _reply_json(self, 423, {"ok": False, "lock": status,
                                                   "error": "Binary Ninja is " + describe(status)})
                if (view or main_thread) and not bridge.lock.holds(req.get("session")):
                    return _reply_json(self, 409, {"ok": False, "lock": status,
                                                   "error": NEED_LOCK})
                timeout = req.get("timeout")
                if timeout is not None and (isinstance(timeout, bool) or
                                            not isinstance(timeout, (int, float)) or timeout <= 0):
                    return _reply_json(self, 400, {"ok": False, "error": "bad request: timeout "
                                                   "must be a positive number of seconds"})
                run_id = req.get("run_id")
                reply = bridge.execute(code, view, main_thread,
                                       session=req.get("session"), timeout=timeout,
                                       run_id=run_id if isinstance(run_id, str) else None,
                                       name=str(req.get("name") or ""), parallel=parallel)
                try:
                    _reply_json(self, 200, reply)
                except (BrokenPipeError, ConnectionResetError):
                    pass    # client gone (interrupted); the script already finished or stopped

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

    def running(self) -> Optional[Dict[str, Any]]:
        """Status of the serialized script being run (see Run.status), or None when idle."""
        with self.state:
            return self.current.status() if self.current else None

    def limit(self) -> int:
        """Current maximum of parallel scripts (at least 1)."""
        value = self.max_parallel() if callable(self.max_parallel) else self.max_parallel
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return DEFAULT_MAX_PARALLEL

    def run_status(self) -> Dict[str, Any]:
        """Both lanes: the GET /run reply without "ok"."""
        with self.state:
            return {"running": self.current.status() if self.current else None,
                    "queued": self.queued,
                    "parallel": [r.status() for r in self.parallel_runs],
                    "parallel_queued": self.parallel_queued,
                    "max_parallel": self.limit()}

    def execute(self, code: str, view: Optional[str], main_thread: bool,
                session: Optional[str] = None, timeout: Optional[float] = None,
                run_id: Optional[str] = None, name: str = "",
                parallel: bool = False) -> Dict[str, Any]:
        if parallel and main_thread:
            return {"ok": False, "stdout": "", "result": None,
                    "error": "parallel scripts can't run on the main thread"}
        run = Run(code, session if isinstance(session, str) else None, name, main_thread,
                  float(timeout) if timeout else None, run_id or secrets.token_hex(16),
                  parallel)
        if parallel:
            return self._execute_parallel(run, code, view)
        with self.state:
            self.queued += 1
        waiting = True
        try:
            with self.run_mutex:
                with self.state:
                    self.queued -= 1
                    waiting = False
                    self.current = run
                    if run.run_id in self.cancelled_ids:
                        run.cancel_by = self.cancelled_ids.pop(run.run_id)
                try:
                    return self._execute_run(run, code, view)
                finally:
                    if run.timer is not None:
                        run.timer.cancel()
                    with self.state:
                        self.current = None
                        run.finished = True
        finally:
            if waiting:
                with self.state:
                    self.queued -= 1

    def _execute_parallel(self, run: Run, code: str, view: Optional[str]) -> Dict[str, Any]:
        with self.state:
            self.parallel_queued += 1
            try:
                while (len(self.parallel_runs) >= self.limit()
                       and run.run_id not in self.cancelled_ids):
                    self.slot_free.wait(0.5)    # also picks up a changed limit
            finally:
                self.parallel_queued -= 1
            if run.run_id in self.cancelled_ids:
                run.cancel_by = self.cancelled_ids.pop(run.run_id)
                run.finished = True
                return self._cancelled(run, "")
            self.parallel_runs.append(run)
        try:
            return self._execute_run(run, code, view)
        finally:
            if run.timer is not None:
                run.timer.cancel()
            with self.state:
                self.parallel_runs.remove(run)
                run.finished = True
                self.slot_free.notify_all()

    def _namespace(self, run: Run, view: Optional[str]) -> Dict[str, Any]:
        """The script's globals: the UI views only for a serialized lock holder.
        Raises LookupError (bad view filter) or UIViewsUnavailable (view not allowed)."""
        if run.parallel:
            reason = PARALLEL_NO_UI
        elif not self.lock.holds(run.session):
            reason = NEED_LOCK
        else:
            return dict(self.base_namespace, **self.namespace_factory(view))
        if view or run.main_thread:     # re-checked here: the lock may have gone while queued
            raise UIViewsUnavailable(reason)
        return dict(self.base_namespace, bv=NoUIViews("bv", reason),
                    bvs=NoUIViews("bvs", reason))

    def _execute_run(self, run: Run, code: str, view: Optional[str]) -> Dict[str, Any]:
        """Run a script that holds its lane (run_mutex or a parallel slot)."""
        if run.cancel_by is not None:
            return self._cancelled(run, "")
        try:
            namespace = self._namespace(run, view)
        except (LookupError, UIViewsUnavailable) as e:
            return {"ok": False, "stdout": "", "result": None, "error": str(e)}
        box: Dict[str, Any] = {}

        def job():
            box.update(self._run_guarded(run, code, namespace))
        if run.main_thread:
            self.main_thread_runner(job)
        else:
            job()
        return box or self._cancelled(run, "")

    @staticmethod
    def _cancelled(run: Run, stdout: str) -> Dict[str, Any]:
        return {"ok": False, "stdout": stdout, "result": None, "error": "cancelled",
                "cancelled_by": run.cancel_by}

    def _run_guarded(self, run: Run, code: str, namespace: Dict[str, Any]) -> Dict[str, Any]:
        """Run the script in the calling thread (a worker or the main thread) so that a cancel
        can target it, and make sure no ScriptCancelled outlives the script."""
        out = io.StringIO()
        result: Optional[Dict[str, Any]] = None
        begun = False
        while True:     # a late ScriptCancelled may hit this bookkeeping: redo it until clean
            try:
                if not begun:
                    begun = True
                    run.thread = threading.get_ident()
                    run.started = time.time()
                    run.executing = True
                    if run.cancel_by is not None:   # cancelled before it began
                        raise ScriptCancelled()
                    if run.timeout:
                        run.timer = threading.Timer(run.timeout, self._on_timeout, (run,))
                        run.timer.daemon = True
                        run.timer.start()
                    result = run_script(code, namespace, out)
                run.executing = False
                while run.sending:          # a canceller is deciding: wait for its verdict
                    time.sleep(0.001)
                if run.exc_sent:            # clear a cancel that hasn't fired yet
                    set_async_exc(run.thread, None)
                break
            except ScriptCancelled:
                continue
        if run.cancel_by is not None and (result is None or result.get("error") == "cancelled"):
            return self._cancelled(run, out.getvalue())
        return result if result is not None else self._cancelled(run, out.getvalue())

    def _interrupt(self, run: Run) -> None:
        """Raise ScriptCancelled in the script's thread if it is still executing. Holds state."""
        if not run.executing:
            return
        run.sending = True
        try:
            if run.executing:
                if set_async_exc(run.thread, ScriptCancelled):
                    run.exc_sent = True
        finally:
            run.sending = False

    def _on_timeout(self, run: Run) -> None:
        with self.state:
            if not run.finished and run.cancel_by is None:
                run.cancel_by = "timeout after %gs" % run.timeout
                self._interrupt(run)

    def cancel(self, session: Optional[str] = None, run_id: Optional[str] = None,
               force: bool = False) -> Tuple[int, Dict[str, Any]]:
        """Cancel running scripts: (HTTP status, reply).

        With run_id (only the client that sent /run knows it): exactly that run; one that isn't
        running yet (queued, or not arrived) is remembered and won't start. Without: the
        caller's own running scripts (serialized and parallel) if it has any; otherwise, for
        the usage-lock holder or with force, every running script. The reply's "cancelled" is
        the first cancelled run (the serialized one if hit), "cancelled_runs" all of them."""
        session = session if isinstance(session, str) and session else None
        run_id = run_id if isinstance(run_id, str) and run_id else None
        holder = self.lock.status().get("session")

        def same(a, b):
            return a is not None and b is not None and hmac.compare_digest(a.encode(),
                                                                           b.encode())
        with self.state:
            active = ([self.current] if self.current else []) + list(self.parallel_runs)
            if run_id:
                targets = [r for r in active if same(run_id, r.run_id)]
                if not targets:
                    self.cancelled_ids[run_id] = "its client"
                    while len(self.cancelled_ids) > 100:
                        self.cancelled_ids.pop(next(iter(self.cancelled_ids)))
                    self.slot_free.notify_all()     # a queued parallel run stops waiting
                    return 200, {"ok": True, "cancelled": None, "pending": True}
                by = "its client"
            elif not active:
                return 409, {"ok": False, "running": None, "error": "no script is running"}
            else:
                targets = [r for r in active if same(session, r.session)]
                if targets:
                    by = "session %s" % session
                elif same(session, holder):
                    targets, by = active, "lock holder %s" % session
                elif force:
                    targets, by = active, "force by %s" % (session or "unknown session")
                else:
                    owners = sorted({r.name or r.session or "no session" for r in active})
                    return 403, {"ok": False, "running": active[0].status(),
                                 "error": "not allowed: the running script%s belong%s to %s; "
                                          "you need to be that session or the lock holder, "
                                          "or use force" % (
                                              "s" if len(active) > 1 else "",
                                              "" if len(active) > 1 else "s",
                                              ", ".join(owners))}
            for run in targets:
                if run.cancel_by is None:
                    run.cancel_by = by
                try:
                    self._interrupt(run)
                except RuntimeError as e:
                    return 500, {"ok": False, "running": run.status(), "error": str(e)}
            return 200, {"ok": True, "cancelled": targets[0].status(),
                         "cancelled_runs": [r.status() for r in targets]}

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
