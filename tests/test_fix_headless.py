"""Regression tests for the control API and web UI fixes of 27 Sep 2026.

Bad request bodies answered rather than dropped, a TLS port that one idle
connection cannot stall, health and status that answer while the agent is
busy, a bounded login throttle, proposal decisions that land on the proposal
the operator was looking at, and a cookie session the page does not throw
back to the login screen.
"""

import http.client
import json
import logging
import os
import re
import socket
import ssl
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from vigil.agent.core import AgentCore
from vigil.cloud.headless import HeadlessRunner, UI_HTML_PATH

NO_HW = {"display": None, "input": None, "memory": None, "storage": None}
LOG = logging.getLogger("test")
AUTH = {"Authorization": "Bearer t0k", "Content-Type": "application/json"}


def make_agent():
    agent = AgentCore({"name": "fix-test", "profile": "cloud"}, dict(NO_HW), LOG)
    agent.planner._boot_tasks_generated = True
    return agent


def call(base, path, data=None, headers=None, method=None, timeout=5, ctx=None):
    req = urllib.request.Request(base + path, data=data, headers=headers or {},
                                 method=method or ("POST" if data is not None else "GET"))
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.loads(r.read()), r.status


def raw_post(port, path, headers, body=b"", timeout=5):
    """A request urllib would refuse to build: a bad Content-Length."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.putrequest("POST", path, skip_accept_encoding=True)
        for name, value in headers.items():
            conn.putheader(name, value)
        conn.endheaders(body)
        resp = conn.getresponse()
        text = resp.read()
        return resp.status, (json.loads(text) if text else None)
    finally:
        conn.close()


class _Base(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.runners = []

    def tearDown(self):
        for runner in self.runners:
            runner.stop_status_server()
        self._tmp.cleanup()

    def ui_runner(self, agent=None, tls=False):
        runner = HeadlessRunner(
            agent or make_agent(), LOG, interval=0, status_port=None, token="t0k",
            token_file=None, token_persist_file=None,
            session_key_file=os.path.join(self.tmp, "session.key"),
            ui={"enabled": True, "host": "127.0.0.1", "port": 0, "tls": tls,
                "tls_dir": os.path.join(self.tmp, "tls"),
                "upload_dir": os.path.join(self.tmp, "up"), "max_upload_mb": 0.001})
        port = runner.start_ui_server()
        self.assertIsNotNone(port)
        self.runners.append(runner)
        return runner, port

    def loopback_runner(self, agent=None):
        runner = HeadlessRunner(
            agent or make_agent(), LOG, interval=0, status_port=0, token="t0k",
            token_file=None, token_persist_file=None,
            session_key_file=os.path.join(self.tmp, "session.key"))
        port = runner.start_status_server()
        self.assertIsNotNone(port)
        self.runners.append(runner)
        return runner, port


class TestBadRequestsAreAnswered(_Base):
    """D1: a malformed request gets a status, not a dropped connection."""

    def test_non_numeric_content_length_is_400(self):
        _, port = self.ui_runner()
        # /login is before the auth check, so this needs no token
        status, body = raw_post(port, "/login", {"Content-Length": "abc"})
        self.assertEqual(status, 400)
        self.assertIn("Content-Length", body["error"])
        status, body = raw_post(port, "/goal", {"Authorization": "Bearer t0k",
                                                "Content-Length": "12x"})
        self.assertEqual(status, 400)
        status, _ = raw_post(port, "/upload", {"Authorization": "Bearer t0k",
                                               "X-Filename": "a.txt",
                                               "Content-Length": "nope"})
        self.assertEqual(status, 400)

    def test_negative_content_length_is_400(self):
        _, port = self.ui_runner()
        status, _ = raw_post(port, "/goal", {"Authorization": "Bearer t0k",
                                             "Content-Length": "-1"})
        self.assertEqual(status, 400)
        # a digit to isdigit() but not to int(); http.client sends latin-1
        status, _ = raw_post(port, "/goal", {"Authorization": "Bearer t0k",
                                             "Content-Length": "²"})
        self.assertEqual(status, 400)

    def test_decide_with_a_json_array_is_400(self):
        agent = make_agent()
        agent.proposals.append({"ts": 1, "text": "something"})
        _, port = self.ui_runner(agent)
        base = f"http://127.0.0.1:{port}"
        for body in (b"[1, 2]", b'"text"', b"7"):
            with self.assertRaises(urllib.error.HTTPError, msg=body) as cm:
                call(base, "/proposals/decide", headers=AUTH, data=body)
            self.assertEqual(cm.exception.code, 400, body)
        self.assertFalse(list(agent.proposals)[0].get("decision"))

    def test_browse_with_a_json_array_is_400(self):
        class Browser:
            opened = []

            def open(self, url):
                self.opened.append(url)
                return {"url": url}
        agent = make_agent()
        agent.browser = Browser()
        _, port = self.ui_runner(agent)
        base = f"http://127.0.0.1:{port}"
        for what in ("open", "follow", "act"):
            with self.assertRaises(urllib.error.HTTPError, msg=what) as cm:
                call(base, f"/browse/{what}", headers=AUTH, data=b'["https://x"]')
            self.assertEqual(cm.exception.code, 400, what)
        self.assertEqual(Browser.opened, [])

    def test_an_unexpected_failure_is_a_json_500_without_the_message(self):
        agent = make_agent()

        def boom(*_a, **_k):
            raise RuntimeError("secret-value-xyz")
        agent.withdraw_goal = boom
        agent.memory.get_summary = boom
        _, port = self.ui_runner(agent)
        base = f"http://127.0.0.1:{port}"
        with self.assertRaises(urllib.error.HTTPError) as cm:
            call(base, "/goal/withdraw", headers=AUTH, data=b"a goal")
        self.assertEqual(cm.exception.code, 500)
        text = cm.exception.read().decode()
        self.assertEqual(json.loads(text), {"error": "internal error"})
        self.assertNotIn("secret-value-xyz", text)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            call(base, "/memory", headers=AUTH)
        self.assertEqual(cm.exception.code, 500)
        self.assertNotIn("secret-value-xyz", cm.exception.read().decode())
        # and the server is still serving
        got, status = call(base, "/health")
        self.assertEqual((status, got), (200, {"ok": True}))


class TestTlsHandshakeIsPerConnection(_Base):
    """D2: an idle TCP connection must not hold the TLS port for everyone."""

    def test_idle_connection_does_not_block_a_tls_client(self):
        _, port = self.ui_runner(tls=True)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        idle = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            time.sleep(0.2)          # let the server accept it
            started = time.time()
            got, status = call(f"https://127.0.0.1:{port}", "/health", ctx=ctx, timeout=3)
            self.assertEqual((status, got), (200, {"ok": True}))
            self.assertLess(time.time() - started, 3)
            # plain HTTP to the TLS port is still refused, not served
            with self.assertRaises(Exception):
                call(f"http://127.0.0.1:{port}", "/health", timeout=3)
        finally:
            # Closing it also frees the old serve thread, so teardown's
            # shutdown() cannot hang on it.
            idle.close()


class TestHealthUnderLoad(_Base):
    """D3: a model call holding the agent lock does not make the box look dead."""

    def _hold(self, runner):
        held = threading.Event()
        release = threading.Event()

        def hold():
            with runner._lock:
                held.set()
                release.wait(20)
        t = threading.Thread(target=hold, daemon=True)
        t.start()
        self.assertTrue(held.wait(5))
        return release, t

    def test_health_and_status_answer_while_the_lock_is_held(self):
        runner, port = self.loopback_runner()
        base = f"http://127.0.0.1:{port}"
        fresh, _ = call(base, "/status")
        self.assertNotIn("stale", fresh)
        release, t = self._hold(runner)
        try:
            started = time.time()
            health, status = call(base, "/health", timeout=3)
            self.assertEqual(status, 200)
            self.assertTrue(health["ok"])
            snap, status = call(base, "/status", timeout=3)
            self.assertEqual(status, 200)
            self.assertTrue(snap["stale"])
            self.assertEqual(snap["name"], "fix-test")
            self.assertIsNotNone(snap["snapshot_at"])
            self.assertLess(time.time() - started, 3)
        finally:
            release.set()
            t.join(5)
        again, _ = call(base, "/status")
        self.assertNotIn("stale", again)

    def test_status_with_nothing_published_yet(self):
        runner, port = self.loopback_runner()
        release, t = self._hold(runner)
        try:
            snap, status = call(f"http://127.0.0.1:{port}", "/status", timeout=3)
            self.assertEqual(status, 200)
            self.assertTrue(snap["stale"])
            self.assertIsNone(snap["snapshot_at"])
            self.assertEqual(snap["name"], "fix-test")
        finally:
            release.set()
            t.join(5)


class TestAuthBookkeeping(_Base):
    """D4: the throttle table holds failures, not every visitor, and is bounded."""

    def test_a_request_without_failures_leaves_nothing_behind(self):
        runner, port = self.ui_runner()
        base = f"http://127.0.0.1:{port}"
        call(base, "/uploads", headers={"Authorization": "Bearer t0k"})
        with self.assertRaises(urllib.error.HTTPError):
            call(base, "/uploads")
        self.assertEqual(runner._auth_failures, {})
        self.assertFalse(runner.auth_locked("198.51.100.1"))
        self.assertEqual(runner._auth_failures, {})

    def test_expired_failures_are_removed(self):
        runner, _ = self.ui_runner()
        runner.auth_failed("203.0.113.5")
        self.assertIn("203.0.113.5", runner._auth_failures)
        runner._auth_failures["203.0.113.5"] = [time.time() - runner.auth_failure_window - 1]
        self.assertFalse(runner.auth_locked("203.0.113.5"))
        self.assertNotIn("203.0.113.5", runner._auth_failures)

    def test_the_table_is_capped_and_keeps_the_active_guesser(self):
        runner, _ = self.ui_runner()
        runner.auth_failure_max_clients = 50
        runner.auth_failure_limit = 3
        for _ in range(3):
            runner.auth_failed("203.0.113.9")
        for i in range(200):
            runner.auth_failed(f"10.0.{i // 256}.{i % 256}")
            runner.auth_failed("203.0.113.9")
        self.assertLessEqual(len(runner._auth_failures), 50)
        self.assertTrue(runner.auth_locked("203.0.113.9"))

    def test_concurrent_failures_are_all_counted(self):
        # auth_locked rebuilt the list and stored it back while another
        # thread appended to the old one, and that failure was lost.
        runner, _ = self.ui_runner()
        runner.auth_failure_limit = 10 ** 6
        previous = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            def fail():
                for _ in range(2000):
                    runner.auth_failed("203.0.113.7")
                    runner.auth_locked("203.0.113.7")
            threads = [threading.Thread(target=fail) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            sys.setswitchinterval(previous)
        self.assertEqual(len(runner._auth_failures["203.0.113.7"]), 16000)


class TestProposalDecisionsById(_Base):
    """D5: the verdict lands on the proposal the operator was shown."""

    def test_deciding_the_first_shown_of_25_by_id(self):
        agent = make_agent()
        for i in range(25):
            agent.proposals.append({"ts": 1000 + i, "cycle": i, "text": f"proposal {i}"})
        _, port = self.ui_runner(agent)
        base = f"http://127.0.0.1:{port}"
        listed, _ = call(base, "/proposals", headers=AUTH)
        shown = listed["proposals"]
        self.assertEqual(len(shown), 20)
        first = shown[0]
        self.assertEqual(first["text"], "proposal 5")
        self.assertTrue(first["id"])
        self.assertEqual(len({p["id"] for p in shown}), 20)
        decided, status = call(base, "/proposals/decide", headers=AUTH,
                               data=json.dumps({"id": first["id"], "accepted": False,
                                                "reason": "not this one"}).encode())
        self.assertEqual(status, 200)
        self.assertEqual(decided["text"], "proposal 5")
        self.assertEqual(decided["id"], first["id"])
        items = list(agent.proposals)
        self.assertEqual(items[5]["decision"], "declined")
        self.assertEqual([p["text"] for p in items if p.get("decision")], ["proposal 5"])
        # the listing still shows the same id for it
        listed, _ = call(base, "/proposals", headers=AUTH)
        self.assertEqual(listed["proposals"][0]["id"], first["id"])
        self.assertEqual(listed["proposals"][0]["decision"], "declined")

    def test_the_id_survives_the_bounded_list_moving(self):
        agent = make_agent()
        for i in range(agent.proposals.maxlen):
            agent.proposals.append({"ts": 2000 + i, "text": f"old {i}"})
        _, port = self.ui_runner(agent)
        base = f"http://127.0.0.1:{port}"
        target = call(base, "/proposals?limit=5", headers=AUTH)[0]["proposals"][0]
        # two more arrive between reading the list and answering it
        agent.proposals.append({"ts": 9000, "text": "new a"})
        agent.proposals.append({"ts": 9001, "text": "new b"})
        decided, _ = call(base, "/proposals/decide", headers=AUTH,
                          data=json.dumps({"id": target["id"], "accepted": True}).encode())
        self.assertEqual(decided["text"], target["text"])
        self.assertEqual(decided["decision"], "accepted")

    def test_unknown_id_is_404_and_index_still_works(self):
        agent = make_agent()
        agent.proposals.append({"ts": 1, "text": "a"})
        agent.proposals.append({"ts": 2, "text": "b"})
        _, port = self.ui_runner(agent)
        base = f"http://127.0.0.1:{port}"
        with self.assertRaises(urllib.error.HTTPError) as cm:
            call(base, "/proposals/decide", headers=AUTH,
                 data=json.dumps({"id": "p-nope", "accepted": True}).encode())
        self.assertEqual(cm.exception.code, 404)
        decided, _ = call(base, "/proposals/decide", headers=AUTH,
                          data=json.dumps({"index": 1, "accepted": True}).encode())
        self.assertEqual(decided["text"], "b")

    def test_filed_proposals_carry_an_id(self):
        agent = make_agent()

        class Decision:
            proposal = "tune the swap file"
            reasoning = "memory pressure"
        entry = agent._record_stated_proposal(Decision())
        self.assertTrue(entry["id"].startswith("p-"))
        decided = agent.decide_proposal(None, True, proposal_id=entry["id"])
        self.assertEqual(decided["description"], "tune the swap file")

    def test_the_page_sends_the_id(self):
        with open(UI_HTML_PATH, encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("decideProposal(p.id,", html)
        self.assertRegex(html, r"JSON\.stringify\(\{\s*id\s*,\s*accepted")


class TestUiPortZero(_Base):
    """Port 0 asks for any free port; it was read as unset and became 8443."""

    def test_port_zero_is_ephemeral(self):
        runner, port = self.ui_runner()
        self.assertEqual(runner.ui_port, 0)
        self.assertNotEqual(port, 8443)
        default = HeadlessRunner(make_agent(), LOG, interval=0, status_port=None,
                                 token="t", token_file=None, token_persist_file=None,
                                 session_key_file=os.path.join(self.tmp, "session.key"),
                                 ui={"enabled": True})
        self.assertEqual(default.ui_port, 8443)


class TestCookieSessionInThePage(unittest.TestCase):
    """D6: a cookie-only session is not sent back to the login screen."""

    @classmethod
    def setUpClass(cls):
        with open(UI_HTML_PATH, encoding="utf-8") as fh:
            cls.html = fh.read()

    def _function(self, head):
        start = self.html.index(head)
        return self.html[start:self.html.index("\n  }", start)]

    def test_refresh_accepts_a_cookie_session(self):
        body = self._function("async function refresh()")
        self.assertIn("if (!token && !sessionActive)", body)
        self.assertNotRegex(body, r"if\s*\(\s*!token\s*\)")

    def test_no_other_function_bounces_on_token_alone(self):
        # The only place "no token" alone may mean "log in" is boot(), after
        # the cookie probe has already failed.
        guards = [m.start() for m in re.finditer(r"if\s*\(\s*!token\s*\)", self.html)]
        boot = self.html.index("async function boot()")
        self.assertTrue(all(g > boot for g in guards), guards)

    def test_no_empty_bearer_without_a_token(self):
        body = self._function("function headers(extra)")
        self.assertRegex(body, r"token\s*\?\s*\{\s*\"Authorization\"")
        self.assertIn('if (token) xhr.setRequestHeader("Authorization"', self.html)


if __name__ == "__main__":
    unittest.main()
