"""Script bridge, usage lock and MCP proxy for Binary Ninja, free of Binary Ninja imports.

The Binary Ninja plugin (__init__.py) supplies the UI views (bv, bvs), the base namespace
(binaryninja), the scriptbridge.maxParallel setting and a main-thread runner; this module does the HTTP, the checks, the execution, the usage lock and the
MCP proxy, so it can be tested with a plain python3.

Script bridge endpoints (bearer token + localhost checks, see ScriptBridge):

    GET  /ping     {"ok": true}
    POST /run      {"code": str, "view": str?, "main_thread": bool?, "parallel": bool?,
                    "bndb": str?, "args": [str, ...]?, "env": {str: str}?,
                    "session": str?, "timeout": seconds?, "run_id": str?}
                   -> {"ok", "stdout", "result", "error"}; 423 while locked by another session;
                   400 for parallel + main_thread / parallel + view / bndb + view, main_thread
                   or parallel; 409 for view or main_thread without holding the usage lock
                   (see "UI views" below).
                   A cancelled script answers error "cancelled" (plus "cancelled_by") with the
                   stdout captured so far.
                   args is argv (argv[0] plus the script's arguments) and env extra environment
                   variables; serialized runs also swap them into sys.argv / os.environ for the
                   duration of the script (restored after; parallel runs overlap, so they only
                   get the bare `argv` / `env` names in their namespace). bndb opens that
                   database headlessly, caches the view and runs the script with it as `bv`
                   (serialized lane, no usage lock needed; see "bndb cache" below).
    GET  /run      {"ok", "running": serialized script status or null, "queued": int,
                    "parallel": [status, ...], "parallel_queued": int, "max_parallel": int,
                    "bndb": [cached view status, ...], "jobs": [unfinished job status, ...],
                    "lost": int}
    POST /cancel   {"session": str?, "run_id": str?, "force": bool?} cancel running scripts
                   (or, with run_id, that run even if still queued); 409 if nothing to cancel,
                   403 if not allowed (see ScriptBridge.cancel)
    GET  /lock     lock status (+ the keys of GET /run)
    POST /bndb     {"action": "save"|"close", "path": str?} save or close cached views
                   (all of them when path is omitted); 404 when that path isn't cached,
                   409 while a serialized script runs, 423 while locked by another session
    POST /result   {"run_id": str, "wait": seconds?} the job's state, waiting up to "wait"
                   (max 60) for it to finish: {"ok", "state": "queued"|"starting"|"running"|
                   "done"|"lost", "run": status, "reply": the /run reply once done}; a lost job
                   (see "Jobs") answers {"state": "lost", "lost": record}; 404 if unknown
    POST /jobs     {"session": str?} {"ok", "jobs": [that session's jobs, with run_id],
                   "lost": [every lost job, with run_id]}
    POST /lost     {"action": "requeue"|"forget", "run_ids": [str, ...]|"all", "force": bool?,
                   "session": str?, "name": str?} rerun (as new jobs of this session, same
                   checks as /run) or drop lost jobs; one that was running when its bridge died
                   is only requeued with force: it may have applied part of its edits, or be
                   what wedged Binary Ninja

Jobs: a /run with "async": true answers 202 {"ok", "run_id", "job", "state"} at once and runs
the script in a thread of its own; the client polls POST /result (long poll) for the reply, so
no HTTP request is held open for the whole run and a client timeout never abandons a job.
Submitting a run_id the bridge already knows answers that job instead of running it again, so a
client may resubmit after a lost connection without double-applying. Every run (async or not)
is a job: finished ones keep their reply for /result for JOB_KEEP_SECONDS (at most JOB_KEEP).
With a spool_dir, each job's request is written there while it is queued or running and
removed when it finishes; jobs still spooled when a bridge starts (their Binary Ninja died or
stopped first) become "lost": reported by GET /run ("lost": count), /jobs and /result, kept on
disk until requeued or forgotten (POST /lost). Nothing is rerun on its own.

Two lanes: serialized scripts (the default) run one at a time behind run_mutex; parallel
scripts ("parallel": true) don't take run_mutex and run concurrently with the serialized one
and with each other, at most max_parallel at once (more wait for a slot). Each script runs in
its own HTTP handler thread (or, with main_thread, on the main thread), so cancel/timeout target
that thread only. Parallel is for scripts that only create, analyse and close their own views.

bndb cache: a "bndb" run opens that file headlessly (any openable path -- a .bndb opens its
database, a raw binary is analysed once), caches the view and gives it to the script as `bv`.
It runs in the serialized lane (cached views are shared between runs, so they must not overlap)
but needs no usage lock: it touches no UI view. The plugin supplies the open/save/close hooks;
bndb_open is only called with the lane held, so at most one opener at a time, and save/close is
refused while a serialized script runs (see ScriptBridge._bndb_view for the ordering caveats).
`bvs` stays a NoUIViews placeholder: it means the UI views everywhere else, and a --bndb run
doesn't get them.

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
import fnmatch
import hmac
import http.client
import http.server
import io
import json
import os
import re
import secrets
import sys
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
JOB_KEEP = 100                  # finished jobs whose reply /result still gives
JOB_KEEP_SECONDS = 60 * 60
MAX_RESULT_WAIT = 60
DONE_STDOUT_KEEP = 64 * 1024    # stdout kept in a finished job's on-disk record
RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
# The request fields a job is spooled (and requeued) with.
JOB_FIELDS = ("code", "view", "main_thread", "parallel", "bndb", "args", "env", "timeout")
# run_ids of spooled jobs alive in this process: a Stop + Start must not call them lost.
_LIVE_RUN_IDS: set = set()

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


class OpenAbandoned(LookupError):
    """A --bndb open given up on (timeout or cancel) while it still runs in Binary Ninja."""


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
BNDB_NO_UI = ("--bndb runs headless on one cached view in the serialized lane: it can't be "
              "combined with --view, --main-thread or --parallel; no usage lock needed")
BNDB_BVS = ("`bvs` is not available for --bndb runs: they get one cached headless view as "
            "`bv`; the UI views (bv/bvs of the open tabs) need the serialized lane with the "
            "usage lock (bnrun --lock)")
BNDB_NO_HOOKS = ("cached --bndb views aren't available: the plugin didn't register its "
                 "open/save/close hooks (restart Binary Ninja with the current plugin)")


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
    except SystemExit as e:
        # sys.exit() ends the script like a command: no status (or 0) is success, a string
        # is the error message, anything else an exit status. No traceback either way.
        if e.code is None or e.code == 0:
            return {"ok": True, "stdout": out.getvalue(),
                    "result": _jsonable(namespace.get("result")), "error": None}
        message = e.code if isinstance(e.code, str) else "script exited with status %s" % e.code
        return {"ok": False, "stdout": out.getvalue(), "result": None,
                "error": message.rstrip("\n") + "\n"}
    except BaseException:  # noqa: BLE001 -- report everything
        return {"ok": False, "stdout": out.getvalue(), "result": None,
                "error": traceback.format_exc()}


def _install_globals(args: Optional[List[str]],
                     env: Optional[Dict[str, str]]) -> Callable[[], None]:
    """Serialized runs only (the lane is mutually exclusive): make `import sys` / `import os`
    inside the script see the forwarded argv/env. Namespace injection can't -- `import sys`
    fetches the real module from sys.modules. os.environ is updated, not replaced, so the
    script keeps the rest of Binary Ninja's environment. Returns the restore closure."""
    saved_argv = sys.argv
    if args:
        sys.argv = list(args)
    changed = {}
    for key, value in (env or {}).items():
        changed[key] = os.environ.get(key)       # None: the key was absent
        os.environ[key] = value

    def restore():
        sys.argv = saved_argv
        for key, old in changed.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old

    return restore


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
                 timeout: Optional[float], run_id: str, parallel: bool = False,
                 args: Optional[List[str]] = None, env: Optional[Dict[str, str]] = None,
                 bndb: Optional[str] = None):
        self.run_id = run_id
        self.session = session
        self.name = name
        self.code = code
        self.label = _label(code)
        self.main_thread = main_thread
        self.parallel = parallel
        self.args = args
        self.env = env
        self.bndb = bndb
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
        self.request: Dict[str, Any] = {}      # JOB_FIELDS, for the spool
        self.in_lane = False                    # holds run_mutex or a parallel slot
        self.reply: Optional[Dict[str, Any]] = None
        self.done = threading.Event()
        self.finished_at: Optional[float] = None
        self.opening_since: Optional[float] = None   # opening its --bndb (before the script)
        self.open_seconds = 0.0                      # spent opening; counts against timeout

    def state_name(self) -> str:
        if self.done.is_set():      # set after the reply and its on-disk record are written
            return "done"
        if not self.in_lane:
            return "queued"
        if self.started:
            return "running"
        return "opening" if self.opening_since else "starting"

    def status(self, full: bool = False) -> Dict[str, Any]:
        """full adds the run_id (which lets anyone cancel the run or fetch its reply)."""
        now = time.time()
        status = {"session": self.session, "name": self.name, "label": self.label,
                  "main_thread": self.main_thread, "parallel": self.parallel,
                  "timeout": self.timeout, "bndb": self.bndb, "job": self.run_id[:8],
                  "state": self.state_name(),
                  "queued_for": int((self.started or now) - self.queued_at),
                  "running_for": int((self.finished_at or now) - self.started)
                  if self.started else 0,
                  "opening_for": int(now - self.opening_since)
                  if self.opening_since and not self.started else 0,
                  "cancel_requested": self.cancel_by}
        if full:
            status["run_id"] = self.run_id
            if self.reply is not None:
                status["ok"] = bool(self.reply.get("ok"))
                status["error"] = self.reply.get("error")
                status["finished_ago"] = int(now - (self.finished_at or now))
        return status


