"""The agent producing a document, end to end.

compose.py is tested on its own in test_compose.py; this is about the wiring
around it — that the planner can reach it, that the operator can, that the
destination is never the caller's to choose, and that what came out is on the
chain by name and hash so "this is the file, and this was its hash on the day
it was made" is answerable later.
"""

import json
import logging
import os
import tempfile
import unittest
import urllib.error
import urllib.request

from jarvis.agent import compose
from jarvis.agent.core import AgentCore
from jarvis.agent.planner import Task, TaskType
from jarvis.cloud.headless import DOCUMENT_TYPES, HeadlessRunner

NO_HW = {"display": None, "input": None, "memory": None, "storage": None}
LOG = logging.getLogger("test")

BODY = """# Letter

Dear Sir or Madam,

I am writing about the matter we discussed.

- the first point
- the second point

Yours faithfully,
"""


class Base(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "documents")
        self.recorded = []
        self.agent = AgentCore({"name": "doc-test", "profile": "cloud",
                                "documents": {"dir": self.dir}},
                               dict(NO_HW), LOG)
        self.agent.planner._boot_tasks_generated = True
        self.agent.ledger.record = (
            lambda kind, body: self.recorded.append((kind, body)) or True)

    def tearDown(self):
        self.tmp.cleanup()

    def entries(self, action):
        return [b for k, b in self.recorded
                if k == "action" and b.get("action") == action]


class TestTheDestinationIsNotTheCallersToChoose(Base):

    def test_config_sets_the_directory_and_the_executor_gets_it(self):
        self.assertEqual(self.agent.document_dir, self.dir)
        self.assertEqual(self.agent.executor.document_dir, self.dir)

    def test_the_default_is_used_when_config_says_nothing(self):
        plain = AgentCore({"name": "d", "profile": "cloud"}, dict(NO_HW), LOG)
        self.assertEqual(plain.document_dir, compose.DEFAULT_DIR)

    def test_a_task_cannot_name_a_directory(self):
        # There is no "dir" argument, and adding one to the metadata must not
        # be enough: a caller that can choose the directory can choose any.
        task = Task(priority=5, description="a letter",
                    task_type=TaskType.COMPOSE_DOCUMENT,
                    metadata={"source": "llm", "content": BODY, "format": "txt",
                              "dir": "/etc", "directory": "/etc",
                              "path": "/etc/passwd"})
        result = self.agent.executor.execute(task)
        self.assertTrue(result["success"], result.get("error"))
        self.assertEqual(os.path.dirname(result["output"]["path"]),
                         os.path.realpath(self.dir))

    def test_a_name_cannot_climb_out(self):
        for attempt in ("../../etc/cron.d/x", "/etc/passwd", "../escape"):
            task = Task(priority=5, description="a letter",
                        task_type=TaskType.COMPOSE_DOCUMENT,
                        metadata={"source": "llm", "content": BODY,
                                  "format": "txt", "name": attempt})
            result = self.agent.executor.execute(task)
            self.assertEqual(os.path.dirname(result["output"]["path"]),
                             os.path.realpath(self.dir), attempt)


class TestThePlannerPath(Base):

    def compose_task(self, **meta):
        base = {"source": "llm", "content": BODY, "title": "A letter"}
        base.update(meta)
        return Task(priority=5, description="write a letter",
                    task_type=TaskType.COMPOSE_DOCUMENT, metadata=base)

    def test_every_format_can_be_asked_for(self):
        for fmt in compose.FORMATS:
            result = self.agent.act(self.compose_task(format=fmt, name=f"f-{fmt}"))
            self.assertTrue(result["success"], f"{fmt}: {result.get('error')}")
            self.assertTrue(os.path.isfile(result["output"]["path"]), fmt)

    def test_the_refusal_says_what_to_do_differently(self):
        # "failed" tells the model nothing it can act on; a named format that
        # does not exist tells it to pick another.
        result = self.agent.act(self.compose_task(format="html"))
        self.assertFalse(result["success"])
        self.assertIn("not a format", result["error"])
        self.assertIn("docx", result["error"])

    def test_nothing_to_write_is_refused_not_written_empty(self):
        result = self.agent.act(self.compose_task(content="", title=""))
        self.assertFalse(result["success"])
        self.assertFalse(os.path.isdir(self.dir) and os.listdir(self.dir))

    def test_the_document_is_on_the_chain_by_name_and_hash(self):
        result = self.agent.act(self.compose_task(format="pdf", name="letter"))
        written = self.entries("compose_document")
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0]["name"], "letter.pdf")
        self.assertEqual(written[0]["sha256"], result["output"]["sha256"])
        self.assertEqual(written[0]["bytes"], result["output"]["bytes"])

    def test_the_chain_carries_no_contents(self):
        # The file is on disk; the chain is copied to a bucket nobody can
        # delete from for thirty days, and a letter does not belong in it.
        self.agent.act(self.compose_task(format="txt", name="private"))
        blob = json.dumps(self.entries("compose_document"))
        self.assertNotIn("Dear Sir or Madam", blob)
        self.assertNotIn("the first point", blob)

    def test_a_refused_document_is_not_recorded_as_written(self):
        self.agent.act(self.compose_task(format="html"))
        self.assertEqual(self.entries("compose_document"), [])


