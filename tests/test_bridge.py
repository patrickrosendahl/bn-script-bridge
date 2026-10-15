"""Host tests for the script bridge: bridge_server.py and the bnrun client, without Binary Ninja."""

import enum
import http.client
import http.server
import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import bridge_server  # noqa: E402

BNRUN = os.path.join(ROOT, "bnrun")
BNANNOTATE = os.path.join(ROOT, "bnannotate")


class FakeView:
    def __init__(self, filename):
        self.file = type("File", (), {"filename": filename})()


VIEWS = [FakeView("/x/DM365_AllegroIndoor_debug.bndb"), FakeView("/x/cx20707.ko.bndb")]


def namespace(view_filter):
    if view_filter:
        matches = [v for v in VIEWS if view_filter in v.file.filename]
        if len(matches) != 1:
            raise LookupError("view filter %r matches %d open views" % (view_filter, len(matches)))
        return {"bv": matches[0], "bvs": VIEWS}
    return {"bv": VIEWS[0], "bvs": VIEWS}


class BridgeFixture(unittest.TestCase):
    """A running ScriptBridge with a fake namespace and a bnrun config pointing at it."""

    def setUp(self):
        self.token = bridge_server.new_token()
        self.main_calls = []

        def runner(fn):
            self.main_calls.append(threading.current_thread().name)
            fn()

        self.bridge = bridge_server.ScriptBridge(self.token, namespace, runner)
        self.bridge.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.config = os.path.join(self.tmp.name, "script_bridge.json")
        with open(self.config, "w") as f:
            json.dump({"port": self.bridge.port, "token": self.token}, f)

    def tearDown(self):
        self.bridge.stop()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.bridge.port, timeout=10)
        h = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse()
        reply = resp.status, json.loads(resp.read())
        conn.close()
        return reply

    def hold_lock(self, session="me"):
        """Take the usage lock: UI views (bv, bvs, view, main_thread) need it."""
        status, _ = self.request("POST", "/lock", {"session": session})
        self.assertEqual(status, 200)

    def bnrun(self, *args, stdin=""):
        env = dict(os.environ, BN_SCRIPT_BRIDGE_CONFIG=self.config)
        return subprocess.run([sys.executable, BNRUN, *args], input=stdin, capture_output=True,
                              text=True, env=env, timeout=30)


class BridgeTest(BridgeFixture):
    def test_ping(self):
        self.assertEqual(self.request("GET", "/ping"), (200, {"ok": True, "jobs": True}))

    def test_runs_script_with_stdout_and_result(self):
        self.hold_lock()
        status, reply = self.request("POST", "/run", {
            "code": "print('hello', bv.file.filename)\nresult = {'n': len(bvs)}",
            "session": "me"})
        self.assertEqual(status, 200)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["stdout"], "hello /x/DM365_AllegroIndoor_debug.bndb\n")
        self.assertEqual(reply["result"], {"n": 2})

    def test_non_json_result_is_repr(self):
        _, reply = self.request("POST", "/run", {"code": "result = object"})
        self.assertEqual(reply["result"], "<class 'object'>")

    def test_view_selection(self):
        self.hold_lock()
        _, reply = self.request("POST", "/run", {"code": "result = bv.file.filename",
                                                 "view": "cx20707", "session": "me"})
        self.assertEqual(reply["result"], "/x/cx20707.ko.bndb")
        _, reply = self.request("POST", "/run", {"code": "pass", "view": "bndb",
                                                 "session": "me"})
        self.assertFalse(reply["ok"])
        self.assertIn("matches 2 open views", reply["error"])

    def test_script_exception_is_reported(self):
        _, reply = self.request("POST", "/run", {"code": "print('before')\n1/0"})
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["stdout"], "before\n")
        self.assertIn("ZeroDivisionError", reply["error"])

    def test_main_thread_runner_used_on_request(self):
        self.hold_lock()
        self.request("POST", "/run", {"code": "pass"})
        self.assertEqual(self.main_calls, [])
        _, reply = self.request("POST", "/run", {"code": "result = 1", "main_thread": True,
                                                 "session": "me"})
        self.assertEqual(reply["result"], 1)
        self.assertEqual(len(self.main_calls), 1)

    def test_rejects_wrong_or_missing_token(self):
        for auth in ("Bearer " + "0" * 64, "Bearer", None, self.token):
            status, reply = self.request("POST", "/run", {"code": "result = 1"},
                                         {"Authorization": auth})
            self.assertEqual(status, 403, auth)
            self.assertEqual(reply["error"], "bad token")

    def test_rejects_browser_style_requests(self):
        status, reply = self.request("POST", "/run", {"code": "result = 1"},
                                     {"Origin": "http://evil.example"})
        self.assertEqual((status, reply["error"]), (403, "Origin header not allowed"))
        status, reply = self.request("POST", "/run", {"code": "result = 1"},
                                     {"Host": "evil.example:80"})
        self.assertEqual((status, reply["error"]), (403, "bad Host header"))

    def test_rejects_bad_body(self):
        status, _ = self.request("POST", "/run", {"nocode": 1})
        self.assertEqual(status, 400)
        status, _ = self.request("POST", "/elsewhere", {"code": "pass"})
        self.assertEqual(status, 404)

    def test_bnrun_client(self):
        r = self.bnrun("-e", "print('x'); result = [1, 2]")
        self.assertEqual((r.returncode, r.stdout), (0, "x\n[\n  1,\n  2\n]\n"))
        self.hold_lock()
        r = self.bnrun("--session", "me", "--view", "cx20707", stdin="print(bv.file.filename)")
        self.assertEqual((r.returncode, r.stdout), (0, "/x/cx20707.ko.bndb\n"))
        r = self.bnrun("--session", "me", "-e", "raise ValueError('boom')")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ValueError: boom", r.stderr)

    def test_bnrun_wrong_token_and_missing_config(self):
        with open(self.config, "w") as f:
            json.dump({"port": self.bridge.port, "token": "wrong"}, f)
        r = self.bnrun("-e", "pass")
        self.assertEqual(r.returncode, 2)
        self.assertIn("refused (403)", r.stderr)
        os.remove(self.config)
        r = self.bnrun("-e", "pass")
        self.assertEqual(r.returncode, 2)
        self.assertIn("not found", r.stderr)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class UsageLockTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.events = []
        self.lock = bridge_server.UsageLock(self.clock, lambda e, s: self.events.append(e))

    def test_take_renew_release(self):
        self.assertEqual(self.lock.status(), {"locked": False})
        ok, status = self.lock.acquire("sess-fe", "fe", "decoding ctl.bin")
        self.assertTrue(ok)
        self.assertEqual((status["session"], status["name"], status["purpose"],
                          status["expires_in"]), ("sess-fe", "fe", "decoding ctl.bin", 300))
        ok, status = self.lock.acquire("sess-audio")
        self.assertEqual((ok, status["session"]), (False, "sess-fe"))
        self.clock.now += 200
        ok, status = self.lock.acquire("sess-fe")
        self.assertEqual((ok, status["expires_in"], status["purpose"]),
                         (True, 300, "decoding ctl.bin"))
        self.assertEqual(self.lock.release("sess-audio")[0], False)
        self.assertEqual(self.lock.release("sess-fe")[0], True)
        self.assertEqual(self.lock.status(), {"locked": False})
        self.assertEqual(self.events, ["locked", "renewed", "released"])

    def test_expiry_and_use_renews(self):
        self.lock.acquire("sess-fe", ttl=60)
        self.clock.now += 50
        self.assertEqual(self.lock.check_use(None)[0], False)
        self.assertEqual(self.lock.check_use("sess-audio")[0], False)
        allowed, status = self.lock.check_use("sess-fe")
        self.assertEqual((allowed, status["expires_in"]), (True, 60))
        self.clock.now += 61
        self.assertEqual(self.lock.status(), {"locked": False})
        self.assertEqual(self.events, ["locked", "expired"])
        self.assertTrue(self.lock.acquire("sess-audio")[0])

    def test_force_release_and_ttl_limits(self):
        self.lock.acquire("sess-fe", ttl=10 ** 9)
        self.assertEqual(self.lock.status()["expires_in"], bridge_server.MAX_LOCK_TTL)
        ok, status = self.lock.release("sess-audio", force=True)
        self.assertEqual((ok, status["session"]), (True, "sess-fe"))
        self.assertEqual(self.events, ["locked", "forced"])

    def test_describe(self):
        self.assertEqual(bridge_server.describe({"locked": False}), "free")
        _, status = self.lock.acquire("0123456789abcdef", purpose="x")
        self.assertTrue(bridge_server.describe(status).startswith("locked by 01234567: x"))


