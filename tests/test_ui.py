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

from openclaw.agent.core import AgentCore
from openclaw.cloud.headless import HeadlessRunner, UI_HTML_PATH
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
                self.assertIn("<title>OpenClaw</title>", html)
                self.assertIn("/upload", html)
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    call(base, "/uploads")
                self.assertEqual(cm.exception.code, 401)
                lst, _ = call(base, "/uploads", headers={"Authorization": "Bearer t0k"})
                self.assertEqual(lst, [])
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
        from openclaw.brain.llm import BaseBrain
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
                self.assertIn("OpenClaw", html)
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
                self.assertEqual(codes, [401, 401, 401, 429, 429])
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
