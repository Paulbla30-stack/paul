"""Tests for the web UI listener: chat, uploads, page, TLS."""

import json
import logging
import os
import ssl
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from jarvis.agent.core import AgentCore
from jarvis.cloud.headless import HeadlessRunner, UI_HTML_PATH, UI_OPEN_PATHS
from tests.test_brain import FakeClaude, make_brain, message, needs_sdk

NO_HW = {"display": None, "input": None, "memory": None, "storage": None}
LOG = logging.getLogger("test")


def make_agent(brain=None):
    agent = AgentCore({"name": "ui-test", "profile": "cloud"}, dict(NO_HW), LOG, brain=brain)
    agent.planner._boot_tasks_generated = True
    return agent


def call(base, path, data=None, headers=None, method=None, timeout=5, ctx=None):
    req = urllib.request.Request(base + path, data=data, headers=headers or {},
                                 method=method or ("POST" if data is not None else "GET"))
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        body = r.read()
        ctype = r.headers.get("Content-Type", "")
        return (json.loads(body) if ctype.startswith("application/json") else body.decode(), r.status)


class TestUiListener(unittest.TestCase):

    def _runner(self, agent, tmp, tls=False):
        runner = HeadlessRunner(agent, LOG, interval=0, status_port=None, token="t0k",
                                token_file=None,
                                ui={"enabled": True, "host": "127.0.0.1", "port": 0, "tls": tls,
                                    "tls_dir": os.path.join(tmp, "tls"),
                                    "upload_dir": os.path.join(tmp, "up"), "max_upload_mb": 0.001})
        port = runner.start_ui_server()
        self.assertIsNotNone(port)
        return runner, port

    def test_page_is_served_without_token_and_api_needs_it(self):
        self.assertTrue(os.path.exists(UI_HTML_PATH))
        with tempfile.TemporaryDirectory() as tmp:
            runner, port = self._runner(make_agent(), tmp)
            base = f"http://127.0.0.1:{port}"
            try:
                html, status = call(base, "/ui")
                self.assertEqual(status, 200)
                self.assertIn("<title>Jarvis</title>", html)
                self.assertIn("/upload", html)
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    call(base, "/uploads")
                self.assertEqual(cm.exception.code, 401)
                lst, _ = call(base, "/uploads", headers={"Authorization": "Bearer t0k"})
                self.assertEqual(lst, [])
            finally:
                runner.stop_status_server()

    def test_status_needs_a_token_on_the_ui_listener(self):
        """The UI port is reachable from off the box; /status is not.

        The loopback status API serves /status without a token because only
        the box itself can reach it. The same handler ran the UI listener, so
        anyone who could reach the UI port could read the model ARN, the open
        goals and the agent's last reasoning without logging in.
        """
        with tempfile.TemporaryDirectory() as tmp:
            runner, port = self._runner(make_agent(), tmp)
            base = f"http://127.0.0.1:{port}"
            try:
                for path in ("/status", "/", "/goals", "/brain", "/memory"):
                    with self.assertRaises(urllib.error.HTTPError, msg=path) as cm:
                        call(base, path)
                    self.assertEqual(cm.exception.code, 401, path)
                snapshot, status = call(base, "/status",
                                        headers={"Authorization": "Bearer t0k"})
                self.assertEqual(status, 200)
                self.assertEqual(snapshot["name"], "ui-test")
                # Liveness stays open, and says nothing but that it is alive.
                health, status = call(base, "/health")
                self.assertEqual((status, health), (200, {"ok": True}))
            finally:
                runner.stop_status_server()

    def test_loopback_status_api_still_answers_the_box_itself(self):
        """Nothing off the box can reach 127.0.0.1:8471, so it stays open."""
        with tempfile.TemporaryDirectory() as tmp:
            agent = make_agent()
            runner = HeadlessRunner(agent, LOG, interval=0, status_port=0, token="t0k",
                                    token_file=None, ui={"enabled": False})
            port = runner.start_status_server()
            base = f"http://127.0.0.1:{port}"
            try:
                snapshot, status = call(base, "/status")
                self.assertEqual(status, 200)
                self.assertEqual(snapshot["name"], "ui-test")
                health, _ = call(base, "/health")
                self.assertEqual(health["agent"], "ui-test")
            finally:
                runner.stop_status_server()

    def test_lockout_follows_the_real_client_through_the_tunnel(self):
        """The tunnel makes every request look like 127.0.0.1.

        Counting failures against the socket peer would put the whole
        internet in one bucket: a stranger guessing tokens would lock the
        operator out of his own agent, and the operator's own fat-fingered
        retry would spend the stranger's budget.
        """
        with tempfile.TemporaryDirectory() as tmp:
            runner, port = self._runner(make_agent(), tmp)
            base = f"http://127.0.0.1:{port}"
            stranger = {"CF-Connecting-IP": "203.0.113.9"}
            try:
                for _ in range(runner.auth_failure_limit):
                    with self.assertRaises(urllib.error.HTTPError):
                        call(base, "/goals", headers={**stranger,
                                                      "Authorization": "Bearer wrong"})
                self.assertTrue(runner.auth_locked("203.0.113.9"))
                # the operator, arriving from a different address, is untouched
                self.assertFalse(runner.auth_locked("198.51.100.4"))
                goals, status = call(base, "/goals",
                                     headers={"CF-Connecting-IP": "198.51.100.4",
                                              "Authorization": "Bearer t0k"})
                self.assertEqual(status, 200)
            finally:
                runner.stop_status_server()

    def test_the_header_is_only_trusted_from_loopback(self):
        """A request straight at the open port could otherwise set the header
        itself and have every guess charged to someone else."""
        with tempfile.TemporaryDirectory() as tmp:
            runner, _ = self._runner(make_agent(), tmp)
            try:
                handler = runner._make_handler(UI_OPEN_PATHS)
                probe = handler.__new__(handler)
                probe.headers = {"CF-Connecting-IP": "203.0.113.9"}
                probe.client_address = ("127.0.0.1", 1234)
                self.assertEqual(probe._client(), "203.0.113.9")
                probe.client_address = ("198.51.100.77", 1234)
                self.assertEqual(probe._client(), "198.51.100.77")
                # and a missing header falls back to the peer
                probe.headers = {}
                probe.client_address = ("127.0.0.1", 1234)
                self.assertEqual(probe._client(), "127.0.0.1")
            finally:
                runner.stop_status_server()

    def test_upload_saves_file_and_tells_the_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = make_agent()
            runner, port = self._runner(agent, tmp)
            base = f"http://127.0.0.1:{port}"
            auth = {"Authorization": "Bearer t0k"}
            try:
                entry, _ = call(base, "/upload", data=b"hello,world\n",
                                headers={**auth, "X-Filename": "../etc/../my report (1).csv"})
                self.assertEqual(entry["name"], "my_report_1_.csv")
                self.assertTrue(entry["path"].startswith(os.path.join(tmp, "up")))
                with open(entry["path"]) as f:
                    self.assertEqual(f.read(), "hello,world\n")
                self.assertEqual(entry["size"], 12)
                # second upload with the same name gets a suffix, not overwritten
                again, _ = call(base, "/upload", data=b"x", headers={**auth, "X-Filename": "my_report_1_.csv"})
                self.assertTrue(again["path"].endswith("my_report_1_-1.csv"))
                lst, _ = call(base, "/uploads", headers=auth)
                self.assertEqual([u["name"] for u in lst], ["my_report_1_.csv", "my_report_1_.csv"])
                self.assertTrue(all(u["exists"] for u in lst))
                self.assertEqual(len(agent.uploads), 2)
                self.assertIn("Operator uploaded my_report_1_.csv", agent.notes[0])
                self.assertEqual(len(agent.memory.recall("upload")), 2)
                # limits and validation
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    call(base, "/upload", data=b"y" * 2000, headers={**auth, "X-Filename": "big.bin"})
                self.assertEqual(cm.exception.code, 413)
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    call(base, "/upload", data=b"y", headers=auth)
                self.assertEqual(cm.exception.code, 400)
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    call(base, "/upload", data=b"y", headers={"X-Filename": "a.txt"})
                self.assertEqual(cm.exception.code, 401)
            finally:
                runner.stop_status_server()

    @needs_sdk
    def test_chat_is_multi_turn_and_sees_uploads(self):
        with FakeClaude() as api, tempfile.TemporaryDirectory() as tmp:
            brain = make_brain(api.url)
            agent = make_agent(brain)
            runner, port = self._runner(agent, tmp)
            base = f"http://127.0.0.1:{port}"
            auth = {"Authorization": "Bearer t0k", "Content-Type": "application/json"}
            try:
                call(base, "/upload", data=b"a,b\n1,2\n", headers={"Authorization": "Bearer t0k", "X-Filename": "data.csv"})
                api.respond_with(lambda r: message("It has two columns."))
                turns = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
                         {"role": "user", "content": "what is in data.csv?"}]
                ans, _ = call(base, "/chat", data=json.dumps({"messages": turns}).encode(), headers=auth)
                self.assertEqual(ans["answer"], "It has two columns.")
                body = api.requests[-1]["body"]
                self.assertEqual([m["role"] for m in body["messages"]], ["user", "assistant", "user"])
                self.assertIn("uploaded_files", body["messages"][0]["content"])
                self.assertIn("data.csv", body["messages"][0]["content"])
                self.assertTrue(body["messages"][0]["content"].startswith("Context as JSON"))
                self.assertEqual(body["messages"][-1]["content"], "what is in data.csv?")
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    call(base, "/chat", data=b"{}", headers=auth)
                self.assertEqual(cm.exception.code, 400)
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    call(base, "/chat", data=b"not json", headers=auth)
                self.assertEqual(cm.exception.code, 400)
            finally:
                runner.stop_status_server()

    def test_turn_normalisation(self):
        from jarvis.brain.llm import BaseBrain
        norm = BaseBrain._normalise_turns
        self.assertEqual(norm([]), [])
        self.assertEqual(norm([{"role": "assistant", "content": "x"}]), [])  # must start with user
        out = norm([{"role": "user", "content": "a"}, {"role": "user", "content": "b"},
                    {"role": "assistant", "content": ""}, {"role": "assistant", "content": "c"},
                    {"role": "bot", "content": "d"}, "junk"])
        self.assertEqual(out, [{"role": "user", "content": "a\n\nb"}, {"role": "assistant", "content": "c"},
                               {"role": "user", "content": "d"}])
        long = [{"role": "user" if i % 2 == 0 else "assistant", "content": str(i)} for i in range(30)]
        kept = norm(long, limit=5)
        self.assertEqual(len(kept), 4)  # window of 5 starts on an assistant turn, which is dropped
        self.assertEqual(kept[0]["role"], "user")
        self.assertEqual(kept[-1]["content"], "29")

    def test_tls_listener_with_generated_certificate(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, port = self._runner(make_agent(), tmp, tls=True)
            try:
                self.assertTrue(os.path.exists(os.path.join(tmp, "tls", "ui.crt")))
                self.assertEqual(oct(os.stat(os.path.join(tmp, "tls", "ui.key")).st_mode & 0o777), "0o600")
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                html, status = call(f"https://127.0.0.1:{port}", "/ui", ctx=ctx)
                self.assertEqual(status, 200)
                self.assertIn("Jarvis", html)
                # plain HTTP to the TLS port must fail, not be served
                with self.assertRaises(Exception):
                    call(f"http://127.0.0.1:{port}", "/ui", timeout=3)
            finally:
                runner.stop_status_server()
            # second start reuses the files
            runner2, port2 = self._runner(make_agent(), tmp, tls=True)
            runner2.stop_status_server()

    def test_failed_logins_are_throttled_per_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, port = self._runner(make_agent(), tmp)
            runner.auth_failure_limit = 3
            base = f"http://127.0.0.1:{port}"
            try:
                codes = []
                for _ in range(5):
                    try:
                        call(base, "/uploads", headers={"Authorization": "Bearer wrong"})
                    except urllib.error.HTTPError as e:
                        codes.append(e.code)
                self.assertEqual(codes, [401, 401, 429, 429, 429])  # locked from the Nth failure
                # even the right token is refused while locked out
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    call(base, "/uploads", headers={"Authorization": "Bearer t0k"})
                self.assertEqual(cm.exception.code, 429)
                runner._auth_failures.clear()
                lst, _ = call(base, "/uploads", headers={"Authorization": "Bearer t0k"})
                self.assertEqual(lst, [])
            finally:
                runner.stop_status_server()

    def test_ui_disabled_by_default(self):
        runner = HeadlessRunner(make_agent(), LOG, interval=0, status_port=None, token="t", token_file=None)
        self.assertIsNone(runner.start_ui_server())


if __name__ == "__main__":
    unittest.main()