class LockEndpointTest(BridgeTest):
    def test_lock_gates_scripts(self):
        status, reply = self.request("POST", "/lock", {"session": "sess-fe", "name": "fe",
                                                       "purpose": "ctl.bin"})
        self.assertEqual(status, 200)
        status, reply = self.request("POST", "/run", {"code": "result = 1"})
        self.assertEqual(status, 423)
        self.assertIn("locked by fe: ctl.bin", reply["error"])
        status, reply = self.request("POST", "/run", {"code": "result = 1", "session": "sess-fe"})
        self.assertEqual((status, reply["result"]), (200, 1))
        status, reply = self.request("POST", "/lock", {"session": "sess-audio"})
        self.assertEqual((status, reply["lock"]["session"]), (409, "sess-fe"))
        self.assertEqual(self.request("GET", "/lock")[1]["lock"]["name"], "fe")
        self.assertEqual(self.request("POST", "/unlock", {"session": "sess-audio"})[0], 409)
        self.assertEqual(self.request("POST", "/unlock", {"session": "sess-fe"})[0], 200)
        self.assertEqual(self.request("GET", "/lock")[1]["lock"], {"locked": False})

    def test_lock_needs_session(self):
        self.assertEqual(self.request("POST", "/lock", {"session": " "})[0], 400)
        self.assertEqual(self.request("POST", "/lock", {"session": "x", "ttl": "soon"})[0], 400)

    def test_bnrun_lock_commands(self):
        me = ("--session", "sess-fe", "--name", "fe")
        r = self.bnrun("--status")
        self.assertEqual((r.returncode, r.stdout), (0, "free\nno script running\n"))
        r = self.bnrun("--lock", "--purpose", "ctl.bin", "--ttl", "2", *me)
        self.assertEqual(r.returncode, 0)
        self.assertIn("locked by fe: ctl.bin", r.stdout)
        self.assertRegex(r.stdout, r"expires in (119|120)s")
        r = self.bnrun("--status")
        self.assertEqual(r.returncode, 3)
        self.assertTrue(r.stdout.startswith("locked by fe: ctl.bin"))
        r = self.bnrun("--session", "sess-audio", "-e", "result = 2")
        self.assertEqual(r.returncode, 3)
        self.assertIn("Binary Ninja is locked by fe", r.stderr)
        r = self.bnrun("-e", "result = 2", *me)
        self.assertEqual((r.returncode, r.stdout), (0, "2\n"))
        r = self.bnrun("--lock", "--session", "sess-audio")
        self.assertEqual(r.returncode, 3)
        r = self.bnrun("--unlock", "--session", "sess-audio")
        self.assertEqual(r.returncode, 3)
        r = self.bnrun("--unlock", *me)
        self.assertEqual((r.returncode, r.stdout), (0, "released\n"))
        r = self.bnrun("--lock", "--session", "sess-audio")
        self.assertEqual(r.returncode, 0)
        r = self.bnrun("--unlock", "--force", *me)
        self.assertEqual((r.returncode, r.stdout), (0, "released\n"))

    def test_bnrun_session_from_environment(self):
        env = dict(os.environ, BN_SCRIPT_BRIDGE_CONFIG=self.config,
                   CLAUDE_CODE_SESSION_ID="sess-env", BN_SESSION_NAME="envname")
        r = subprocess.run([sys.executable, BNRUN, "--lock"], capture_output=True, text=True,
                           env=env, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("locked by envname", r.stdout)
        self.assertEqual(self.bridge.lock.status()["session"], "sess-env")


BUSY = "print('started')\nwhile True:\n    try:\n        pass\n    except Exception:\n        pass\n"


class CancelTest(BridgeFixture):
    def run_async(self, body):
        """POST /run on a thread; returns (thread, box) with box["reply"] when it answers."""
        box = {}
        t = threading.Thread(target=lambda: box.update(
            reply=self.request("POST", "/run", body)), daemon=True)
        t.start()
        return t, box

    def wait_running(self):
        for _ in range(500):
            running = self.request("GET", "/run")[1]["running"]
            if running and running["state"] == "running":
                return running
            time.sleep(0.01)
        self.fail("script never started")

    def assert_idle_and_usable(self):
        self.assertFalse(self.bridge.run_mutex.locked())
        self.assertEqual(self.request("GET", "/run")[1], {"ok": True, "running": None,
                                                          "queued": 0, "parallel": [],
                                                          "parallel_queued": 0,
                                                          "max_parallel": 4, "bndb": [],
                                                          "bndb_hung": [], "jobs": [],
                                                          "lost": 0})
        status, reply = self.request("POST", "/run", {"code": "result = sum(range(100000))"})
        self.assertEqual((status, reply["ok"], reply["result"]), (200, True, 4999950000))

    def test_cancel_busy_loop(self):
        t, box = self.run_async({"code": BUSY, "session": "sess-fe", "name": "fe"})
        running = self.wait_running()
        self.assertEqual((running["label"], running["session"], running["name"]),
                         ("print('started')", "sess-fe", "fe"))
        self.assertIn("running script \"print('started')\" for 0s (fe, job %s)" % running["job"],
                      bridge_server.describe_run(running))
        self.assertEqual(self.request("GET", "/lock")[1]["running"]["label"], "print('started')")
        status, reply = self.request("POST", "/cancel", {"session": "sess-fe"})
        self.assertEqual((status, reply["cancelled"]["cancel_requested"]),
                         (200, "session sess-fe"))
        t.join(10)
        self.assertFalse(t.is_alive())
        status, reply = box["reply"]
        self.assertEqual(status, 200)
        self.assertEqual((reply["ok"], reply["error"], reply["stdout"], reply["cancelled_by"]),
                         (False, "cancelled", "started\n", "session sess-fe"))
        self.assert_idle_and_usable()

    def test_timeout(self):
        start = time.time()
        status, reply = self.request("POST", "/run", {"code": BUSY, "timeout": 0.3})
        self.assertLess(time.time() - start, 5)
        self.assertEqual((status, reply["error"], reply["cancelled_by"], reply["stdout"]),
                         (200, "cancelled", "timeout after 0.3s", "started\n"))
        _, reply = self.request("POST", "/run", {"code": "result = 1", "timeout": 5})
        self.assertEqual(reply["result"], 1)
        for bad in (0, -1, "soon", True):
            self.assertEqual(self.request("POST", "/run", {"code": "pass", "timeout": bad})[0],
                             400, bad)
        self.assert_idle_and_usable()

    def test_cancel_when_idle(self):
        status, reply = self.request("POST", "/cancel", {"session": "sess-fe", "force": True})
        self.assertEqual((status, reply["error"]), (409, "no script is running"))
        self.assert_idle_and_usable()

    def test_permissions(self):
        t, box = self.run_async({"code": BUSY, "session": "sess-fe"})
        self.wait_running()
        for body in ({}, {"session": "sess-audio"}, {"run_id": None, "session": "sess-x"}):
            status, reply = self.request("POST", "/cancel", body)
            self.assertEqual(status, 403, body)
            self.assertIn("not allowed", reply["error"])
        self.assertTrue(t.is_alive())
        status, reply = self.request("POST", "/cancel", {"session": "sess-audio", "force": True})
        self.assertEqual((status, reply["cancelled"]["cancel_requested"]),
                         (200, "force by sess-audio"))
        t.join(10)
        self.assertEqual(box["reply"][1]["error"], "cancelled")
        self.assert_idle_and_usable()

    def test_lock_holder_may_cancel(self):
        t, box = self.run_async({"code": BUSY})                # no session
        self.wait_running()
        self.request("POST", "/lock", {"session": "sess-fe"})
        self.assertEqual(self.request("POST", "/cancel", {"session": "sess-fe"})[0], 200)
        t.join(10)
        self.assertEqual(box["reply"][1]["cancelled_by"], "lock holder sess-fe")
        self.request("POST", "/unlock", {"session": "sess-fe"})
        self.assert_idle_and_usable()

    def test_run_id_cancels_running_or_queued_run(self):
        t, box = self.run_async({"code": BUSY, "run_id": "r1"})
        self.wait_running()
        t2, box2 = self.run_async({"code": "result = 'queued ran'", "run_id": "r2"})
        for _ in range(500):
            if self.request("GET", "/run")[1]["queued"] == 1:
                break
            time.sleep(0.01)
        self.assertEqual(self.request("POST", "/cancel", {"run_id": "r2"})[1],
                         {"ok": True, "cancelled": None, "pending": True})
        self.assertEqual(self.request("POST", "/cancel", {"run_id": "r1"})[0], 200)
        t.join(10)
        t2.join(10)
        self.assertEqual(box["reply"][1]["cancelled_by"], "its client")
        self.assertEqual((box2["reply"][1]["error"], box2["reply"][1]["stdout"]),
                         ("cancelled", ""))
        self.assert_idle_and_usable()

    def test_main_thread_runner_targets_executing_thread(self):
        jobs = []
        executed_on = []
        stop = threading.Event()

        def main_loop():                        # stands in for Binary Ninja's UI thread
            while not stop.is_set():
                if jobs:
                    fn, done = jobs.pop()
                    executed_on.append(threading.get_ident())
                    fn()
                    done.set()
                time.sleep(0.005)
            executed_on.append("survived")

        main = threading.Thread(target=main_loop, daemon=True)
        main.start()

        def runner(fn):
            done = threading.Event()
            jobs.append((fn, done))
            done.wait()
        self.bridge.main_thread_runner = runner
        self.hold_lock("s")
        t, box = self.run_async({"code": BUSY, "main_thread": True, "session": "s"})
        self.wait_running()
        self.assertEqual(self.request("POST", "/cancel", {"session": "s"})[0], 200)
        t.join(10)
        self.assertEqual(box["reply"][1]["error"], "cancelled")
        _, reply = self.request("POST", "/run", {"code": "result = 2", "main_thread": True,
                                                 "session": "s"})
        self.request("POST", "/unlock", {"session": "s"})
        self.assertEqual(reply["result"], 2)
        self.assertEqual(executed_on, [main.ident, main.ident])
        stop.set()
        main.join(5)
        self.assertEqual(executed_on[-1], "survived")   # no stray ScriptCancelled killed it

    def test_cancel_racing_script_end_never_leaks(self):
        rnd = random.Random(1)
        for i in range(30):
            t, box = self.run_async({"code": "for i in range(%d): pass\nresult = 'done'"
                                     % rnd.randrange(1, 200000), "session": "s"})
            time.sleep(rnd.random() * 0.01)
            self.request("POST", "/cancel", {"session": "s"})
            t.join(10)
            reply = box["reply"][1]
            self.assertTrue(reply["result"] == "done" or reply["error"] == "cancelled", reply)
        self.assert_idle_and_usable()

    def test_bnrun_cancel_timeout_and_status(self):
        r = self.bnrun("--cancel", "--session", "s")
        self.assertEqual((r.returncode, r.stderr), (1, "bnrun: no script is running\n"))
        r = self.bnrun("--timeout", "0.3", stdin=BUSY)
        self.assertEqual((r.returncode, r.stdout), (1, "started\n"))
        self.assertIn("script cancelled (by timeout after 0.3s)", r.stderr)
        t, box = self.run_async({"code": BUSY, "session": "sess-fe", "name": "fe"})
        self.wait_running()
        r = self.bnrun("--status")
        self.assertEqual(r.returncode, 0)
        self.assertIn("running script \"print('started')\"", r.stdout)
        r = self.bnrun("--cancel", "--session", "sess-audio")
        self.assertEqual(r.returncode, 3)
        self.assertIn("not allowed", r.stderr)
        r = self.bnrun("--cancel", "--force", "--session", "sess-audio")
        self.assertEqual(r.returncode, 0)
        self.assertIn("cancel sent", r.stdout)
        t.join(10)
        self.assertEqual(box["reply"][1]["error"], "cancelled")
        self.assert_idle_and_usable()

    def test_bnrun_ctrl_c_cancels(self):
        env = dict(os.environ, BN_SCRIPT_BRIDGE_CONFIG=self.config)
        env.pop("CLAUDE_CODE_SESSION_ID", None)            # cancel must work without a session
        for sig in (signal.SIGINT, signal.SIGTERM):
            proc = subprocess.Popen([sys.executable, BNRUN, "-e", BUSY], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.wait_running()
            proc.send_signal(sig)
            _, err = proc.communicate(timeout=20)
            self.assertEqual(proc.returncode, 130, err)
            self.assertIn("script cancelled in Binary Ninja", err)
            for _ in range(500):
                if self.request("GET", "/run")[1]["running"] is None:
                    break
                time.sleep(0.01)
            self.assert_idle_and_usable()


# Waits (in small, cancellable steps) until the test sets `gate`, which it gets from base_namespace.
GATED = "print('started')\nwhile not gate.is_set():\n    time.sleep(0.005)\nresult = 'done'\n"

# Counts how many scripts run at once (in the shared `meter`, from base_namespace).
METERED = """
with meter['lock']:
    meter['now'] += 1
    meter['max'] = max(meter['max'], meter['now'])
time.sleep(0.3)
with meter['lock']:
    meter['now'] -= 1
"""


class ParallelTest(BridgeFixture):
    run_async = CancelTest.run_async
    assert_idle_and_usable = CancelTest.assert_idle_and_usable

    def setUp(self):
        super().setUp()
        self.gate = threading.Event()
        self.meter = {"lock": threading.Lock(), "now": 0, "max": 0}
        self.bridge.base_namespace = {"time": time, "gate": self.gate, "meter": self.meter,
                                      "barrier": threading.Barrier(2)}

    def tearDown(self):
        self.gate.set()
        super().tearDown()

    def wait_for(self, predicate, what):
        for _ in range(1000):
            status = self.request("GET", "/run")[1]
            if predicate(status):
                return status
            time.sleep(0.01)
        self.fail("timed out waiting for " + what)

    def running_parallel(self, n):
        return self.wait_for(lambda st: len([r for r in st["parallel"]
                                             if r["state"] == "running"]) == n,
                             "%d parallel runs" % n)

    def test_two_parallel_scripts_overlap(self):
        code = "barrier.wait(5)\nresult = 'met'"   # passes only if both run at the same time
        runs = [self.run_async({"code": code, "parallel": True}) for _ in range(2)]
        for t, box in runs:
            t.join(10)
            self.assertEqual(box["reply"][1]["result"], "met", box["reply"])
        self.assert_idle_and_usable()

    def test_serialized_and_parallel_overlap(self):
        code = "barrier.wait(5)\nresult = 'met'"
        runs = [self.run_async({"code": code, "parallel": p}) for p in (False, True)]
        for t, box in runs:
            t.join(10)
            self.assertEqual(box["reply"][1]["result"], "met", box["reply"])

    def test_serialized_still_one_at_a_time(self):
        runs = [self.run_async({"code": METERED}) for _ in range(3)]
        for t, box in runs:
            t.join(10)
            self.assertTrue(box["reply"][1]["ok"], box["reply"])
        self.assertEqual(self.meter["max"], 1)
        runs = [self.run_async({"code": METERED, "parallel": True}) for _ in range(3)]
        for t, box in runs:
            t.join(10)
            self.assertTrue(box["reply"][1]["ok"], box["reply"])
        self.assertEqual(self.meter["max"], 3)

    def test_max_parallel_limit_queues(self):
        self.bridge.max_parallel = lambda: 2        # like the scriptbridge.maxParallel setting
        runs = [self.run_async({"code": GATED, "parallel": True, "name": "p%d" % i})
                for i in range(3)]
        status = self.wait_for(lambda st: len(st["parallel"]) == 2 and st["parallel_queued"] == 1,
                               "2 running + 1 queued")
        self.assertEqual((status["max_parallel"], status["running"], status["queued"]),
                         (2, None, 0))
        self.assertTrue(all(r["parallel"] for r in status["parallel"]))
        r = self.bnrun("--status")
        self.assertEqual(r.stdout.count("running script \"print('started')\""), 2, r.stdout)
        self.assertEqual(r.stdout.count(", parallel)"), 3, r.stdout)
        self.assertIn("queued: 0 serialized, 1 parallel\n  queued script \"print('started')\"",
                      r.stdout)
        self.assertNotIn("no script running", r.stdout)            # what bnrestart checks
        _, reply = self.request("POST", "/run", {"code": "result = 'serialized'"})
        self.assertEqual(reply["result"], "serialized")             # not stuck behind them
        self.gate.set()
        for t, box in runs:
            t.join(10)
            self.assertEqual(box["reply"][1]["result"], "done")
        self.bridge.max_parallel = 4
        self.assert_idle_and_usable()

    def test_cancel_hits_only_that_run(self):
        a, box_a = self.run_async({"code": GATED, "parallel": True, "session": "sess-a"})
        b, box_b = self.run_async({"code": GATED, "parallel": True, "session": "sess-b"})
        c, box_c = self.run_async({"code": GATED, "session": "sess-c"})
        self.running_parallel(2)
        self.wait_for(lambda st: st["running"] and st["running"]["state"] == "running",
                      "serialized run")
        status, reply = self.request("POST", "/cancel", {"session": "sess-a"})
        self.assertEqual(status, 200)
        self.assertEqual([r["session"] for r in reply["cancelled_runs"]], ["sess-a"])
        a.join(10)
        self.assertEqual((box_a["reply"][1]["error"], box_a["reply"][1]["cancelled_by"]),
                         ("cancelled", "session sess-a"))
        status = self.request("GET", "/run")[1]
        self.assertEqual([r["session"] for r in status["parallel"]], ["sess-b"])
        self.assertEqual(status["running"]["session"], "sess-c")
        self.assertEqual(self.request("POST", "/cancel", {"session": "sess-x"})[0], 403)
        r = self.bnrun("--cancel", "--force", "--session", "sess-x")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.count("cancel sent"), 2, r.stdout)
        for t, box in ((b, box_b), (c, box_c)):
            t.join(10)
            self.assertEqual(box["reply"][1]["cancelled_by"], "force by sess-x")
        self.assert_idle_and_usable()

    def test_timeout_and_run_id_hit_only_that_run(self):
        other, box_other = self.run_async({"code": GATED, "parallel": True, "run_id": "keep"})
        self.running_parallel(1)
        status, reply = self.request("POST", "/run", {"code": GATED, "parallel": True,
                                                      "timeout": 0.3})
        self.assertEqual((reply["error"], reply["cancelled_by"]),
                         ("cancelled", "timeout after 0.3s"))
        self.bridge.max_parallel = 1                 # a second run now queues for the slot
        q, box_q = self.run_async({"code": GATED, "parallel": True, "run_id": "queued"})
        self.wait_for(lambda st: st["parallel_queued"] == 1, "queued parallel run")
        self.assertEqual(self.request("POST", "/cancel", {"run_id": "queued"})[1]["pending"], True)
        q.join(5)
        self.assertEqual(box_q["reply"][1]["error"], "cancelled")
        self.assertTrue(other.is_alive())
        self.gate.set()
        other.join(10)
        self.assertEqual(box_other["reply"][1]["result"], "done")
        self.bridge.max_parallel = 4
        self.assert_idle_and_usable()

    def test_parallel_with_main_thread_rejected(self):
        status, reply = self.request("POST", "/run", {"code": "pass", "parallel": True,
                                                      "main_thread": True})
        self.assertEqual(status, 400)
        self.assertIn("main thread", reply["error"])
        r = self.bnrun("--parallel", "--main-thread", "-e", "pass")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--parallel can't be combined with --main-thread", r.stderr)
        self.assertEqual(self.main_calls, [])

    def test_lock_rules_apply_to_parallel(self):
        self.hold_lock("sess-fe")
        # another session's parallel run goes ahead (no UI views), its plain run waits
        status, reply = self.request("POST", "/run", {"code": "result = len(bvs)", "parallel": True,
                                                      "session": "sess-audio"})
        self.assertEqual((status, reply["ok"]), (200, False))
        self.assertIn("UIViewsUnavailable", reply["error"])       # ran, without UI views
        status, reply = self.request("POST", "/run", {"code": "pass", "session": "sess-audio"})
        self.assertEqual(status, 423)
        self.assertIn("--bndb and --parallel runs (no UI views) may still run", reply["error"])
        r = self.bnrun("--parallel", "--session", "sess-audio", "-e", "pass")
        self.assertEqual(r.returncode, 0, r.stderr)
        # renewing the lock leaves it as it was; --exclusive refuses parallel runs too
        r = self.bnrun("--lock", "--exclusive", "--session", "sess-fe")
        self.assertIn("locked exclusively by", r.stdout)
        self.bnrun("--lock", "--session", "sess-fe")
        self.assertTrue(self.bridge.lock.status()["exclusive"])
        r = self.bnrun("--parallel", "--session", "sess-audio", "-e", "pass")
        self.assertEqual(r.returncode, 3)
        self.assertEqual(self.request("POST", "/unlock", {"session": "sess-fe"})[0], 200)
        self.hold_lock("sess-fe")
        self.assertFalse(self.bridge.lock.status()["exclusive"])   # a new lock: not exclusive
        # the holder's parallel script runs next to its own serialized one (no self-deadlock)
        t, box = self.run_async({"code": GATED, "session": "sess-fe"})
        self.wait_for(lambda st: st["running"] and st["running"]["state"] == "running",
                      "serialized run")
        _, reply = self.request("POST", "/run", {"code": "result = 1", "parallel": True,
                                                 "session": "sess-fe"})
        self.assertEqual(reply["result"], 1)
        self.gate.set()
        t.join(10)
        self.assertEqual(box["reply"][1]["result"], "done")


class ArgsEnvTest(BridgeFixture):
    """args/env forwarding: bare names everywhere, sys.argv/os.environ swap serialized only.

    The bridge runs in-process, so the test's own sys.argv / os.environ ARE the swapped
    globals -- which also lets us assert the restore."""

    def test_serialized_argv_and_env_swapped_and_restored(self):
        saved_argv, saved_environ = sys.argv, os.environ.get("BN_T_X")
        try:
            status, reply = self.request("POST", "/run", {
                "code": "import sys, os\n"
                        "result = {'argv': sys.argv, 'x': os.environ.get('BN_T_X'), "
                        "'y': os.environ.get('BN_T_Y')}",
                "args": ["s.py", "x", "--y"], "env": {"BN_T_X": "set", "BN_T_Y": "new"}})
            self.assertEqual(status, 200)
            self.assertTrue(reply["ok"])
            self.assertEqual(reply["result"], {"argv": ["s.py", "x", "--y"], "x": "set",
                                               "y": "new"})
            self.assertEqual(sys.argv, saved_argv)
            self.assertEqual(os.environ.get("BN_T_X"), saved_environ)
        finally:
            os.environ.pop("BN_T_Y", None)

    def test_pre_existing_env_restored(self):
        os.environ["BN_T_Y"] = "old"
        try:
            _, reply = self.request("POST", "/run", {
                "code": "import os\nresult = os.environ['BN_T_Y']", "env": {"BN_T_Y": "new"}})
            self.assertEqual(reply["result"], "new")
            self.assertEqual(os.environ["BN_T_Y"], "old")
        finally:
            os.environ.pop("BN_T_Y", None)

    def test_env_restored_when_script_raises(self):
        _, reply = self.request("POST", "/run", {
            "code": "import os\nresult = os.environ['BN_T_Z']\n1/0", "env": {"BN_T_Z": "v"}})
        self.assertFalse(reply["ok"])
        self.assertIsNone(os.environ.get("BN_T_Z"))

    def test_bare_names_in_both_lanes(self):
        code = "result = [argv, env]"
        _, reply = self.request("POST", "/run", {"code": code, "args": ["s.py", "a"],
                                                 "env": {"K": "V"}})
        self.assertEqual(reply["result"], [["s.py", "a"], {"K": "V"}])
        _, reply = self.request("POST", "/run", {"code": code, "args": ["p.py"],
                                                 "env": {"K": "V"}, "parallel": True})
        self.assertEqual(reply["result"], [["p.py"], {"K": "V"}])

    def test_parallel_never_swaps_sys_argv(self):
        saved_argv = sys.argv
        _, reply = self.request("POST", "/run", {
            "code": "import sys\nresult = sys.argv[0] != 'p.py'", "args": ["p.py"],
            "parallel": True})
        self.assertTrue(reply["result"])
        self.assertEqual(sys.argv, saved_argv)

    def test_no_swap_when_args_and_env_absent(self):
        saved_argv = sys.argv
        _, reply = self.request("POST", "/run", {"code": "import sys\nresult = sys.argv"})
        self.assertEqual(reply["result"], saved_argv)
        self.assertEqual(sys.argv, saved_argv)

    def test_no_swap_without_a_view_even_when_the_lock_is_free(self):
        # A serialized run without view/main_thread doesn't need the lock; the swap must
        # still happen for it (serialized lane, no overlap).
        saved_argv = sys.argv
        _, reply = self.request("POST", "/run", {
            "code": "import sys\nresult = sys.argv[1:]", "args": ["s.py", "flag"]})
        self.assertEqual(reply["result"], ["flag"])
        self.assertEqual(sys.argv, saved_argv)

    def test_bad_args_and_env(self):
        for req in ({"args": "s.py"}, {"args": [1]}, {"env": ["K=V"]}, {"env": {"K": 1}}):
            status, reply = self.request("POST", "/run", {"code": "pass", **req})
            self.assertEqual((status, reply["ok"]), (400, False), req)
            self.assertIn("bad request", reply["error"])

    def test_bnrun_dash_dash_and_env(self):
        r = self.bnrun("-e", "import sys\nresult = sys.argv", "--", "a", "b")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout), ["<bnrun>", "a", "b"])
        r = self.bnrun("-e", "import sys\nresult = sys.argv")
        self.assertEqual(json.loads(r.stdout), ["<bnrun>"])
        path = os.path.join(self.tmp.name, "s.py")
        with open(path, "w") as f:
            f.write("import sys\nresult = sys.argv")
        r = self.bnrun(path, "--", "x")
        self.assertEqual(json.loads(r.stdout), [path, "x"])
        r = self.bnrun("-e", "import os\nresult = os.environ.get('BN_T_W')",
                       "--env", "BN_T_W=V")
        self.assertEqual(r.stdout, "V\n")
        r = self.bnrun("-e", "pass", "--env", "BN_T_W")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--env expects K=V", r.stderr)

    def test_bnrun_dash_dash_works_without_args(self):
        r = self.bnrun("-e", "result = argv", "--")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout), ["<bnrun>"])