def describe_run(status: Optional[Dict[str, Any]]) -> str:
    if not status:
        return "no script running"
    who = status.get("name") or (status.get("session") or "")[:8] or "no session"
    extra = [who]
    if status.get("job"):
        extra.append("job %s" % status["job"])
    if status.get("bndb"):
        extra.append("bndb %s" % status["bndb"])
    if status.get("parallel"):
        extra.append("parallel")
    if status.get("main_thread"):
        extra.append("main thread")
    if status.get("timeout"):
        extra.append("timeout %gs" % status["timeout"])
    if status.get("cancel_requested"):
        extra.append("cancel requested: %s" % status["cancel_requested"])
    if status.get("state") == "opening":
        return "opening the bndb for script %r for %ds (%s)" % (
            status["label"], status.get("opening_for", 0), ", ".join(extra))
    if status.get("state") == "queued":
        return "queued script %r for %ds (%s)" % (status["label"], status.get("queued_for", 0),
                                                  ", ".join(extra))
    return "running script %r for %ds (%s)" % (status["label"], status["running_for"],
                                               ", ".join(extra))


def describe_lost(record: Dict[str, Any]) -> str:
    who = record.get("name") or (record.get("session") or "")[:8] or "no session"
    extra = [who]
    if (record.get("request") or {}).get("bndb"):
        extra.append("bndb %s" % record["request"]["bndb"])
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(record.get("submitted_at") or 0))
    return "lost script %r (was %s; %s, submitted %s)" % (
        record.get("label"), record.get("state"), ", ".join(extra), when)