class TestTheOperatorPath(Base):

    def test_it_writes_and_records(self):
        written = self.agent.compose_document(BODY, title="A letter", fmt="docx")
        self.assertTrue(os.path.isfile(written["path"]))
        entry = self.entries("compose_document")[0]
        self.assertEqual(entry["actor"], "operator")
        self.assertEqual(entry["sha256"], written["sha256"])

    def test_a_bad_format_raises_rather_than_writing_something_else(self):
        with self.assertRaises(compose.ComposeError):
            self.agent.compose_document(BODY, title="A letter", fmt="exe")


class TestTheMandateDoesNotBlockIt(Base):
    """Writing a document is not changing this machine.

    If it were classified as a change, the default rung could only ever
    *propose* a document, which is the capability not existing. The fence is
    that the destination is fixed in code, not that the rung is high.
    """

    def test_it_runs_at_the_default_rung(self):
        self.assertEqual(self.agent.rung, "proposer")
        result = self.agent.act(Task(
            priority=5, description="write a letter",
            task_type=TaskType.COMPOSE_DOCUMENT,
            metadata={"source": "llm", "content": BODY, "format": "txt"}))
        self.assertTrue(result["success"], result.get("error"))

    def test_it_runs_at_observer_too(self):
        from jarvis.agent import authority
        self.agent.executor.rung = authority.OBSERVER
        result = self.agent.executor.execute(Task(
            priority=5, description="write a letter",
            task_type=TaskType.COMPOSE_DOCUMENT,
            metadata={"source": "llm", "content": BODY, "format": "txt"}))
        self.assertTrue(result["success"], result.get("error"))

    def test_the_tool_is_declared_as_reporting_not_as_a_change(self):
        from jarvis.agent import tools
        tool = tools.get("compose_document")
        self.assertIsNotNone(tool)
        self.assertTrue(tool.reporting)
        self.assertEqual(tool.effect, tools.READ)
        self.assertIn("compose_document", tools.reporting_names())


def call(base, path, data=None, headers=None, method=None):
    req = urllib.request.Request(base + path, data=data, headers=headers or {},
                                 method=method or ("POST" if data is not None else "GET"))
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.read(), r.status, dict(r.headers)