class MaxOutTest(BridgeFixture):
    """--max-out: truncate on the terminal, keep the full output in a file."""

    def bnrun(self, *args, stdin=""):
        env = dict(os.environ, BN_SCRIPT_BRIDGE_CONFIG=self.config,
                   HOME=self.tmp.name, XDG_CACHE_HOME=os.path.join(self.tmp.name, ".cache"))
        return subprocess.run([sys.executable, BNRUN, *args], input=stdin, capture_output=True,
                              text=True, env=env, timeout=30)

    def cache_files(self):
        cache = os.path.join(self.tmp.name, ".cache", "bn-script-bridge")
        return sorted(os.listdir(cache)) if os.path.isdir(cache) else []

    def test_truncates_and_persists(self):
        r = self.bnrun("-e", "print('x' * 500)\nresult = 'y' * 500", "--max-out", "100")
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.stdout.startswith("x" * 100))
        self.assertIn("bnrun: truncated at 100 characters; full output: ", r.stdout)
        files = self.cache_files()
        self.assertEqual(len(files), 1)
        with open(os.path.join(self.tmp.name, ".cache", "bn-script-bridge", files[0])) as f:
            full = f.read()
        self.assertEqual(len(full), 1001)      # 500 x's + \n + 500 y's
        self.assertTrue(full.startswith("x" * 500))
        self.assertIn("y" * 500, full)

    def test_short_output_untouched(self):
        r = self.bnrun("-e", "print('hi')\nresult = 42", "--max-out", "1000")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "hi\n42\n")
        self.assertEqual(self.cache_files(), [])

    def test_json_mode_persists_full_dump(self):
        r = self.bnrun("-e", "result = 'z' * 300", "--json", "--max-out", "50")
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.stdout.startswith("{"))
        self.assertIn("bnrun: truncated at 50 characters; full output: ", r.stdout)
        files = self.cache_files()
        self.assertEqual(len(files), 1)

    def test_errors_are_never_truncated(self):
        r = self.bnrun("-e", "1/0", "--max-out", "10")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ZeroDivisionError", r.stderr)
        self.assertEqual(self.cache_files(), [])

    def test_bad_max_out(self):
        r = self.bnrun("-e", "pass", "--max-out", "0")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--max-out must be at least 1", r.stderr)


