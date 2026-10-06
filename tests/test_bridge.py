"""Host tests for the script bridge: bridge_server.py and the bnrun client, without Binary Ninja."""

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
        self.assertEqual(self.request("GET", "/ping"), (200, {"ok": True}))

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
                                                          "max_parallel": 4})
        status, reply = self.request("POST", "/run", {"code": "result = sum(range(100000))"})
        self.assertEqual((status, reply["ok"], reply["result"]), (200, True, 4999950000))

    def test_cancel_busy_loop(self):
        t, box = self.run_async({"code": BUSY, "session": "sess-fe", "name": "fe"})
        running = self.wait_running()
        self.assertEqual((running["label"], running["session"], running["name"]),
                         ("print('started')", "sess-fe", "fe"))
        self.assertIn("running script \"print('started')\" for 0s (fe)",
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
        self.assertEqual(r.stdout.count(", parallel)"), 2, r.stdout)
        self.assertIn("queued: 0 serialized, 1 parallel", r.stdout)
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
        status, reply = self.request("POST", "/run", {"code": "pass", "parallel": True,
                                                      "session": "sess-audio"})
        self.assertEqual(status, 423)
        r = self.bnrun("--parallel", "--session", "sess-audio", "-e", "pass")
        self.assertEqual(r.returncode, 3)
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

    def __init__(self):
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
                    result = {"tools": [{"name": "bn_function_list", "inputSchema": {}}]}
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


if __name__ == "__main__":
    unittest.main()