class TestReachingThemOverHttp(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "documents")
        agent = AgentCore({"name": "doc-http", "profile": "cloud",
                           "documents": {"dir": self.dir}}, dict(NO_HW), LOG)
        agent.planner._boot_tasks_generated = True
        self.agent = agent
        # A real free port, found and released: the UI config coerces 0 to
        # its default, and two tests in one process would then fight over it.
        import socket
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free = probe.getsockname()[1]
        self.runner = HeadlessRunner(
            agent, LOG, interval=0, status_port=None, token="t0k", token_file=None,
            ui={"enabled": True, "host": "127.0.0.1", "port": free, "tls": False,
                "tls_dir": os.path.join(self.tmp.name, "tls"),
                "upload_dir": os.path.join(self.tmp.name, "up")})
        port = self.runner.start_ui_server()
        self.assertIsNotNone(port, "the UI listener did not start")
        self.base = f"http://127.0.0.1:{port}"
        self.auth = {"Authorization": "Bearer t0k",
                     "Content-Type": "application/json"}

    def tearDown(self):
        self.runner.stop_status_server()
        self.tmp.cleanup()

    def post(self, path, payload):
        return call(self.base, path, json.dumps(payload).encode(), self.auth)

    def test_compose_then_list_then_download(self):
        body, status, _ = self.post("/compose", {"content": BODY,
                                                 "title": "A letter",
                                                 "format": "pdf"})
        self.assertEqual(status, 200)
        written = json.loads(body)
        self.assertEqual(written["url"], f"/documents/{written['name']}")

        listing, status, _ = call(self.base, "/documents", headers=self.auth)
        self.assertEqual(status, 200)
        names = [row["name"] for row in json.loads(listing)["documents"]]
        self.assertIn(written["name"], names)

        raw, status, headers = call(self.base, written["url"], headers=self.auth)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], DOCUMENT_TYPES["pdf"])
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertTrue(raw.startswith(b"%PDF"))
        import hashlib
        self.assertEqual(hashlib.sha256(raw).hexdigest(), written["sha256"])

    def test_a_download_cannot_traverse_out_of_the_directory(self):
        secret = os.path.join(self.tmp.name, "secret.txt")
        with open(secret, "w") as fh:
            fh.write("not yours")
        for attempt in ("/documents/../secret.txt",
                        "/documents/..%2Fsecret.txt",
                        "/documents/%2e%2e%2fsecret.txt",
                        "/documents/....//secret.txt"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                call(self.base, attempt, headers=self.auth)
            self.assertIn(caught.exception.code, (400, 404), attempt)

    def test_only_the_formats_it_writes_can_be_downloaded(self):
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, "script.sh"), "w") as fh:
            fh.write("#!/bin/sh\necho no\n")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            call(self.base, "/documents/script.sh", headers=self.auth)
        self.assertEqual(caught.exception.code, 404)

    def test_the_endpoints_need_the_token(self):
        for path in ("/documents", "/documents/anything.pdf"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                call(self.base, path)
            self.assertEqual(caught.exception.code, 401, path)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            call(self.base, "/compose", b"{}", {"Content-Type": "application/json"})
        self.assertEqual(caught.exception.code, 401)

    def test_a_format_it_cannot_write_is_a_reason_not_a_crash(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/compose", {"content": BODY, "format": "html"})
        self.assertEqual(caught.exception.code, 400)
        self.assertIn("not a format", caught.exception.read().decode())


class TestTheUiCanReachThem(unittest.TestCase):
    """A capability nobody can get at is the half-built shape the tool
    register exists to stop."""

    def setUp(self):
        from jarvis.cloud.headless import UI_HTML_PATH
        with open(UI_HTML_PATH, encoding="utf-8") as fh:
            self.page = fh.read()

    def test_the_page_lists_and_downloads_them(self):
        self.assertIn('id="documents"', self.page)
        self.assertIn('/documents/', self.page)
        self.assertIn('loadDocuments', self.page)

    def test_the_page_can_ask_for_one(self):
        self.assertIn('"/compose"', self.page)
        for control in ('id="docBody"', 'id="docTitle"', 'id="docFormat"',
                        'id="docWrite"'):
            self.assertIn(control, self.page)

    def test_every_format_the_code_writes_is_offered(self):
        for fmt in compose.FORMATS:
            self.assertIn(f'value="{fmt}"', self.page, fmt)

    def test_the_download_is_a_link_not_a_render(self):
        # An agent-written document rendered in the page's own origin would
        # put its text inside the session that can drive the agent.
        self.assertIn('setAttribute("download"', self.page)


class TestTheImageMakesTheDirectory(unittest.TestCase):

    def test_provision_creates_it_and_config_names_it(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "aws", "scripts", "provision.sh")) as fh:
            provision = fh.read()
        self.assertIn("/var/lib/jarvis/documents", provision)
        import yaml
        with open(os.path.join(root, "rootfs", "etc", "jarvis",
                               "config-aws.yaml")) as fh:
            config = yaml.safe_load(fh)
        self.assertEqual(config["documents"]["dir"], "/var/lib/jarvis/documents")


if __name__ == "__main__":
    unittest.main()