class CachedView:
    """A fake BinaryView: identity is the (real) file path it was opened for."""
    def __init__(self, path):
        self.path = path
        self.saved = 0
        self.closed = False


class BndbCacheTest(BridgeFixture):
    """The --bndb cache: one headless view per path, serialized lane, no lock needed."""

    run_async = CancelTest.run_async
    wait_running = CancelTest.wait_running

    def setUp(self):
        super().setUp()
        self.opened = []

        def open_view(path):
            self.opened.append(path)
            return CachedView(path)

        def save_view(v):
            v.saved += 1
            return True

        def close_view(v):
            v.closed = True

        self.bridge.bndb_open = open_view
        self.bridge.bndb_save = save_view
        self.bridge.bndb_close = close_view

    def path(self, name="target.bndb"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w") as f:
            f.write("x")
        return path

    def run_bndb(self, path, **extra):
        return self.request("POST", "/run", dict({"code": "result = bv.path", "bndb": path},
                                                 **extra))

    def test_caches_by_abspath(self):
        path = self.path()
        _, reply = self.run_bndb(path)
        self.assertEqual(reply["result"], path)
        self.assertEqual(self.opened, [os.path.abspath(path)])
        _, reply = self.run_bndb(os.path.join(self.tmp.name, "..", os.path.basename(self.tmp.name),
                                              "target.bndb"))
        self.assertEqual(reply["result"], path)      # abspath-normalized: same view
        self.assertEqual(self.opened, [os.path.abspath(path)])   # opened once
        other = self.path("other.bndb")
        _, reply = self.run_bndb(other)
        self.assertEqual(reply["result"], other)
        self.assertEqual(len(self.opened), 2)

    def test_bvs_is_a_placeholder(self):
        path = self.path()
        _, reply = self.run_bndb(path)
        self.assertTrue(reply["ok"])
        _, reply = self.request("POST", "/run", {
            "code": "try:\n    len(bvs)\nexcept Exception as e:\n    result = str(e)",
            "bndb": path})
        self.assertIn("not available", reply["result"])
        self.assertIn(bridge_server.BNDB_BVS, reply["result"])
        _, reply = self.request("POST", "/run", {"code": "result = repr(bvs)", "bndb": path})
        self.assertTrue(reply["ok"])

    def test_combo_400s_and_no_lock_needed(self):
        path = self.path()
        for extra in ({"view": "x"}, {"main_thread": True}, {"parallel": True}):
            status, reply = self.run_bndb(path, **extra)
            self.assertEqual((status, reply["ok"]), (400, False))
            self.assertIn(bridge_server.BNDB_NO_UI, reply["error"])
        status, reply = self.run_bndb(path)          # no lock held: still runs
        self.assertEqual((status, reply["ok"]), (200, True))
        self.hold_lock("other")                      # another session holds it: still runs
        status, reply = self.run_bndb(path)
        self.assertEqual((status, reply["ok"]), (200, True))
        self.request("POST", "/lock", {"session": "other", "exclusive": True})
        status, reply = self.run_bndb(path)          # exclusive: 423
        self.assertEqual(status, 423)
        self.assertIn("locked exclusively by", reply["error"])

    def test_not_found_and_bad_types(self):
        status, reply = self.run_bndb(os.path.join(self.tmp.name, "missing.bndb"))
        self.assertEqual((status, reply["ok"]), (400, False))
        self.assertIn("not found", reply["error"])
        for bad in (123, ""):
            status, reply = self.request("POST", "/run", {"code": "pass", "bndb": bad})
            self.assertEqual(status, 400)

    def test_open_failure_reports_and_leaves_cache_empty(self):
        path = self.path()

        def failing(path):
            raise RuntimeError("nope")

        self.bridge.bndb_open = failing
        _, reply = self.run_bndb(path)
        self.assertFalse(reply["ok"])
        self.assertIn("nope", reply["error"])
        self.assertEqual(self.request("GET", "/run")[1]["bndb"], [])
        self.bridge.bndb_open = lambda p: None     # None: also a failure
        _, reply = self.run_bndb(path)
        self.assertFalse(reply["ok"])
        self.assertIn("couldn't open", reply["error"])
        self.assertEqual(self.request("GET", "/run")[1]["bndb"], [])
        self.bridge.bndb_open = lambda p: CachedView(p)   # good again
        _, reply = self.run_bndb(path)
        self.assertTrue(reply["ok"])

    def test_no_hooks_is_a_400(self):
        self.bridge.bndb_open = None
        status, reply = self.run_bndb(self.path())
        self.assertEqual((status, reply["error"]), (400, bridge_server.BNDB_NO_HOOKS))

    def test_unsaved_changes_flag(self):
        dirty = {}
        self.bridge.bndb_modified = lambda v: dirty.get(v.path, False)
        path = self.path()
        self.run_bndb(path)
        self.assertEqual(self.request("GET", "/run")[1]["bndb"][0]["modified"], False)
        dirty[path] = True                     # the next run edits it
        self.run_bndb(path)
        self.assertIn("cached views: target.bndb (unsaved changes)", self.bnrun("--status").stdout)
        self.assertIn("(unsaved changes)", self.bnrun("--bndb-list").stdout)
        self.assertEqual(self.request("POST", "/bndb", {"action": "save"})[0], 200)
        self.assertEqual(self.request("GET", "/run")[1]["bndb"][0]["modified"], False)
        dirty[path] = True
        self.run_bndb(path, code="save_bndb()")    # the end of the run asks again
        self.assertTrue(self.request("GET", "/run")[1]["bndb"][0]["modified"])

    def test_status_lists_cached_views(self):
        path = self.path()
        self.run_bndb(path)
        status, reply = self.request("GET", "/run")
        self.assertEqual(reply["bndb"], [{"path": os.path.abspath(path),
                                          "name": "target.bndb", "modified": None}])
        r = self.bnrun("--status")
        self.assertEqual(r.returncode, 0)
        self.assertIn("cached views: target.bndb", r.stdout)
        # Idle line unchanged (bnrestart depends on it)
        self.assertIn("no script running", r.stdout)

    def test_status_of_a_running_bndb_run_shows_it(self):
        path = self.path()
        t, box = self.run_async({"code": BUSY, "bndb": path})
        try:
            running = self.wait_running()
            self.assertEqual(running["bndb"], path)
            self.assertIn("bndb %s" % path, bridge_server.describe_run(running))   # full path
        finally:
            self.request("POST", "/cancel", {"force": True})
            t.join(10)
        self.assertFalse(t.is_alive())
        _, reply = box["reply"]
        self.assertEqual(reply["error"], "cancelled")

        status, reply = self.request("GET", "/run")
        self.assertEqual(reply["bndb"], [{"path": os.path.abspath(path),
                                          "name": "target.bndb", "modified": None}])
        self.assertIn("cached views: target.bndb", bridge_server.describe_runs(
            self.request("GET", "/run")[1]))

    def test_post_bndb_save_close_all_and_one(self):
        path_a, path_b = self.path("a.bndb"), self.path("b.bndb")
        self.run_bndb(path_a)
        self.run_bndb(path_b)
        status, reply = self.request("POST", "/bndb", {"action": "save"})
        self.assertEqual(status, 200)
        self.assertTrue(reply["ok"])
        views = {p: self.bridge.bndb_views[os.path.abspath(p)] for p in (path_a, path_b)}
        self.assertTrue(all(v.saved == 1 for v in views.values()))
        status, reply = self.request("POST", "/bndb", {"action": "close", "path": path_a})
        self.assertEqual(status, 200)
        self.assertTrue(reply["ok"])
        self.assertTrue(views[path_a].closed)
        self.assertNotIn(os.path.abspath(path_a), self.bridge.bndb_views)
        self.assertIn(os.path.abspath(path_b), self.bridge.bndb_views)
        # closing an unknown path: 404
        status, reply = self.request("POST", "/bndb", {"action": "close",
                                                       "path": "/nope.bndb"})
        self.assertEqual(status, 404)
        # bad action: 400
        status, reply = self.request("POST", "/bndb", {"action": "reload"})
        self.assertEqual(status, 400)
        # empty cache with no path: a no-op success, not a 404 (bnrestart relies on it)
        self.request("POST", "/bndb", {"action": "close"})
        status, reply = self.request("POST", "/bndb", {"action": "save"})
        self.assertEqual((status, reply["ok"], reply["results"]), (200, True, []))

    def test_post_bndb_refused_while_running_or_locked(self):
        path = self.path()
        self.run_bndb(path)
        t, box = self.run_async({"code": BUSY, "bndb": path})
        try:
            running = self.wait_running()
            status, reply = self.request("POST", "/bndb", {"action": "save"})
            self.assertEqual(status, 409)
            self.assertIn("a script is running", reply["error"])
        finally:
            self.request("POST", "/cancel", {"force": True})
            t.join(10)
        self.assertFalse(t.is_alive())
        self.hold_lock("other")                      # a plain lock: cached views are no UI
        status, reply = self.request("POST", "/bndb", {"action": "save"})
        self.assertEqual(status, 200)
        self.request("POST", "/lock", {"session": "other", "exclusive": True})
        status, reply = self.request("POST", "/bndb", {"action": "save"})
        self.assertEqual(status, 423)

    def test_post_bndb_save_failure_reports_per_entry(self):
        path = self.path()
        self.run_bndb(path)
        original = self.bridge.bndb_save
        self.bridge.bndb_save = lambda v: (_ for _ in ()).throw(RuntimeError("disk full"))
        try:
            status, reply = self.request("POST", "/bndb", {"action": "save"})
            self.assertEqual(status, 200)
            self.assertFalse(reply["ok"])
            self.assertIn("disk full", reply["results"][0]["error"])
        finally:
            self.bridge.bndb_save = original

    def test_bndb_close_all(self):
        path = self.path()
        self.run_bndb(path)
        view = self.bridge.bndb_views[os.path.abspath(path)]
        self.bridge.bndb_close_all()
        self.assertTrue(view.closed)
        self.assertEqual(self.request("GET", "/run")[1]["bndb"], [])
        _, reply = self.run_bndb(path)      # reopens after close_all
        self.assertTrue(reply["ok"])
        self.assertEqual(len(self.opened), 2)

    def test_bnrun_bndb_flags(self):
        path = self.path()
        r = self.bnrun("--bndb", path, "-e", "result = bv.path")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, path + "\n")
        r = self.bnrun("--bndb-list")
        self.assertEqual(r.returncode, 0)
        self.assertIn(os.path.abspath(path), r.stdout)
        r = self.bnrun("--bndb-save", path)
        self.assertEqual(r.returncode, 0)
        self.assertIn("saved", r.stdout)
        r = self.bnrun("--bndb-close", path)
        self.assertEqual(r.returncode, 0)
        self.assertIn("closed", r.stdout)
        self.assertEqual(self.request("GET", "/run")[1]["bndb"], [])
        r = self.bnrun("--bndb", path, "--view", "x")
        self.assertEqual(r.returncode, 2)
        r = self.bnrun("--bndb", os.path.join(self.tmp.name, "missing.bndb"), "-e", "pass")
        self.assertEqual(r.returncode, 2)


