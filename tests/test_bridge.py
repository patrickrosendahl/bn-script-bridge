"""Host tests for the script bridge: bridge_server.py and the bnrun client, without Binary Ninja."""

import http.client
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
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


class BridgeTest(unittest.TestCase):
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

    def bnrun(self, *args, stdin=""):
        env = dict(os.environ, BN_SCRIPT_BRIDGE_CONFIG=self.config)
        return subprocess.run([sys.executable, BNRUN, *args], input=stdin, capture_output=True,
                              text=True, env=env, timeout=30)

    def test_ping(self):
        self.assertEqual(self.request("GET", "/ping"), (200, {"ok": True}))

    def test_runs_script_with_stdout_and_result(self):
        status, reply = self.request("POST", "/run", {
            "code": "print('hello', bv.file.filename)\nresult = {'n': len(bvs)}"})
        self.assertEqual(status, 200)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["stdout"], "hello /x/DM365_AllegroIndoor_debug.bndb\n")
        self.assertEqual(reply["result"], {"n": 2})

    def test_non_json_result_is_repr(self):
        _, reply = self.request("POST", "/run", {"code": "result = object"})
        self.assertEqual(reply["result"], "<class 'object'>")

    def test_view_selection(self):
        _, reply = self.request("POST", "/run", {"code": "result = bv.file.filename",
                                                 "view": "cx20707"})
        self.assertEqual(reply["result"], "/x/cx20707.ko.bndb")
        _, reply = self.request("POST", "/run", {"code": "pass", "view": "bndb"})
        self.assertFalse(reply["ok"])
        self.assertIn("matches 2 open views", reply["error"])

    def test_script_exception_is_reported(self):
        _, reply = self.request("POST", "/run", {"code": "print('before')\n1/0"})
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["stdout"], "before\n")
        self.assertIn("ZeroDivisionError", reply["error"])

    def test_main_thread_runner_used_on_request(self):
        self.request("POST", "/run", {"code": "pass"})
        self.assertEqual(self.main_calls, [])
        _, reply = self.request("POST", "/run", {"code": "result = 1", "main_thread": True})
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
        r = self.bnrun("--view", "cx20707", stdin="print(bv.file.filename)")
        self.assertEqual((r.returncode, r.stdout), (0, "/x/cx20707.ko.bndb\n"))
        r = self.bnrun("-e", "raise ValueError('boom')")
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
        self.assertEqual((r.returncode, r.stdout), (0, "free\n"))
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