def describe_runs(reply: Dict[str, Any]) -> str:
    """All running scripts of a GET /run (or /lock) reply, one per line, then the queued ones;
    "no script running" only when nothing runs or waits in either lane (cached views and lost
    jobs add lines of their own). For scripts: bnrun --status --json."""
    runs = ([reply["running"]] if reply.get("running") else []) + list(reply.get("parallel") or [])
    queued, pqueued = reply.get("queued") or 0, reply.get("parallel_queued") or 0
    lines = [describe_run(r) for r in runs]
    if not lines and not (queued or pqueued):
        lines.append(describe_run(None))
    if queued or pqueued:
        lines.append("queued: %d serialized, %d parallel" % (queued, pqueued))
        lines += ["  " + describe_run(r) for r in reply.get("jobs") or []
                  if r.get("state") == "queued"]
    for hung in reply.get("bndb_hung") or []:
        lines.append("still opening: %s for %ds (its job stopped waiting; --bndb runs of it "
                     "are refused until the open finishes, then it is cached)"
                     % (hung["path"], hung["stuck_for"]))
    if reply.get("lost"):
        lines.append("lost: %d job%s whose Binary Ninja stopped before they finished "
                     "(bnrun --jobs; --requeue ID|all, --forget ID|all)"
                     % (reply["lost"], "" if reply["lost"] == 1 else "s"))
    cached = [v.get("name") + (" (unsaved changes)" if v.get("modified") else "")
              for v in reply.get("bndb") or []]
    if cached:
        lines.append("cached views: %s" % ", ".join(cached))
    return "\n".join(lines)


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
                 base_namespace: Optional[Dict[str, Any]] = None,
                 bndb_open: Optional[Callable[[str], Any]] = None,
                 bndb_save: Optional[Callable[[Any], bool]] = None,
                 bndb_close: Optional[Callable[[Any], None]] = None,
                 spool_dir: Optional[str] = None,
                 bndb_modified: Optional[Callable[[Any], bool]] = None):
        """namespace_factory(view) gives the UI views (bv, bvs, ...) and is only called for
        scripts allowed to use them; base_namespace (e.g. the binaryninja module) is what every
        script gets. max_parallel: int, or a callable read whenever a parallel script waits.
        bndb_open/bndb_save/bndb_close are the plugin's hooks for the cached headless views
        (open a path -> view or None, save a view -> bool, close a view); None disables
        the --bndb cache (see the module docstring); a --bndb script also gets save_bndb(),
        which saves its view with bndb_save within the job. spool_dir keeps queued and running jobs
        on disk so the next bridge can report them as lost, and finished ones for JOB_KEEP_SECONDS
        so it can still answer for them (None: no spool). bndb_modified(view) -> whether a cached
        view has unsaved changes; asked only from the job thread after each --bndb run and kept,
        so a status request never calls into Binary Ninja (None: unknown)."""
        self.token = token
        self.namespace_factory = namespace_factory
        self.base_namespace = dict(base_namespace or {})
        self.main_thread_runner = main_thread_runner or (lambda fn: fn())
        self.lock = lock or UsageLock()
        self.max_parallel = max_parallel
        self.bndb_open = bndb_open
        self.bndb_save = bndb_save
        self.bndb_close = bndb_close
        self.bndb_mutex = threading.Lock()
        self.bndb_views: Dict[str, Any] = {}    # abspath -> cached view handle
        self.bndb_hung: Dict[str, float] = {}   # abspath -> since: an abandoned, unfinished open
        self.bndb_modified = bndb_modified
        self.bndb_dirty: Dict[str, Optional[bool]] = {}   # abspath -> unsaved changes, as last seen
        self.run_mutex = threading.Lock()    # serialized lane: one script at a time
        self.state = threading.Lock()        # guards current, queued, parallel*, cancelled_ids
        self.slot_free = threading.Condition(self.state)   # a parallel slot or cancel happened
        self.current: Optional[Run] = None   # the serialized run
        self.queued = 0                      # serialized runs waiting for run_mutex
        self.parallel_runs: List[Run] = []   # parallel runs holding a slot
        self.parallel_queued = 0             # parallel runs waiting for a slot
        self.cancelled_ids: Dict[str, str] = {}   # run_id -> by, for runs not started yet
        self.jobs: Dict[str, Run] = {}       # run_id -> job, oldest first (guarded by state)
        self.spool_dir = spool_dir
        self.done_records: Dict[str, Dict[str, Any]] = self._load_done()   # previous bridges'
        self.lost: Dict[str, Dict[str, Any]] = self._load_lost()   # run_id -> spool record
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
                    return _reply_json(self, 200, {"ok": True, "jobs": True})
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
                if self.path not in ("/run", "/lock", "/unlock", "/cancel", "/bndb", "/result",
                                     "/jobs", "/lost"):
                    return _reply_json(self, 404, {"ok": False, "error": "not found"})
                req = self._body()
                if req is None:
                    return
                if self.path == "/lock":
                    return self._lock(req)
                if self.path == "/unlock":
                    return self._unlock(req)
                if self.path == "/bndb":
                    return self._bndb(req)
                if self.path == "/cancel":
                    return _reply_json(self, *bridge.cancel(
                        req.get("session"), req.get("run_id"), bool(req.get("force"))))
                if self.path == "/result":
                    try:
                        return _reply_json(self, *bridge.result(req.get("run_id"),
                                                                req.get("wait")))
                    except (BrokenPipeError, ConnectionResetError):
                        return      # the poller went away (bnrun killed); the job goes on
                if self.path == "/jobs":
                    return _reply_json(self, 200, bridge.jobs_of(req.get("session")))
                if self.path == "/lost":
                    return _reply_json(self, *bridge.lost_action(req))
                refusal, params = bridge.check_run(req)
                if refusal:
                    return _reply_json(self, *refusal)
                if req.get("async"):
                    run = bridge.submit(params)
                    return _reply_json(self, 202, {"ok": True, "run_id": run.run_id,
                                                   "job": run.run_id[:8],
                                                   "state": run.state_name()})
                reply = bridge.execute(**params)
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

            def _bndb(self, req):
                action = req.get("action")
                if action not in ("save", "close"):
                    return _reply_json(self, 400, {"ok": False, "error": "bad request: action "
                                                   'must be "save" or "close"'})
                allowed, status = bridge.lock.check_use(req.get("session"))
                if not allowed:
                    return _reply_json(self, 423, {"ok": False, "lock": status,
                                                   "error": "Binary Ninja is " + describe(status)})
                if bridge.running() is not None:
                    return _reply_json(self, 409, {"ok": False,
                                                   "running": bridge.running(),
                                                   "error": "a script is running; retry when "
                                                            "it finishes"})
                path = req.get("path")
                if path is not None and not isinstance(path, str):
                    return _reply_json(self, 400, {"ok": False, "error": "bad request: path "
                                                   "must be a string"})
                targets = bridge.bndb_paths(path)
                if not targets and path is not None:
                    return _reply_json(self, 404, {"ok": False, "error": "not cached: %s"
                                                   % path})
                results = (bridge.bndb_save_views(targets) if action == "save"
                           else bridge.bndb_close_views(targets))
                _reply_json(self, 200, {"ok": all(r["ok"] for r in results),
                                        "action": action, "results": results})

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.httpd.server_address[1]

    def check_run(self, req: Dict[str, Any]) -> Tuple[Optional[Tuple[int, Dict[str, Any]]],
                                                      Dict[str, Any]]:
        """(refusal as (HTTP status, reply) or None, execute()'s keyword arguments) for a /run
        request (also used to requeue a lost job)."""
        def bad(text, status=400, **extra):
            return (status, dict({"ok": False, "error": text}, **extra)), {}
        code = req.get("code")
        if not isinstance(code, str):
            return bad("bad request: code must be a string")
        view, main_thread = req.get("view"), bool(req.get("main_thread"))
        parallel = bool(req.get("parallel"))
        bndb = req.get("bndb")
        if parallel and (main_thread or view):
            return bad("bad request: " + ("parallel scripts can't run on the main thread "
                                          "(--parallel with --main-thread)"
                                          if main_thread else PARALLEL_NO_UI))
        if bndb is not None:
            # bndb runs use their own cached headless view: no UI, serialized only
            # (cached views are shared, so runs using them must not overlap).
            if not isinstance(bndb, str) or not bndb.strip():
                return bad("bad request: bndb must be a path string")
            if view or main_thread or parallel:
                return bad("bad request: " + BNDB_NO_UI)
            if not os.path.isfile(bndb):
                return bad("bad request: --bndb file not found: %s" % bndb)
            if self.bndb_open is None:
                return bad(BNDB_NO_HOOKS)
        args = req.get("args")
        if args is not None and (not isinstance(args, list)
                                 or not all(isinstance(a, str) for a in args)):
            return bad("bad request: args must be a list of strings (argv[0] plus the "
                       "script's arguments)")
        env = req.get("env")
        if env is not None and (not isinstance(env, dict)
                                or not all(isinstance(k, str) and isinstance(v, str)
                                           for k, v in env.items())):
            return bad("bad request: env must be an object mapping names to string values")
        allowed, status = self.lock.check_use(req.get("session"))
        if not allowed:
            return bad("Binary Ninja is " + describe(status), 423, lock=status)
        if (view or main_thread) and not self.lock.holds(req.get("session")):
            return bad(NEED_LOCK, 409, lock=status)
        timeout = req.get("timeout")
        if timeout is not None and (isinstance(timeout, bool) or
                                    not isinstance(timeout, (int, float)) or timeout <= 0):
            return bad("bad request: timeout must be a positive number of seconds")
        run_id = req.get("run_id")
        if run_id is not None and not (isinstance(run_id, str) and RUN_ID.fullmatch(run_id)):
            return bad("bad request: run_id must be 1-64 letters, digits, - or _")
        return None, {"code": code, "view": view, "main_thread": main_thread,
                      "session": req.get("session"), "timeout": timeout, "run_id": run_id,
                      "name": str(req.get("name") or ""), "parallel": parallel,
                      "args": args, "env": env, "bndb": bndb}

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
        bndb, hung = self.bndb_status(), self.bndb_hung_status()
        with self.state:
            return {"running": self.current.status() if self.current else None,
                    "queued": self.queued,
                    "parallel": [r.status() for r in self.parallel_runs],
                    "parallel_queued": self.parallel_queued,
                    "max_parallel": self.limit(),
                    "bndb": bndb,
                    "bndb_hung": hung,
                    "jobs": [r.status() for r in self.jobs.values() if r.reply is None],
                    "lost": len(self.lost)}

    def execute(self, code: str, view: Optional[str], main_thread: bool,
                session: Optional[str] = None, timeout: Optional[float] = None,
                run_id: Optional[str] = None, name: str = "",
                parallel: bool = False, args: Optional[List[str]] = None,
                env: Optional[Dict[str, str]] = None,
                bndb: Optional[str] = None) -> Dict[str, Any]:
        """Run a script to its end and return the /run reply."""
        if parallel and main_thread:
            return {"ok": False, "stdout": "", "result": None,
                    "error": "parallel scripts can't run on the main thread"}
        if bndb and (view or main_thread or parallel):
            return {"ok": False, "stdout": "", "result": None, "error": BNDB_NO_UI}
        run, _ = self._new_run(code, view, main_thread, session, timeout, run_id, name,
                               parallel, args, env, bndb, reuse=False)
        return self._drive(run)

    def submit(self, params: Dict[str, Any]) -> Run:
        """Start a job (execute()'s keyword arguments, checked by check_run) in a thread of its
        own and return it at once; a run_id the bridge already knows returns that job."""
        run, created = self._new_run(reuse=True, **params)
        if created:
            threading.Thread(target=self._drive, args=(run,), daemon=True,
                             name="bn-script-bridge job %s" % run.run_id[:8]).start()
        return run

    def _new_run(self, code: str, view: Optional[str], main_thread: bool,
                 session: Optional[str], timeout: Optional[float], run_id: Optional[str],
                 name: str, parallel: bool, args: Optional[List[str]],
                 env: Optional[Dict[str, str]], bndb: Optional[str],
                 reuse: bool) -> Tuple[Run, bool]:
        """(the job, whether it is new): registered and spooled; with reuse an existing job of
        the same run_id is returned instead."""
        with self.state:
            if reuse and run_id in self.jobs:
                return self.jobs[run_id], False
            run = Run(code, session if isinstance(session, str) else None, name, main_thread,
                      float(timeout) if timeout else None, run_id or secrets.token_hex(16),
                      parallel, args=args, env=env, bndb=bndb)
            run.request = {"code": code, "view": view, "main_thread": main_thread,
                           "parallel": parallel, "bndb": bndb, "args": args, "env": env,
                           "timeout": run.timeout}
            self._prune_jobs()
            self.jobs.pop(run.run_id, None)
            self.jobs[run.run_id] = run
        self._spool(run)
        return run, True

    def _prune_jobs(self) -> None:
        """Drop finished jobs past JOB_KEEP_SECONDS or beyond the JOB_KEEP newest. Holds state."""
        now = time.time()
        finished = [r for r in self.jobs.values() if r.finished_at is not None]
        for i, run in enumerate(finished):
            if now - run.finished_at > JOB_KEEP_SECONDS or len(finished) - i > JOB_KEEP:
                del self.jobs[run.run_id]

    def _drive(self, run: Run) -> Dict[str, Any]:
        """Run the job through its lane; record the reply, wake /result waiters, unspool."""
        reply = None
        try:
            reply = (self._execute_parallel(run, run.request["view"]) if run.parallel
                     else self._execute_serialized(run, run.request["view"]))
            return reply
        finally:
            with self.state:
                run.reply = reply if reply is not None else {
                    "ok": False, "stdout": "", "result": None,
                    "error": "the bridge failed to run the script (see Binary Ninja's log)"}
                run.finished_at = time.time()
            self._write_done(run)       # first: a crash in between leaves done, not lost
            self._unspool(run)
            run.done.set()              # last: a waiter told "done" sees the disk state too

    def _enter_lane(self, run: Run) -> None:
        """Called with state held once the run holds its lane."""
        run.in_lane = True
        if run.run_id in self.cancelled_ids:
            run.cancel_by = self.cancelled_ids.pop(run.run_id)

    def _execute_serialized(self, run: Run, view: Optional[str]) -> Dict[str, Any]:
        with self.state:
            self.queued += 1
        waiting = True
        try:
            with self.run_mutex:
                with self.state:
                    self.queued -= 1
                    waiting = False
                    self.current = run
                    self._enter_lane(run)
                self._spool(run)
                try:
                    return self._execute_run(run, run.code, view)
                finally:
                    if run.timer is not None:
                        run.timer.cancel()
                    if run.bndb:
                        self._refresh_dirty(run.bndb)   # still holding the lane
                    with self.state:
                        self.current = None
                        run.finished = True
        finally:
            if waiting:
                with self.state:
                    self.queued -= 1

    # -- job results, spool and lost jobs

    def result(self, run_id: Any, wait: Any = None) -> Tuple[int, Dict[str, Any]]:
        """POST /result: (HTTP status, reply) for a job, waiting up to `wait` seconds for it."""
        if not isinstance(run_id, str):
            return 400, {"ok": False, "error": "bad request: run_id must be a string"}
        try:
            wait = min(max(float(wait or 0), 0.0), MAX_RESULT_WAIT)
        except (TypeError, ValueError):
            return 400, {"ok": False, "error": "bad request: wait must be a number of seconds"}
        with self.state:
            run = self.jobs.get(run_id)
            lost = self.lost.get(run_id)
        if run is None:
            if lost is not None:
                return 200, {"ok": True, "state": "lost", "lost": self._lost_summary(lost)}
            done = self.done_records.get(run_id)
            if done is not None and time.time() - (done.get("finished_at") or 0) \
                    <= JOB_KEEP_SECONDS:
                return 200, {"ok": True, "state": "done", "run": self._done_status(done),
                             "reply": done.get("reply"), "previous_bridge": True}
            return 404, {"ok": False, "state": "unknown",
                         "error": "no such job (unknown, or finished over %d minutes ago)"
                                  % (JOB_KEEP_SECONDS // 60)}
        run.done.wait(wait)
        with self.state:
            return 200, {"ok": True, "state": run.state_name(), "run": run.status(full=True),
                         "reply": run.reply}

    def jobs_of(self, session: Any) -> Dict[str, Any]:
        """POST /jobs: this session's jobs and every lost one, with run_ids."""
        session = session if isinstance(session, str) and session else None
        with self.state:
            jobs = [self._done_status(rec) for rec in self.done_records.values()
                    if rec.get("session") == session and rec["run_id"] not in self.jobs]
            jobs += [r.status(full=True) for r in self.jobs.values() if r.session == session]
            lost = [self._lost_summary(rec) for rec in self.lost.values()]
        return {"ok": True, "jobs": jobs, "lost": lost}

    def lost_action(self, req: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """POST /lost: requeue or forget lost jobs (see the module docstring)."""
        action, ids = req.get("action"), req.get("run_ids")
        if action not in ("requeue", "forget"):
            return 400, {"ok": False, "error": 'bad request: action must be "requeue" or '
                                               '"forget"'}
        with self.state:
            if ids == "all":
                ids = list(self.lost)
            elif not (isinstance(ids, list) and all(isinstance(i, str) for i in ids)):
                return 400, {"ok": False, "error": 'bad request: run_ids must be a list of '
                                                   'strings or "all"'}
            # Claimed (taken out of self.lost) at once, so two concurrent requeues can't both
            # rerun one job; a record that isn't used goes back.
            records = {i: self.lost.pop(i, None) for i in dict.fromkeys(ids)}
        results = []
        for old_id, rec in records.items():
            entry: Dict[str, Any] = {"run_id": old_id}
            used = False
            if rec is None:
                entry.update(ok=False, error="not a lost job (or another request is handling "
                                             "it)")
            elif action == "forget":
                used = True
                entry.update(ok=True)
            elif rec.get("state") != "queued" and not req.get("force"):
                entry.update(ok=False, error="was %s when its bridge died: it may have applied "
                             "part of its edits (or be what wedged Binary Ninja); check, then "
                             "requeue with force" % rec.get("state"))
            else:
                refusal, params = self.check_run(dict(rec.get("request") or {},
                                                      session=req.get("session"),
                                                      name=req.get("name") or rec.get("name")))
                if refusal:
                    entry.update(ok=False, status=refusal[0], error=refusal[1].get("error"))
                else:
                    used = True
                    run = self.submit(params)
                    entry.update(ok=True, new_run_id=run.run_id, label=run.label)
            if used:
                self._drop_lost(old_id)
            elif rec is not None:
                with self.state:
                    self.lost[old_id] = rec
            results.append(entry)
        return 200, {"ok": all(r["ok"] for r in results), "action": action,
                     "results": results}

    @staticmethod
    def _lost_summary(rec: Dict[str, Any]) -> Dict[str, Any]:
        out = {k: rec.get(k) for k in ("run_id", "state", "session", "name", "label",
                                       "submitted_at", "started_at")}
        out["request"] = {"bndb": (rec.get("request") or {}).get("bndb")}
        return out

    def _spool_file(self, run_id: str, lost: bool = False, done: bool = False) -> str:
        assert self.spool_dir and RUN_ID.fullmatch(run_id)
        sub = "lost" if lost else "done" if done else ""
        return os.path.join(self.spool_dir, sub, run_id + ".json")

    @staticmethod
    def _write_json(path: str, record: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(record, f)
        os.replace(tmp, path)

    def _write_done(self, run: Run) -> None:
        """Keep a finished job's outcome on disk for JOB_KEEP_SECONDS, so a bridge started
        after a crash can still say it finished (and how) instead of "unknown"."""
        if not self.spool_dir or not RUN_ID.fullmatch(run.run_id):
            return
        reply = dict(run.reply or {})
        stdout = reply.get("stdout") or ""
        if len(stdout) > DONE_STDOUT_KEEP:
            reply["stdout"] = stdout[:DONE_STDOUT_KEEP] + (
                "\n[bridge: output truncated to %d characters after a Binary Ninja restart]\n"
                % DONE_STDOUT_KEEP)
        try:
            json.dumps(reply.get("result"))
        except (TypeError, ValueError):
            reply["result"] = repr(reply.get("result"))
        record = {"run_id": run.run_id, "session": run.session, "name": run.name,
                  "label": run.label, "bndb": run.bndb, "submitted_at": run.queued_at,
                  "finished_at": run.finished_at, "reply": reply}
        try:
            self._write_json(self._spool_file(run.run_id, done=True), record)
            self._prune_done_files()
        except OSError:
            pass    # best effort, like the spool

    def _prune_done_files(self) -> None:
        done_dir = os.path.join(self.spool_dir, "done")
        cutoff = time.time() - JOB_KEEP_SECONDS
        for fname in os.listdir(done_dir):
            path = os.path.join(done_dir, fname)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass

    def _load_done(self) -> Dict[str, Dict[str, Any]]:
        """Finished-job records younger than JOB_KEEP_SECONDS, from earlier bridges."""
        if not self.spool_dir:
            return {}
        done_dir = os.path.join(self.spool_dir, "done")
        records = {}
        try:
            os.makedirs(done_dir, mode=0o700, exist_ok=True)
            self._prune_done_files()
            names = os.listdir(done_dir)
        except OSError:
            return {}
        for fname in names:
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(done_dir, fname)) as f:
                    rec = json.load(f)
            except (OSError, ValueError):
                continue
            if isinstance(rec, dict) and RUN_ID.fullmatch(str(rec.get("run_id"))):
                records[rec["run_id"]] = rec
        return records

    @staticmethod
    def _done_status(rec: Dict[str, Any]) -> Dict[str, Any]:
        """A previous bridge's finished job, shaped like Run.status(full=True)."""
        reply = rec.get("reply") or {}
        now = time.time()
        return {"run_id": rec["run_id"], "job": rec["run_id"][:8], "session": rec.get("session"),
                "name": rec.get("name"), "label": rec.get("label"), "bndb": rec.get("bndb"),
                "state": "done", "ok": bool(reply.get("ok")), "error": reply.get("error"),
                "finished_ago": int(now - (rec.get("finished_at") or now)),
                "previous_bridge": True}

    def _spool(self, run: Run) -> None:
        """Write (or rewrite, once it is in its lane) the job's spool record."""
        if not self.spool_dir or not RUN_ID.fullmatch(run.run_id) or run.reply is not None:
            return
        _LIVE_RUN_IDS.add(run.run_id)
        record = {"run_id": run.run_id, "state": "running" if run.in_lane else "queued",
                  "session": run.session, "name": run.name, "label": run.label,
                  "submitted_at": run.queued_at, "started_at": time.time() if run.in_lane
                  else None, "pid": os.getpid(), "request": run.request}
        try:
            self._write_json(self._spool_file(run.run_id), record)
        except OSError:
            pass    # the spool is best effort; the job runs anyway
        if run.reply is not None:   # finished while being written
            self._unspool(run)

    def _unspool(self, run: Run) -> None:
        _LIVE_RUN_IDS.discard(run.run_id)
        if self.spool_dir and RUN_ID.fullmatch(run.run_id):
            try:
                os.remove(self._spool_file(run.run_id))
            except OSError:
                pass

    def _load_lost(self) -> Dict[str, Dict[str, Any]]:
        """Move the spool records no live job owns into lost/ and read them all."""
        if not self.spool_dir:
            return {}
        lost_dir = os.path.join(self.spool_dir, "lost")
        try:
            os.makedirs(lost_dir, mode=0o700, exist_ok=True)
            names = os.listdir(self.spool_dir)
        except OSError:
            return {}
        for fname in names:
            run_id = fname[:-5] if fname.endswith(".json") else None
            if run_id and RUN_ID.fullmatch(run_id) and run_id in self.done_records:
                try:        # finished; the crash came before its spool file went
                    os.remove(os.path.join(self.spool_dir, fname))
                except OSError:
                    pass
            elif run_id and RUN_ID.fullmatch(run_id) and run_id not in _LIVE_RUN_IDS:
                try:
                    os.replace(os.path.join(self.spool_dir, fname),
                               os.path.join(lost_dir, fname))
                except OSError:
                    pass
        lost = {}
        for fname in sorted(os.listdir(lost_dir)):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(lost_dir, fname)) as f:
                    rec = json.load(f)
                if isinstance(rec, dict) and RUN_ID.fullmatch(str(rec.get("run_id"))):
                    lost[rec["run_id"]] = rec
            except (OSError, ValueError):
                continue
        return dict(sorted(lost.items(), key=lambda kv: kv[1].get("submitted_at") or 0))

    def _drop_lost(self, run_id: str) -> None:
        with self.state:
            self.lost.pop(run_id, None)
        try:
            os.remove(self._spool_file(run_id, lost=True))
        except OSError:
            pass

    def _execute_parallel(self, run: Run, view: Optional[str]) -> Dict[str, Any]:
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
            self._enter_lane(run)
        self._spool(run)
        try:
            return self._execute_run(run, run.code, view)
        finally:
            if run.timer is not None:
                run.timer.cancel()
            with self.state:
                self.parallel_runs.remove(run)
                run.finished = True
                self.slot_free.notify_all()

    def _namespace(self, run: Run, view: Optional[str]) -> Dict[str, Any]:
        """The script's globals: the UI views only for a serialized lock holder, or the cached
        headless view for a bndb run. Raises LookupError (bad view filter, or the bndb couldn't
        be opened) or UIViewsUnavailable (view not allowed)."""
        if run.bndb:
            handle = self._bndb_view(run.bndb, run)
            return dict(self.base_namespace, bv=handle, bvs=NoUIViews("bvs", BNDB_BVS),
                        save_bndb=lambda: self._save_in_run(run.bndb, handle))
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

    def _bndb_view(self, path: str, run: Optional[Run] = None) -> Any:
        """The cached headless view for `path`, opening it if needed.

        The open runs outside bndb_mutex (it can take minutes), which is safe because bndb runs
        execute only in the serialized lane (enforced by the combo checks in the handler, in
        execute and in bnrun): at most one opener, and management save/close is refused while a
        serialized script runs. A close that passed that 409 gate just before a queued bndb run
        starts can miss the run's newly opened view -- harmless, the entry stays cached.

        The open itself is a native call nothing can interrupt (it can deadlock in Binary
        Ninja's core, e.g. against another handle on the same database), so it runs in a
        thread of its own while the run waits for it, watching its --timeout (which the open
        counts against) and --cancel. Either one abandons the open: the run ends as cancelled
        and frees the lane, the path goes into bndb_hung (further runs on it are refused at
        once instead of piling up behind it), and an open that does finish later is cached."""
        key = os.path.abspath(path)
        with self.bndb_mutex:
            if key in self.bndb_views:
                return self.bndb_views[key]
            if key in self.bndb_hung:
                raise LookupError(
                    "%s is still opening inside Binary Ninja (for %ds; the job that started "
                    "it stopped waiting); refusing a second open. Retry when --status no longer "
                    "shows it (its view is then cached); if it never finishes, it is stuck "
                    "(e.g. starved by another heavy load): restart Binary Ninja (bnrestart)"
                    % (key, time.time() - self.bndb_hung[key]))
        box: Dict[str, Any] = {}
        done = threading.Event()

        def opener():
            try:
                box["handle"] = self.bndb_open(key) if self.bndb_open else None
            except Exception as e:  # noqa: BLE001 -- reported by the waiting run
                box["error"] = e
            finally:
                with self.bndb_mutex:
                    done.set()
                    if key in self.bndb_hung:       # its run gave up: keep what it opened
                        del self.bndb_hung[key]
                        if box.get("handle") is not None:
                            self.bndb_views[key] = box["handle"]

        started = time.time()
        if run is not None:
            run.opening_since = started
        threading.Thread(target=opener, daemon=True,
                         name="bn-script-bridge open %s" % os.path.basename(key)).start()
        try:
            while not done.wait(0.2):
                why = None
                if run is not None and run.cancel_by is not None:
                    why = run.cancel_by
                elif run is not None and run.timeout and time.time() - started >= run.timeout:
                    why = run.cancel_by = "timeout after %gs (opening the bndb)" % run.timeout
                if why is None:
                    continue
                with self.bndb_mutex:
                    if not done.is_set():
                        self.bndb_hung[key] = started
                        raise OpenAbandoned(
                            "abandoned opening %s after %ds (%s); the open goes on inside "
                            "Binary Ninja (a native call can't be interrupted) and its view is "
                            "cached when it finishes; until then further --bndb runs of it are "
                            "refused (--status: still opening). If it never finishes, it is "
                            "stuck -- starved by another heavy load, or the file is open "
                            "elsewhere too: restart Binary Ninja" % (key, time.time() - started, why))
        finally:
            if run is not None:
                run.open_seconds = time.time() - started
        if "error" in box:
            raise LookupError("couldn't open %s: %s" % (key, box["error"])) from box["error"]
        if box.get("handle") is None:
            raise LookupError("couldn't open %s" % key)
        with self.bndb_mutex:
            self.bndb_views[key] = box["handle"]
        return box["handle"]

    def _save_in_run(self, path: str, handle: Any) -> str:
        """save_bndb() for a --bndb script: save its cached view into the .bndb within the same
        job (so a client dying after the edits can't orphan the save); the path, or raises."""
        if not self.bndb_save or not self.bndb_save(handle):
            raise RuntimeError("saving %s failed" % os.path.abspath(path))
        return os.path.abspath(path)     # the run's end refreshes bndb_dirty

    def bndb_hung_status(self) -> List[Dict[str, Any]]:
        """Abandoned opens still stuck inside Binary Ninja, for GET /run."""
        now = time.time()
        with self.bndb_mutex:
            return [{"path": p, "stuck_for": int(now - since)}
                    for p, since in sorted(self.bndb_hung.items())]

    def bndb_status(self) -> List[Dict[str, Any]]:
        """The cached headless views, for GET /run."""
        with self.bndb_mutex:
            return [{"path": path, "name": os.path.basename(path),
                     "modified": self.bndb_dirty.get(path)}
                    for path in sorted(self.bndb_views)]

    def _refresh_dirty(self, path: str) -> None:
        """Ask the plugin whether the cached view of `path` has unsaved changes (job thread
        only, see __init__); None when unknown."""
        key = os.path.abspath(path)
        with self.bndb_mutex:
            handle = self.bndb_views.get(key)
        if handle is None:
            return
        try:
            dirty = bool(self.bndb_modified(handle)) if self.bndb_modified else None
        except Exception:  # noqa: BLE001 -- unknown, not fatal
            dirty = None
        with self.bndb_mutex:
            if key in self.bndb_views:
                self.bndb_dirty[key] = dirty

    def bndb_paths(self, path: Optional[str]) -> List[str]:
        """Cached abspaths matching `path` (None = all; no match = empty)."""
        with self.bndb_mutex:
            if path is None:
                return list(self.bndb_views)
            return [p for p in self.bndb_views if p == os.path.abspath(path)]

    def bndb_save_views(self, paths: List[str]) -> List[Dict[str, Any]]:
        """Save each cached view; per-entry errors are reported, not raised."""
        results = []
        for path in paths:
            with self.bndb_mutex:
                handle = self.bndb_views.get(path)
            if handle is None:
                results.append({"path": path, "ok": False, "error": "not cached"})
                continue
            try:
                ok = bool(self.bndb_save(handle))
            except Exception as e:  # noqa: BLE001 -- report, don't fail the others
                results.append({"path": path, "ok": False, "error": repr(e)})
                continue
            if ok:
                with self.bndb_mutex:
                    if path in self.bndb_views:
                        self.bndb_dirty[path] = False
            results.append({"path": path, "ok": True} if ok
                           else {"path": path, "ok": False, "error": "save failed"})
        return results

    def bndb_close_views(self, paths: List[str]) -> List[Dict[str, Any]]:
        """Close each cached view (close exceptions are ignored) and drop it from the cache."""
        results = []
        for path in paths:
            with self.bndb_mutex:
                handle = self.bndb_views.get(path)
            if handle is None:
                results.append({"path": path, "ok": False, "error": "not cached"})
                continue
            try:
                if self.bndb_close:
                    self.bndb_close(handle)
                ok = True
            except Exception:  # noqa: BLE001 -- a close failure still drops the entry
                ok = True
            with self.bndb_mutex:
                self.bndb_views.pop(path, None)
                self.bndb_dirty.pop(path, None)
            results.append({"path": path, "ok": ok})
        return results

    def bndb_close_all(self) -> None:
        """Close every cached view (used by stop()); close failures are ignored."""
        with self.bndb_mutex:
            views = list(self.bndb_views.items())
            self.bndb_views.clear()
            self.bndb_dirty.clear()
        for _path, handle in views:
            try:
                if self.bndb_close:
                    self.bndb_close(handle)
            except Exception:  # noqa: BLE001 -- stopping anyway
                pass

    def _execute_run(self, run: Run, code: str, view: Optional[str]) -> Dict[str, Any]:
        """Run a script that holds its lane (run_mutex or a parallel slot)."""
        if run.cancel_by is not None:
            return self._cancelled(run, "")
        try:
            namespace = self._namespace(run, view)
        except OpenAbandoned as e:
            return dict(self._cancelled(run, ""), detail=str(e))
        except (LookupError, UIViewsUnavailable) as e:
            return {"ok": False, "stdout": "", "result": None, "error": str(e)}
        # Bare names for every run (race-free); parallel runs don't get the sys.argv /
        # os.environ swap below -- they overlap, so a global swap would corrupt it.
        namespace["argv"] = list(run.args or [])
        namespace["env"] = dict(run.env or {})
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
            restore: Optional[Callable[[], None]] = None
            try:
                if not begun:
                    begun = True
                    run.thread = threading.get_ident()
                    run.started = time.time()
                    run.executing = True
                    if run.cancel_by is not None:   # cancelled before it began
                        raise ScriptCancelled()
                    if run.timeout:
                        run.timer = threading.Timer(max(run.timeout - run.open_seconds, 0.001),
                                                    self._on_timeout, (run,))
                        run.timer.daemon = True
                        run.timer.start()
                    if not run.parallel and (run.args or run.env):
                        # Serialized only: the lane is mutually exclusive, so the process-global
                        # sys.argv / os.environ swap can't collide with another run.
                        restore = _install_globals(run.args, run.env)
                    result = run_script(code, namespace, out)
                run.executing = False
                while run.sending:          # a canceller is deciding: wait for its verdict
                    time.sleep(0.001)
                if run.exc_sent:            # clear a cancel that hasn't fired yet
                    set_async_exc(run.thread, None)
                break
            except ScriptCancelled:
                continue
            finally:
                if restore is not None:
                    restore()
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

# bn_open_item_open is answered by the proxy when the plugin's open_guard rejects the path
# (a .bndb opened twice gets two database handles, and then saves fail on both lanes).
OPEN_TOOL_NAME = "bn_open_item_open"

# The default scriptbridge.mcpTools allowlist: comma-separated fnmatch globs over the tool
# names of Binary Ninja's MCP server. Empty means no filter (every upstream tool passes).
# Curated for the interactive RE loop (decompile, xrefs, rename, search, lists) -- see the
# skill for the bnrun alternative for everything else. Owner tools always pass.
DEFAULT_MCP_TOOLS = ",".join([
    "bn_analysis_status",
    "bn_binary_view_info",
    "bn_binary_view_list",
    "bn_binary_view_get_active",
    "bn_comment_set",
    "bn_data_variable_list",
    "bn_function_callees",
    "bn_function_callers",
    "bn_function_decompile",
    "bn_function_disassembly",
    "bn_function_il",
    "bn_function_info",
    "bn_function_list",
    "bn_function_search",
    "bn_function_xrefs_from",
    "bn_function_xrefs_to",
    "bn_memory_read",
    "bn_open_item_list",
    "bn_open_item_open",
    "bn_open_item_save",
    "bn_string_list",
    "bn_symbol_list",
    "bn_symbol_list_at",
    "bn_symbol_rename",
    "bn_type_info",
    "bn_type_search",
])
TOOL_NOT_ALLOWED = "tool %s is not in the scriptbridge.mcpTools allowlist"


def tool_allowed(name: str, patterns: Optional[Union[str, List[str]]]) -> bool:
    """True if `name` matches the comma-separated (or list of) fnmatch globs; None or ""
    means no filter. Owner tools always pass."""
    if name in OWNER_TOOL_NAMES:
        return True
    if not patterns:
        return True
    if isinstance(patterns, str):
        patterns = [p.strip() for p in patterns.split(",") if p.strip()]
    return any(fnmatch.fnmatch(name, p) for p in patterns)


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


def add_owner_tools(message: Any, patterns: Optional[Union[str, List[str]]] = None) -> Any:
    """A tools/list response with the allowlist applied and OWNER_TOOLS appended (other
    messages unchanged). Owner tools always pass the allowlist; upstream order is kept."""
    tools = ((message or {}).get("result") or {}).get("tools") if isinstance(message, dict) \
        else None
    if isinstance(tools, list):
        if patterns is not None:
            tools[:] = [t for t in tools
                        if not isinstance(t, dict)
                        or tool_allowed(t.get("name") or "", patterns)]
        if not any(t.get("name") in OWNER_TOOL_NAMES for t in tools):
            tools.extend(OWNER_TOOLS)
    return message


class McpProxy:
    """Lock-aware pass-through to Binary Ninja's MCP server (see the module docstring)."""

    def __init__(self, lock: UsageLock, upstream_port: int, port: int = 0,
                 upstream_host: str = "127.0.0.1", timeout: float = 600.0,
                 upstream_token: Optional[str] = None,
                 allowed_tools: Union[str, List[str], Callable[[], Union[str, List[str]]],
                                      None] = None,
                 open_guard: Optional[Callable[[str], Optional[str]]] = None):
        self.lock = lock
        # Optional plugin hook for bn_open_item_open: open_guard(path) -> refusal message
        # (answered by the proxy) or None to let the call through. The plugin uses it to
        # keep one handle per file: a .bndb opened twice (cached --bndb view + MCP item,
        # say) has two database handles, and then saves fail on both lanes until one of
        # them closes without saving.
        self.open_guard = open_guard
        # Where Binary Ninja's MCP server listens and its bearer token. The plugin moves that
        # server to a random port with a random token that only the proxy knows, so MCP
        # clients can't bypass the lock; clients' own Authorization headers are dropped.
        # Both can change while the proxy runs (the plugin sets them after restarting it).
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.upstream_token = upstream_token
        # The scriptbridge.mcpTools allowlist (see tool_allowed): globs, or a callable
        # returning them, read live for every tools/list and tools/call.
        self.allowed_tools = allowed_tools
        proxy = self

        def _patterns() -> Optional[Union[str, List[str]]]:
            value = proxy.allowed_tools() if callable(proxy.allowed_tools) \
                else proxy.allowed_tools
            return value if value else None   # "" -> no filter

        self._patterns = _patterns

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
                """Relay a tools/list reply (JSON or SSE) with the allowlist applied and
                OWNER_TOOLS appended."""
                data = resp.read()
                ctype = resp.getheader("Content-Type") or ""
                if "text/event-stream" in ctype:
                    lines = data.decode().split("\n")
                    for i, line in enumerate(lines):
                        if line.startswith("data:"):
                            lines[i] = "data: " + json.dumps(add_owner_tools(
                                json.loads(line[5:].strip()), proxy._patterns()))
                    data = "\n".join(lines).encode()
                else:
                    data = json.dumps(add_owner_tools(json.loads(data),
                                                      proxy._patterns())).encode()
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

            def _open_guard_call(self, message: Any) -> bool:
                """Refuse bn_open_item_open calls the plugin's open_guard rejects. True
                if the request was answered here (see McpProxy.__init__)."""
                if proxy.open_guard is None or not _tool_calls(message):
                    return False
                items = message if isinstance(message, list) else [message]
                refusals = []
                for m in items:
                    if not isinstance(m, dict):
                        continue
                    params = m.get("params") or {}
                    if params.get("name") != OPEN_TOOL_NAME:
                        continue
                    path = (params.get("arguments") or {}).get("path")
                    if not isinstance(path, str):
                        continue
                    refusal = proxy.open_guard(path)
                    if refusal:
                        refusals.append(str(refusal))
                if not refusals:
                    return False
                reply = blocked_reply(message, "; ".join(dict.fromkeys(refusals)))
                if reply is None:   # notifications only: nothing to answer
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    _reply_json(self, 200, reply)
                return True

            def _gate(self, body: bytes) -> bool:
                """True if the request was answered here (owner tools, a filtered-out tool,
                or blocked by the lock)."""
                try:
                    message = json.loads(body)
                except ValueError:
                    return False
                if self._owner_call(message):
                    return True
                if self._open_guard_call(message):
                    return True
                if not _tool_calls(message):
                    return False
                patterns = proxy._patterns()
                if patterns:
                    items = message if isinstance(message, list) else [message]
                    called = {str((m.get("params") or {}).get("name"))
                              for m in items if isinstance(m, dict)}
                    not_allowed = sorted(n for n in called
                                         if n and not tool_allowed(n, patterns))
                    if not_allowed:
                        reply = blocked_reply(
                            message, "; ".join(TOOL_NOT_ALLOWED % n for n in not_allowed))
                        if reply is None:
                            self.send_response(202)
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                        else:
                            _reply_json(self, 200, reply)
                        return True
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