class JobsTest(BridgeFixture):
    """Async jobs (/run async + /result), the spool, lost jobs and bnrun's job commands."""

    def setUp(self):
        super().setUp()
        self.bridge.stop()
        self.spool = os.path.join(self.tmp.name, "spool")
        self.gate = threading.Event()
        self.hits = []
        self.bridge = self.make_bridge()
        self.addCleanup(self.gate.set)

    def make_bridge(self):
        bridge = bridge_server.ScriptBridge(self.token, namespace, spool_dir=self.spool,
                                            base_namespace={"gate": self.gate,
                                                            "hits": self.hits})
        bridge.start()
        with open(self.config, "w") as f:
            json.dump({"port": bridge.port, "token": self.token}, f)
        return bridge

    def crash_and_restart(self):
        """A new bridge on the same spool, as if Binary Ninja had died: this process's jobs
        no longer count as alive."""
        self.bridge.stop()
        bridge_server._LIVE_RUN_IDS.clear()
        self.bridge = self.make_bridge()

    def submit(self, code, run_id, **extra):
        return self.request("POST", "/run", dict({"code": code, "run_id": run_id,
                                                  "async": True}, **extra))

    def wait_state(self, run_id, state):
        for _ in range(500):
            if self.request("POST", "/result", {"run_id": run_id})[1].get("state") == state:
                return
            time.sleep(0.01)
        self.fail("job %s never got to %s" % (run_id, state))

    def test_async_submit_and_long_poll(self):
        status, reply = self.submit("gate.wait(); result = 42", "j1")
        self.assertEqual((status, reply["run_id"], reply["job"]), (202, "j1", "j1"))
        self.wait_state("j1", "running")
        self.assertEqual(sorted(os.listdir(self.spool)), ["done", "j1.json", "lost"])
        threading.Timer(0.3, self.gate.set).start()
        status, reply = self.request("POST", "/result", {"run_id": "j1", "wait": 5})
        self.assertEqual((status, reply["state"], reply["reply"]["result"]), (200, "done", 42))
        self.assertEqual(sorted(os.listdir(self.spool)), ["done", "lost"])   # unspooled
        self.assertEqual(os.listdir(os.path.join(self.spool, "done")), ["j1.json"])
        self.assertEqual(self.request("POST", "/result", {"run_id": "nope"})[0], 404)

    def test_resubmitting_a_run_id_never_runs_twice(self):
        for _ in range(3):
            self.assertEqual(self.submit("hits.append(1)", "same")[0], 202)
        self.request("POST", "/result", {"run_id": "same", "wait": 5})
        self.assertEqual(self.hits, [1])

    def test_bad_run_id_refused(self):
        self.assertEqual(self.submit("pass", "../etc/x")[0], 400)

    def test_queued_jobs_listed_with_full_bndb_path_in_status(self):
        self.submit("gate.wait()", "busy", session="s", name="first")
        self.wait_state("busy", "running")
        self.submit("pass", "waiting", session="s", name="second")
        self.wait_state("waiting", "queued")
        status = json.loads(self.bnrun("--status", "--json").stdout)
        self.assertEqual([(j["job"], j["state"]) for j in status["jobs"]],
                         [("busy", "running"), ("waiting", "queued")])
        text = self.bnrun("--status").stdout
        self.assertIn("queued: 1 serialized, 0 parallel\n  queued script 'pass'", text)
        self.assertNotIn("no script running", text)
        jobs = self.bnrun("--jobs", "--session", "s").stdout
        self.assertIn("busy  running script", jobs)
        self.assertIn("waiting  queued script", jobs)

    def test_lost_jobs_reported_requeued_and_forgotten(self):
        self.submit("gate.wait()", "was-running", session="s", name="synthv")
        self.wait_state("was-running", "running")
        self.submit("hits.append('again')", "was-queued", session="s", name="synthv",
                    args=["<bnrun>", "R:1:x"])
        self.wait_state("was-queued", "queued")
        self.crash_and_restart()
        self.assertEqual(sorted(self.bridge.lost), ["was-queued", "was-running"])
        self.assertIn("lost: 2 jobs", self.bnrun("--status").stdout)
        self.assertEqual(json.loads(self.bnrun("--status", "--json").stdout)["lost"], 2)
        # a client still waiting learns the job is lost
        r = self.bnrun("--wait", "was-queued")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("bnrun --requeue was-queued", r.stderr)
        r = self.bnrun("--jobs")
        self.assertIn("was-running  lost script 'gate.wait()' (was running; synthv", r.stdout)
        # the queued one reruns (with its args); the running one needs --force
        r = self.bnrun("--requeue", "all", "--session", "s2", "-e", "x")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("was-running: was running when its bridge died", r.stderr)
        self.assertIn("requeued was-queued as", r.stderr)
        self.assertEqual(self.hits, ["again"])
        self.assertEqual(list(self.bridge.lost), ["was-running"])
        self.assertEqual(self.bnrun("--forget", "was-running").stdout, "forgot was-running\n")
        self.assertEqual(self.bridge.lost, {})
        self.assertEqual(os.listdir(os.path.join(self.spool, "lost")), [])
        self.crash_and_restart()                                     # nothing comes back
        self.assertEqual(self.bridge.lost, {})

    def test_requeue_force_reruns_a_running_one(self):
        self.submit("hits.append(1); gate.wait()", "r", session="s")
        self.wait_state("r", "running")
        self.crash_and_restart()
        self.gate.set()
        r = self.bnrun("--requeue", "r", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.hits, [1, 1])

    def test_stop_start_in_one_process_loses_nothing_alive(self):
        self.submit("gate.wait()", "alive")
        self.wait_state("alive", "running")
        self.bridge.stop()
        self.bridge = self.make_bridge()          # same process: the job is still running
        self.assertEqual(self.bridge.lost, {})

    def test_finished_job_still_answers_after_a_crash(self):
        self.submit("print('applied'); result = 7", "fin", session="s", name="synthv")
        self.request("POST", "/result", {"run_id": "fin", "wait": 5})
        self.crash_and_restart()
        status, reply = self.request("POST", "/result", {"run_id": "fin"})
        self.assertEqual((status, reply["state"], reply["previous_bridge"]), (200, "done", True))
        self.assertEqual((reply["reply"]["stdout"], reply["reply"]["result"]), ("applied\n", 7))
        r = self.bnrun("--wait", "fin")
        self.assertEqual((r.returncode, r.stdout), (0, "applied\n7\n"), r.stderr)
        self.assertIn("fin  done", self.bnrun("--jobs", "--session", "s").stdout)
        self.assertEqual(self.bridge.lost, {})

    def test_crash_between_done_and_unspool_is_not_lost(self):
        self.submit("pass", "both", session="s")
        self.request("POST", "/result", {"run_id": "both", "wait": 5})
        with open(os.path.join(self.spool, "both.json"), "w") as f:    # left behind
            json.dump({"run_id": "both", "state": "running", "request": {"code": "pass"}}, f)
        self.crash_and_restart()
        self.assertEqual(self.bridge.lost, {})
        self.assertEqual(self.request("POST", "/result", {"run_id": "both"})[1]["state"], "done")

    def test_done_records_expire(self):
        self.submit("pass", "old")
        self.request("POST", "/result", {"run_id": "old", "wait": 5})
        path = os.path.join(self.spool, "done", "old.json")
        past = time.time() - bridge_server.JOB_KEEP_SECONDS - 10
        os.utime(path, (past, past))
        self.crash_and_restart()
        self.assertFalse(os.path.exists(path))
        self.assertEqual(self.request("POST", "/result", {"run_id": "old"})[0], 404)

    def test_concurrent_requeues_run_a_lost_job_once(self):
        self.submit("gate.wait()", "busy", session="s")
        self.wait_state("busy", "running")
        self.submit("hits.append(1)", "once", session="s")
        self.wait_state("once", "queued")
        self.crash_and_restart()        # (the old bridge's threads live on: keep the gate shut)
        check = self.bridge.check_run

        def slow_check(req):
            time.sleep(0.3)         # widen the window between reading and resubmitting
            return check(req)
        self.bridge.check_run = slow_check
        replies = []
        threads = [threading.Thread(target=lambda: replies.append(self.request(
            "POST", "/lost", {"action": "requeue", "run_ids": ["once"], "session": "s"})[1]))
            for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(sorted(r["ok"] for r in replies), [False, True])
        new_id = [r for r in replies if r["ok"]][0]["results"][0]["new_run_id"]
        self.request("POST", "/result", {"run_id": new_id, "wait": 5})
        self.assertEqual(self.hits, [1])

    def test_refused_requeue_keeps_the_lost_job(self):
        self.submit("gate.wait()", "r", session="s")
        self.wait_state("r", "running")
        self.crash_and_restart()
        reply = self.request("POST", "/lost", {"action": "requeue", "run_ids": ["r"]})[1]
        self.assertFalse(reply["ok"])                  # was running: needs force
        self.assertEqual(list(self.bridge.lost), ["r"])
        self.assertTrue(os.path.exists(os.path.join(self.spool, "lost", "r.json")))

    def test_bnrun_cancel_one_job_by_run_id(self):
        self.submit("gate.wait()", "first", session="s")
        self.wait_state("first", "running")
        self.submit("hits.append(1)", "second", session="s")
        self.wait_state("second", "queued")
        r = self.bnrun("--cancel", "second", "--session", "s")
        self.assertEqual(r.stdout, "cancel sent: job second isn't running; it won't start\n")
        self.gate.set()
        reply = self.request("POST", "/result", {"run_id": "second", "wait": 5})[1]
        self.assertEqual(reply["reply"]["error"], "cancelled")
        self.assertEqual(self.hits, [])
        self.assertTrue(self.request("POST", "/result", {"run_id": "first", "wait": 5})[1]
                        ["reply"]["ok"])

    def test_bnrun_detach_and_wait(self):
        r = self.bnrun("--detach", "-e", "gate.wait(); print('late')")
        self.assertEqual(r.returncode, 0, r.stderr)
        run_id = r.stdout.strip()
        self.assertRegex(run_id, "^[0-9a-f]{32}$")
        threading.Timer(0.5, self.gate.set).start()
        r = self.bnrun("--wait", run_id)
        self.assertEqual((r.returncode, r.stdout), (0, "late\n"))

    def test_bnrun_waits_across_an_unreachable_bridge(self):
        self.submit("gate.wait(); print('done')", "j")
        self.wait_state("j", "running")
        port = self.bridge.port
        self.bridge.httpd.shutdown()                 # unreachable for a while, job goes on
        self.bridge.httpd.server_close()
        env = dict(os.environ, BN_SCRIPT_BRIDGE_CONFIG=self.config)
        proc = subprocess.Popen([sys.executable, BNRUN, "--wait", "j"], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env)
        time.sleep(3)
        self.bridge.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port),
                                                            self.bridge.httpd.RequestHandlerClass)
        self.bridge.start()
        self.gate.set()
        out, err = proc.communicate(timeout=30)
        self.assertEqual((proc.returncode, out), (0, "done\n"), err)
        self.assertIn("still waiting for job j", err)
        self.assertIn("bridge back", err)


class BndbOpenHangTest(BridgeFixture):
    """A --bndb open stuck inside Binary Ninja: --timeout and --cancel abandon it and free the
    lane; the path is refused until the open finishes (then its view is cached)."""

    run_async = CancelTest.run_async
    path = BndbCacheTest.path
    run_bndb = BndbCacheTest.run_bndb

    def setUp(self):
        super().setUp()
        self.opened = []
        self.release = threading.Event()
        self.addCleanup(self.release.set)

        def stuck_open(path):
            self.opened.append(path)
            if "stuck" in path:
                self.release.wait()
            return CachedView(path)
        self.bridge.bndb_open = stuck_open

    def test_timeout_covers_the_open_and_frees_the_lane(self):
        stuck = self.path("stuck.bndb")
        t, box = self.run_async({"code": "result = 1", "bndb": stuck, "timeout": 0.5})
        for _ in range(100):
            running = self.request("GET", "/run")[1]["running"]
            if running and running["state"] == "opening":
                break
            time.sleep(0.01)
        self.assertEqual(running["state"], "opening")
        self.assertIn("opening the bndb for script", bridge_server.describe_run(running))
        t.join(5)
        reply = box["reply"][1]
        self.assertEqual((reply["error"], reply["cancelled_by"]),
                         ("cancelled", "timeout after 0.5s (opening the bndb)"))
        self.assertIn("abandoned opening %s" % stuck, reply["detail"])
        # the lane is free: other runs go ahead; the stuck path is refused at once
        other = self.path("other.bndb")
        self.assertEqual(self.run_bndb(other)[1]["result"], other)
        refused = self.run_bndb(stuck)[1]
        self.assertIn("is still opening inside Binary Ninja", refused["error"])
        self.assertEqual(self.opened.count(stuck), 1)
        status = self.bnrun("--status").stdout
        self.assertIn("still opening: %s" % stuck, status)
        # the open finishing later caches its view
        self.release.set()
        for _ in range(100):
            if not self.bridge.bndb_hung:
                break
            time.sleep(0.01)
        self.assertEqual(self.run_bndb(stuck)[1]["result"], stuck)
        self.assertEqual(self.opened.count(stuck), 1)

    def test_cancel_lands_during_the_open(self):
        stuck = self.path("stuck.bndb")
        t, box = self.run_async({"code": "result = 1", "bndb": stuck, "session": "s",
                                 "run_id": "r"})
        for _ in range(100):
            if (self.request("GET", "/run")[1]["running"] or {}).get("state") == "opening":
                break
            time.sleep(0.01)
        self.assertEqual(self.request("POST", "/cancel", {"session": "s"})[0], 200)
        t.join(5)
        self.assertEqual(box["reply"][1]["cancelled_by"], "session s")
        self.assertIsNone(self.request("GET", "/run")[1]["running"])

    def test_progress_reported_and_abort_stops_a_bndb_load(self):
        steps = []

        def loading(path, progress):
            for i in range(1000):                 # a slow .bndb load that reports progress
                steps.append(i)
                if not progress(i, 1000):
                    raise RuntimeError("load aborted")
                time.sleep(0.01)
            return CachedView(path)
        self.bridge.bndb_open = loading
        path = self.path("big.bndb")
        t, box = self.run_async({"code": "result = 1", "bndb": path, "timeout": 0.6})
        for _ in range(100):
            running = self.request("GET", "/run")[1]["running"]
            if running and running.get("open_progress"):
                break
            time.sleep(0.01)
        self.assertRegex(bridge_server.describe_run(running),
                         r"opening the bndb \(\d+% loaded\) for script")
        t.join(5)
        self.assertEqual(box["reply"][1]["cancelled_by"], "timeout after 0.6s (opening the bndb)")
        for _ in range(100):                      # the load saw False and ended
            if not self.bridge.bndb_hung:
                break
            time.sleep(0.01)
        self.assertEqual(self.bridge.bndb_hung, {})
        self.assertEqual(self.bridge.bndb_views, {})    # aborted: nothing cached
        self.assertLess(len(steps), 1000)

    def test_hooks_without_progress_still_work(self):
        self.assertFalse(self.bridge._hook_takes("progress"))     # stuck_open(path)
        self.assertEqual(self.run_bndb(self.path("plain.bndb"))[1]["ok"], True)

    def test_script_timeout_gets_what_the_open_left(self):
        def slow_open(path):
            time.sleep(0.6)
            return CachedView(path)
        self.bridge.bndb_open = slow_open
        reply = self.run_bndb(self.path(), code="import time; time.sleep(0.05)\n"
                              "while True: pass", timeout=1)[1]
        self.assertEqual(reply["cancelled_by"], "timeout after 1s")


class BnrestartBusyTest(unittest.TestCase):
    """bnrestart decides idle from `bnrun --status --json`, not from the prose."""

    def setUp(self):
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("bnrestart", os.path.join(ROOT, "bnrestart"))
        spec = importlib.util.spec_from_loader("bnrestart", loader)
        self.mod = importlib.util.module_from_spec(spec)
        loader.exec_module(self.mod)

    def test_busy_reasons(self):
        idle = {"running": None, "parallel": [], "queued": 0, "parallel_queued": 0,
                "bndb_hung": [{"path": "/x.bndb", "stuck_for": 9}], "jobs": []}
        self.assertEqual(self.mod.busy_reasons(idle), [])     # a stuck open doesn't block it
        self.assertEqual(self.mod.busy_reasons(dict(idle, running={"label": "x"}, queued=2)),
                         ["1 script(s) running", "2 queued"])
        self.assertEqual(self.mod.busy_reasons(dict(idle, parallel=[{}], parallel_queued=1)),
                         ["1 script(s) running", "1 queued"])
        self.assertEqual(self.mod.busy_reasons({}), [])       # older bridge: missing keys


class RunScriptExitTest(unittest.TestCase):
    """sys.exit() in a script: a status or message, never a traceback."""

    def test_exit_codes(self):
        run = bridge_server.run_script
        self.assertEqual(run("import sys\nresult = 1\nsys.exit()", {})["result"], 1)
        self.assertTrue(run("import sys\nsys.exit(0)", {})["ok"])
        reply = run("import sys\nprint('x')\nsys.exit('bad batch')", {})
        self.assertEqual((reply["ok"], reply["stdout"], reply["error"]),
                         (False, "x\n", "bad batch\n"))
        reply = run("raise SystemExit(3)", {})
        self.assertEqual((reply["ok"], reply["error"]), (False, "script exited with status 3\n"))


class FakeSymbol:
    def __init__(self, type, address, name):
        self.type, self.address, self.name = type, address, name


class FakeSettings:
    VALUES = {"analysis.limits.maxFunctionSize": 65536, "analysis.mode": "full",
              "ui.theme": "dark"}

    def keys(self):
        return list(self.VALUES)

    def get_json(self, key):
        return json.dumps(self.VALUES[key])


FAKE_BN = type(sys)("binaryninja")
FAKE_BN.Symbol = FakeSymbol
FAKE_BN.SymbolType = type("SymbolType", (), {"FunctionSymbol": "Function", "DataSymbol": "Data"})
FAKE_BN.Settings = FakeSettings


# An IntEnum like Binary Ninja's AnalysisSkipReason: str() of one is just the number.
SkipReason = enum.IntEnum("AnalysisSkipReason", [("NoSkipReason", 0), ("ExceedFunctionSize", 2)])


class FakeFunction:
    def __init__(self, view, name, start, end, skipped=False):
        self.view, self.name, self.start, self.highest_address = view, name, start, end
        self.analysis_skipped = skipped
        self.analysis_skip_reason = SkipReason.ExceedFunctionSize if skipped else \
            SkipReason.NoSkipReason
        self.comments = {}

    def get_comment_at(self, addr):
        return self.comments.get(addr, "")

    def set_comment_at(self, addr, text):
        self.comments[addr] = text

    @property
    def hlil(self):
        return "stub" if self.analysis_skipped else "int32_t %s()\n{\n    return 0;\n}" % self.name


class FakeBinaryView:
    """Just enough BinaryView for the bundled scripts: functions at 0x1000 and 0x2000, valid
    addresses 0x1000..0x10000."""

    def __init__(self, path):
        self.path = path
        self.saved = 0
        self.functions = {0x1000: FakeFunction(self, "sub_1000", 0x1000, 0x10ff),
                          0x2000: FakeFunction(self, "sub_2000", 0x2000, 0x8fff, skipped=True)}
        self.symbols = {}
        self.comments = {}
        self.unskip_requested = False

    def get_function_at(self, addr):
        return self.functions.get(addr)

    def get_functions_containing(self, addr):
        return [f for f in self.functions.values() if f.start <= addr <= f.highest_address]

    def is_valid_offset(self, addr):
        return 0x1000 <= addr < 0x10000

    def define_user_symbol(self, sym):
        self.symbols[sym.address] = sym
        if sym.type == "Function":
            self.functions[sym.address].name = sym.name

    def get_symbol_at(self, addr):
        return self.symbols.get(addr)

    def get_comment_at(self, addr):
        return self.comments.get(addr, "")

    def set_comment_at(self, addr, text):
        self.comments[addr] = text

    def update_analysis_and_wait(self):
        pass        # the fake's HLIL only depends on analysis_skipped


class BundledScriptsTest(BridgeFixture):
    """scripts/ (annotate, info, decompile) run by bare name, and the bnannotate wrapper."""

    def setUp(self):
        super().setUp()
        saved = sys.modules.get("binaryninja")
        sys.modules["binaryninja"] = FAKE_BN     # the scripts `import binaryninja as bn`
        self.addCleanup(lambda: sys.modules.pop("binaryninja") if saved is None
                        else sys.modules.__setitem__("binaryninja", saved))
        self.opened = []

        def open_view(path):
            self.opened.append(path)
            return FakeBinaryView(path)

        def save_view(v):
            v.saved += 1
            return True

        self.bridge.bndb_open = open_view
        self.bridge.bndb_save = save_view
        self.bridge.bndb_close = lambda v: None
        # realpath: macOS /var is a symlink, and bnrun's abspath of a relative path sees the cwd
        self.db = os.path.join(os.path.realpath(self.tmp.name), "target.bndb")
        with open(self.db, "w") as f:
            f.write("x")

    def view(self):
        return self.bridge.bndb_views[self.db]

    def run_tool(self, *args, tool=BNRUN, cwd=None):
        env = dict(os.environ, BN_SCRIPT_BRIDGE_CONFIG=self.config)
        return subprocess.run([sys.executable, tool, *args], capture_output=True, text=True,
                              env=env, timeout=30, cwd=cwd)

    def test_annotate_applies_renames_and_comments(self):
        r = self.run_tool("--bndb", self.db, "annotate", "--", "R:0x1000:parse_header",
                          "D:3000:voice_table", "C:0x1010:checks the magic",
                          "C:0x9500:a data comment")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), [
            "R 0x1000 sub_1000 -> parse_header", "D 0x3000 (none) -> voice_table",
            "C 0x1010 comment set", "C 0x9500 comment set"])
        v = self.view()
        self.assertEqual(v.functions[0x1000].name, "parse_header")
        self.assertEqual(v.symbols[0x3000].name, "voice_table")
        self.assertEqual(v.functions[0x1000].comments, {0x1010: "checks the magic"})
        self.assertEqual(v.comments, {0x9500: "a data comment"})
        r = self.run_tool("--bndb", self.db, "annotate", "--", "C:0x1010:new: with colon")
        self.assertEqual(r.stdout, "C 0x1010 comment set (was: checks the magic)\n")
        self.assertEqual(v.functions[0x1000].comments[0x1010], "new: with colon")

    def test_annotate_bad_directive_applies_nothing(self):
        r = self.run_tool("--bndb", self.db, "annotate", "--", "R:0x1000:ok_name",
                          "R:0x1004:inside_not_start", "R:0x1000", "C:zz:x", "D:0x99:low",
                          "X:1:2")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stderr, "annotate: nothing applied (5 bad directives)\n")
        self.assertEqual(len(r.stdout.splitlines()), 5)
        self.assertIn("no function starts at 0x1004", r.stdout)
        self.assertIn("bad address 'zz'", r.stdout)
        self.assertEqual(self.view().functions[0x1000].name, "sub_1000")
        r = self.run_tool("--bndb", self.db, "annotate")
        self.assertEqual(r.returncode, 1)
        self.assertIn("annotate: no directives", r.stderr)

    def test_info(self):
        r = self.run_tool("--bndb", self.db, "info", "--", "F:0x2000", "F:0x1004", "F:0x9999",
                          "S:ANALYSIS")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), [
            "F:0x2000 sub_2000 @ 0x2000..0x8fff skipped=True reason=ExceedFunctionSize",
            "F:0x1004 sub_1000 @ 0x1000..0x10ff skipped=False reason=NoSkipReason",
            "F:0x9999 no function at 0x9999",
            "S:ANALYSIS analysis.limits.maxFunctionSize = 65536",
            'S:ANALYSIS analysis.mode = "full"'])
        r = self.run_tool("--bndb", self.db, "info", "--", "F:nope", "Q")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stderr, "info: 2 bad queries\n")

    def test_decompile_unskips(self):
        r = self.run_tool("--bndb", self.db, "decompile", "--", "0x2000")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "int32_t sub_2000()\n{\n    return 0;\n}\n")
        self.assertFalse(self.view().functions[0x2000].analysis_skipped)
        r = self.run_tool("--bndb", self.db, "decompile", "--", "0x1000", "rel.hlil")
        self.assertEqual(r.returncode, 1)
        self.assertIn("OUT must be an absolute path", r.stderr)
        out = os.path.join(self.tmp.name, "f.hlil")
        r = self.run_tool("--bndb", self.db, "decompile", "--", "0x1000", out)
        self.assertEqual(r.stdout, "sub_1000: wrote %s (4 lines)\n" % out)
        with open(out) as f:
            self.assertTrue(f.read().startswith("int32_t sub_1000()"))
        r = self.run_tool("--bndb", self.db, "decompile", "--", "0x1004")
        self.assertEqual((r.returncode, r.stderr),
                         (1, "decompile: no function starts at 0x1004\n"))

    def test_script_lookup_and_relative_bndb(self):
        # Not ending in .py: the bundled library, even with a same-named file here (also with
        # a slash); ending in .py: a file.
        with open(os.path.join(self.tmp.name, "info"), "w") as f:
            f.write("result = 'local file'")
        with open(os.path.join(self.tmp.name, "info.py"), "w") as f:
            f.write("result = 'local file'")
        r = self.run_tool("--bndb", "target.bndb", "info", "--", "S:analysis.mode",
                          cwd=self.tmp.name)
        self.assertEqual(r.stdout, 'S:analysis.mode analysis.mode = "full"\n')
        self.assertEqual(self.opened, [self.db])     # sent as an absolute path
        r = self.run_tool("--bndb", "target.bndb", "info.py", cwd=self.tmp.name)
        self.assertEqual(r.stdout, "local file\n")
        r = self.run_tool("--bndb", "target.bndb", "./info", cwd=self.tmp.name)
        self.assertEqual(r.returncode, 2)
        r = self.run_tool("--bndb", self.db, "nosuch")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no bundled script 'nosuch' (bundled: annotate, decompile, info); "
                      "a file must end in .py", r.stderr)
        r = self.run_tool("--bndb-save", "target.bndb", cwd=self.tmp.name)
        self.assertEqual((r.returncode, self.view().saved), (0, 1))

    def test_bnannotate(self):
        r = self.run_tool("--bndb", self.db, "R:0x1000:parse_header", "--save",
                          "--", "C:0x1010:x", tool=BNANNOTATE)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), ["R 0x1000 sub_1000 -> parse_header",
                                                 "C 0x1010 comment set", "saved " + self.db])
        self.assertEqual(self.view().saved, 1)
        r = self.run_tool("--bndb", self.db, "R:0x1004:bad", "--save", tool=BNANNOTATE)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.view().saved, 1)      # failed edits: no save
        r = self.run_tool("annotate", "--", "--save", "R:0x1000:x")    # UI view: no save_bndb
        self.assertIn("--save needs a --bndb run", r.stderr)
        r = self.run_tool("R:0x1000:x", "--save", tool=BNANNOTATE)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--save needs --bndb", r.stderr)


class UIViewRulesTest(BridgeFixture):
    """UI views (bv, bvs, view, main_thread) only for a serialized script of the lock holder."""

    def setUp(self):
        super().setUp()
        self.bridge.base_namespace = {"bn": "the-api"}

    def run_code(self, code, **kw):
        return self.request("POST", "/run", dict(kw, code=code))

    def test_without_lock_bv_and_bvs_refuse_but_the_api_is_there(self):
        _, reply = self.run_code("result = bn")
        self.assertEqual(reply["result"], "the-api")
        for code in ("bv.functions", "len(bvs)", "list(bvs)", "bool(bv)", "bvs[0]"):
            _, reply = self.run_code(code)
            self.assertFalse(reply["ok"], code)
            self.assertIn("UIViewsUnavailable", reply["error"], code)
            self.assertIn("take the usage lock first: bnrun --lock", reply["error"], code)
        _, reply = self.run_code("result = repr(bv)")
        self.assertIn("no UI views", reply["result"])
        _, reply = self.run_code("try:\n    bv.x\nexcept RuntimeError as e:\n    result = 'caught'")
        self.assertEqual(reply["result"], "caught")     # a RuntimeError, so scripts can test

    def test_view_and_main_thread_need_the_lock(self):
        for kw in ({"view": "cx20707"}, {"main_thread": True}):
            status, reply = self.run_code("pass", session="me", **kw)
            self.assertEqual(status, 409, kw)
            self.assertIn("take the usage lock first", reply["error"])
        self.assertEqual(self.main_calls, [])
        r = self.bnrun("--session", "me", "--view", "cx20707", "-e", "pass")
        self.assertEqual(r.returncode, 2)
        self.assertIn("take the usage lock first: bnrun --lock", r.stderr)
        self.hold_lock()
        _, reply = self.run_code("result = bv.file.filename", session="me", view="cx20707")
        self.assertEqual(reply["result"], "/x/cx20707.ko.bndb")
        _, reply = self.run_code("result = (len(bvs), bn)", session="me")
        self.assertEqual(reply["result"], [2, "the-api"])
        _, reply = self.run_code("result = 1", session="me", main_thread=True)
        self.assertEqual((reply["result"], len(self.main_calls)), (1, 1))
        _, reply = self.run_code("bv.file", session="other-but-lock-is-mine")
        self.assertEqual(reply.get("lock", {}).get("session"), "me")    # 423: locked

    def test_parallel_gets_no_ui_views_even_for_the_holder(self):
        self.hold_lock()
        status, reply = self.run_code("pass", session="me", parallel=True, view="cx20707")
        self.assertEqual(status, 400)
        self.assertIn("parallel scripts get no UI views", reply["error"])
        _, reply = self.run_code("bv.file", session="me", parallel=True)
        self.assertIn("parallel scripts get no UI views", reply["error"])
        _, reply = self.run_code("result = bn", session="me", parallel=True)
        self.assertEqual(reply["result"], "the-api")
        r = self.bnrun("--parallel", "--view", "x", "-e", "pass")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--parallel can't be combined with --view", r.stderr)

    def test_lock_lost_while_queued_refuses_the_view(self):
        self.hold_lock()
        self.bridge.base_namespace = {"gate": threading.Event(), "time": time}
        gate = self.bridge.base_namespace["gate"]
        box = {}
        t = threading.Thread(target=lambda: box.update(reply=self.run_code(
            "while not gate.is_set():\n    time.sleep(0.005)", session="me")), daemon=True)
        t.start()
        for _ in range(500):
            if self.bridge.running():
                break
            time.sleep(0.01)
        t2 = threading.Thread(target=lambda: box.update(reply2=self.run_code(
            "result = bv.file.filename", session="me", view="cx20707")), daemon=True)
        t2.start()
        for _ in range(500):
            if self.bridge.queued == 1:
                break
            time.sleep(0.01)
        self.request("POST", "/unlock", {"session": "me"})
        gate.set()
        t.join(10)
        t2.join(10)
        self.assertFalse(box["reply2"][1]["ok"])
        self.assertIn("take the usage lock first", box["reply2"][1]["error"])


class FakeMcpUpstream:
    """Stands in for Binary Ninja's MCP server: JSON replies, or SSE when asked."""

    def __init__(self, tools=("bn_function_list",)):
        self.tools = list(tools)
        self.requests = []
        upstream = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.headers.get("Mcp-Session-Id") in ("stale", "stale-other"):
                    msg = (b"Missing or invalid MCP-Session-Id" if
                           self.headers["Mcp-Session-Id"] == "stale" else b"bad params")
                    self.send_response(400)
                    self.send_header("Content-Length", str(len(msg)))
                    self.end_headers()
                    self.wfile.write(msg)
                    return
                upstream.requests.append((dict(self.headers), body))
                if body.get("method") == "tools/list":
                    result = {"tools": [{"name": n, "inputSchema": {}} for n in upstream.tools]}
                else:
                    result = {"content": [{"type": "text", "text": "upstream ok"}]}
                reply = json.dumps({"jsonrpc": "2.0", "id": body.get("id"), "result": result})
                sse = body.get("params", {}).get("sse")
                data = ("event: message\ndata: %s\n\n" % reply) if sse else reply
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream" if sse
                                 else "application/json")
                self.send_header("Mcp-Session-Id", "upstream-session-1")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data.encode())

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class McpProxyTest(unittest.TestCase):
    def setUp(self):
        self.upstream = FakeMcpUpstream()
        self.lock = bridge_server.UsageLock()
        self.proxy = bridge_server.McpProxy(self.lock, self.upstream.port)
        self.proxy.start()

    def tearDown(self):
        self.proxy.stop()
        self.upstream.stop()

    def rpc(self, method, params=None, session=None, mcp_session=None, headers=None, rid=1):
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy.port, timeout=10)
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if session:
            h["X-BN-Session"] = session
        if mcp_session:
            h["Mcp-Session-Id"] = mcp_session
        h.update(headers or {})
        conn.request("POST", "/mcp", body=json.dumps(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}), headers=h)
        resp = conn.getresponse()
        text = resp.read().decode()
        conn.close()
        return resp, text

    def call_tool(self, name, arguments=None, **kw):
        resp, text = self.rpc("tools/call", {"name": name, "arguments": arguments or {}}, **kw)
        return json.loads(text)["result"]

    def test_passes_through_and_strips_identity_headers(self):
        resp, text = self.rpc("initialize", session="sess-fe")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Mcp-Session-Id"), "upstream-session-1")
        headers, body = self.upstream.requests[-1]
        self.assertEqual(body["method"], "initialize")
        self.assertNotIn("X-BN-Session", headers)

    def test_tools_list_gets_owner_tools_json_and_sse(self):
        _, text = self.rpc("tools/list")
        names = [t["name"] for t in json.loads(text)["result"]["tools"]]
        self.assertEqual(names, ["bn_function_list", "bn_owner_get", "bn_owner_set"])
        resp, text = self.rpc("tools/list", {"sse": True})
        self.assertEqual(resp.getheader("Content-Type"), "text/event-stream")
        data = [l for l in text.split("\n") if l.startswith("data:")][0]
        names = [t["name"] for t in json.loads(data[5:])["result"]["tools"]]
        self.assertEqual(names, ["bn_function_list", "bn_owner_get", "bn_owner_set"])

    def test_owner_tools_and_gating(self):
        self.assertEqual(self.call_tool("bn_owner_get")["structuredContent"]["lock"],
                         {"locked": False})
        self.assertFalse(self.call_tool("bn_function_list", session="sess-audio").get("isError"))
        r = self.call_tool("bn_owner_set", {"name": "fe", "purpose": "ctl.bin"},
                           session="sess-fe")
        self.assertFalse(r["isError"])
        self.assertIn("locked by fe: ctl.bin", r["structuredContent"]["text"])
        n = len(self.upstream.requests)
        r = self.call_tool("bn_function_list", session="sess-audio")
        self.assertTrue(r["isError"])
        self.assertIn("locked by fe: ctl.bin", r["content"][0]["text"])
        self.assertEqual(len(self.upstream.requests), n)          # never reached Binary Ninja
        r = self.call_tool("bn_function_list", session="sess-fe")
        self.assertEqual(r["content"][0]["text"], "upstream ok")
        r = self.call_tool("bn_owner_get", session="sess-audio")  # owner tools never blocked
        self.assertEqual(r["structuredContent"]["lock"]["session"], "sess-fe")
        r = self.call_tool("bn_owner_set", {"purpose": "mine"}, session="sess-audio")
        self.assertTrue(r["isError"])
        r = self.call_tool("bn_owner_set", {"release": True}, session="sess-audio")
        self.assertTrue(r["isError"])
        r = self.call_tool("bn_owner_set", {"release": True, "force": True}, session="sess-audio")
        self.assertFalse(r["isError"])
        self.assertEqual(self.lock.status(), {"locked": False})

    def test_mcp_session_id_is_fallback_identity(self):
        r = self.call_tool("bn_owner_set", {"purpose": "p"}, mcp_session="conn-a")
        self.assertEqual(r["structuredContent"]["lock"]["session"], "conn-a")
        self.assertTrue(self.call_tool("bn_function_list", mcp_session="conn-b")["isError"])
        self.assertFalse(self.call_tool("bn_function_list", mcp_session="conn-a").get("isError"))
        r = self.call_tool("bn_owner_set", {"release": True})
        self.assertTrue(r["isError"])
        self.assertIn("no session identity", r["structuredContent"]["text"])

    def test_session_name_header_used_as_default_name(self):
        r = self.call_tool("bn_owner_set", {}, session="sess-fe",
                           headers={"X-BN-Session-Name": "buschjaeger-fe"})
        self.assertEqual(r["structuredContent"]["lock"]["name"], "buschjaeger-fe")

    def test_stale_session_becomes_404_other_400s_pass(self):
        resp, text = self.rpc("tools/list", mcp_session="stale")
        self.assertEqual(resp.status, 404)
        self.assertIn("re-initialize", text)
        resp, text = self.rpc("tools/list", mcp_session="stale-other")
        self.assertEqual((resp.status, text), (400, "bad params"))

    def test_rejects_browser_requests_and_reports_dead_upstream(self):
        resp, _ = self.rpc("tools/list", headers={"Origin": "http://evil.example"})
        self.assertEqual(resp.status, 403)
        self.upstream.stop()
        resp, _ = self.rpc("tools/list")
        self.assertEqual(resp.status, 502)


class McpAllowlistTest(unittest.TestCase):
    """The scriptbridge.mcpTools allowlist: filter tools/list, gate filtered tools/call."""

    TOOLS = ["bn_function_list", "bn_function_decompile", "bn_memory_read", "bn_other_thing"]

    def setUp(self):
        self.upstream = FakeMcpUpstream(self.TOOLS)
        self.lock = bridge_server.UsageLock()
        self.patterns = {"value": ""}
        self.proxy = bridge_server.McpProxy(self.lock, self.upstream.port,
                                            allowed_tools=lambda: self.patterns["value"])
        self.proxy.start()

    def tearDown(self):
        self.proxy.stop()
        self.upstream.stop()

    def rpc(self, method, params=None, session=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy.port, timeout=10)
        h = {"Content-Type": "application/json"}
        if session:
            h["X-BN-Session"] = session
        payload = body if body is not None else {"jsonrpc": "2.0", "id": 1, "method": method,
                                                 "params": params or {}}
        conn.request("POST", "/mcp", body=json.dumps(payload), headers=h)
        resp = conn.getresponse()
        text = resp.read().decode()
        conn.close()
        return resp, text

    def call_tool(self, name, arguments=None, **kw):
        resp, text = self.rpc("tools/call", {"name": name, "arguments": arguments or {}}, **kw)
        return json.loads(text)["result"]

    def tool_names(self, **kw):
        _, text = self.rpc("tools/list", **kw)
        return [t["name"] for t in json.loads(text)["result"]["tools"]]

    def test_empty_patterns_pass_everything_through(self):
        self.assertEqual(self.tool_names(), self.TOOLS + ["bn_owner_get", "bn_owner_set"])

    def test_patterns_filter_tools_list_json_and_sse(self):
        self.patterns["value"] = "bn_function_*,bn_memory_read"
        names = self.tool_names()
        self.assertEqual(names, ["bn_function_list", "bn_function_decompile", "bn_memory_read",
                                 "bn_owner_get", "bn_owner_set"])
        resp, text = self.rpc("tools/list", {"sse": True})
        data = [l for l in text.split("\n") if l.startswith("data:")][0]
        names = [t["name"] for t in json.loads(data[5:])["result"]["tools"]]
        self.assertEqual(names, ["bn_function_list", "bn_function_decompile", "bn_memory_read",
                                 "bn_owner_get", "bn_owner_set"])

    def test_patterns_are_read_live(self):
        self.patterns["value"] = "bn_memory_read"
        self.assertEqual(self.tool_names(), ["bn_memory_read", "bn_owner_get", "bn_owner_set"])
        self.patterns["value"] = "bn_other_thing"
        self.assertEqual(self.tool_names(), ["bn_other_thing", "bn_owner_get", "bn_owner_set"])

    def test_filtered_tool_call_is_answered_locally(self):
        self.patterns["value"] = "bn_memory_read"
        n = len(self.upstream.requests)
        r = self.call_tool("bn_other_thing")
        self.assertTrue(r["isError"])
        self.assertIn(bridge_server.TOOL_NOT_ALLOWED % "bn_other_thing",
                      r["content"][0]["text"])
        self.assertEqual(len(self.upstream.requests), n)      # never reached Binary Ninja
        r = self.call_tool("bn_memory_read")                  # allowed: forwarded
        self.assertFalse(r.get("isError"))
        self.assertEqual(r["content"][0]["text"], "upstream ok")

    def test_allowlist_and_lock_gate_together(self):
        self.patterns["value"] = "bn_memory_read"
        self.call_tool("bn_owner_set", {"name": "fe"}, session="sess-fe")
        r = self.call_tool("bn_other_thing", session="sess-audio")
        self.assertTrue(r["isError"])
        self.assertIn(bridge_server.TOOL_NOT_ALLOWED % "bn_other_thing",
                      r["content"][0]["text"])   # allowlist named, not the lock
        r = self.call_tool("bn_memory_read", session="sess-audio")   # allowed but locked out
        self.assertTrue(r.get("isError"))
        self.assertIn("locked by fe", r["content"][0]["text"])

    def test_owner_tools_bypass_the_allowlist(self):
        self.patterns["value"] = "bn_memory_read"
        self.assertFalse(self.call_tool("bn_owner_get")["isError"])
        r = self.call_tool("bn_owner_set", {"name": "x"}, session="s")
        self.assertFalse(r.get("isError"))

    def test_batch_with_a_filtered_call_is_wholly_answered(self):
        self.patterns["value"] = "bn_memory_read"
        body = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "bn_memory_read", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "bn_other_thing", "arguments": {}}},
        ]
        resp, text = self.rpc(None, body=body)
        replies = json.loads(text)
        self.assertEqual(len(replies), 2)
        self.assertTrue(replies[1]["result"]["isError"])
        self.assertIn(bridge_server.TOOL_NOT_ALLOWED % "bn_other_thing",
                      replies[1]["result"]["content"][0]["text"])


class McpProxyUpstreamTokenTest(unittest.TestCase):
    def test_sends_its_token_and_drops_the_clients(self):
        upstream = FakeMcpUpstream()
        proxy = bridge_server.McpProxy(bridge_server.UsageLock(), upstream.port,
                                       upstream_token="tok-123")
        proxy.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=10)
            conn.request("POST", "/mcp", body=json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer client-guess"})
            conn.getresponse().read()
            conn.close()
            headers, _ = upstream.requests[-1]
            self.assertEqual(headers.get("Authorization"), "Bearer tok-123")
        finally:
            proxy.stop()
            upstream.stop()


class SessionHeaderHelperTest(unittest.TestCase):
    """mcp-session-header: identity from the environment or ~/.claude/sessions/<ppid>.json."""

    def run_helper(self, env_session=None, sessions=None, name=None):
        with tempfile.TemporaryDirectory() as home:
            if sessions is not None:
                os.makedirs(os.path.join(home, ".claude", "sessions"))
                path = os.path.join(home, ".claude", "sessions", "%d.json" % os.getpid())
                with open(path, "w") as f:
                    json.dump(sessions, f)
            env = {k: v for k, v in os.environ.items()
                   if k not in ("CLAUDE_CODE_SESSION_ID", "BN_SESSION_NAME")}
            env["HOME"] = home
            if env_session:
                env["CLAUDE_CODE_SESSION_ID"] = env_session
            if name:
                env["BN_SESSION_NAME"] = name
            r = subprocess.run([sys.executable, os.path.join(ROOT, "mcp-session-header")],
                               capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(os.path.join(home, ".cache", "bn-mcp-session-header.log")) as f:
                log = f.read()
            return json.loads(r.stdout), log

    def test_from_environment_with_name_from_sessions_file(self):
        headers, log = self.run_helper("sess-a", {"sessionId": "sess-a", "name": "fe"})
        self.assertEqual(headers, {"X-BN-Session": "sess-a", "X-BN-Session-Name": "fe"})
        self.assertIn("source=env", log)

    def test_from_sessions_file_of_parent(self):
        headers, log = self.run_helper(None, {"sessionId": "sess-b", "name": "audio"})
        self.assertEqual(headers, {"X-BN-Session": "sess-b", "X-BN-Session-Name": "audio"})
        self.assertIn("source=sessions/%d.json" % os.getpid(), log)

    def test_name_override_and_foreign_sessions_file(self):
        headers, _ = self.run_helper("sess-a", {"sessionId": "sess-other", "name": "x"}, "mine")
        self.assertEqual(headers, {"X-BN-Session": "sess-a", "X-BN-Session-Name": "mine"})
        headers, _ = self.run_helper("sess-a", {"sessionId": "sess-other", "name": "x"})
        self.assertEqual(headers, {"X-BN-Session": "sess-a"})

    def test_nothing_found(self):
        headers, log = self.run_helper()
        self.assertEqual(headers, {})
        self.assertIn("source=none", log)


class McpProxyOpenGuardTest(unittest.TestCase):
    """The proxy refuses bn_open_item_open calls the plugin's open_guard rejects,
    so a .bndb is never opened twice (two database handles deadlock both saves)."""

    def setUp(self):
        self.upstream = FakeMcpUpstream()
        self.proxy = bridge_server.McpProxy(
            bridge_server.UsageLock(), self.upstream.port, port=0,
            open_guard=lambda p: ("already open: %s" % p) if "busy" in p else None)
        self.proxy.start()

    def tearDown(self):
        self.proxy.stop()
        self.upstream.stop()

    def call(self, name, path, ident=1):
        body = {"jsonrpc": "2.0", "id": ident, "method": "tools/call",
                "params": {"name": name, "arguments": {"path": path}}}
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy.port, timeout=10)
        conn.request("POST", "/mcp", json.dumps(body),
                     {"Content-Type": "application/json"})
        resp = conn.getresponse()
        reply = json.loads(resp.read())
        conn.close()
        return reply

    def text(self, reply):
        return reply["result"]["content"][0]["text"]

    def test_refused_open_is_answered_by_the_proxy(self):
        reply = self.call(bridge_server.OPEN_TOOL_NAME, "/x/busy.bndb")
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("already open: /x/busy.bndb", self.text(reply))
        self.assertEqual(self.upstream.requests, [])

    def test_allowed_open_is_forwarded(self):
        reply = self.call(bridge_server.OPEN_TOOL_NAME, "/x/free.bndb")
        self.assertFalse(reply["result"].get("isError", False))
        self.assertEqual(self.text(reply), "upstream ok")
        self.assertEqual(len(self.upstream.requests), 1)

    def test_other_tools_with_the_same_path_are_forwarded(self):
        self.call("bn_function_decompile", "/x/busy.bndb")
        self.assertEqual(len(self.upstream.requests), 1)

    def test_batch_is_refused_as_a_whole(self):
        batch = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": bridge_server.OPEN_TOOL_NAME,
                             "arguments": {"path": "/x/busy.bndb"}}},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "bn_function_decompile",
                             "arguments": {"path": "/x/free.bndb"}}}]
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy.port, timeout=10)
        conn.request("POST", "/mcp", json.dumps(batch),
                     {"Content-Type": "application/json"})
        resp = conn.getresponse()
        replies = json.loads(resp.read())
        conn.close()
        self.assertEqual([r["id"] for r in replies], [1, 2])
        self.assertTrue(all(r["result"]["isError"] for r in replies))
        self.assertEqual(self.upstream.requests, [])


if __name__ == "__main__":
    unittest.main()
