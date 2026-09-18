"""Tests for OpenClaw cloud support (IMDS, bootstrap, headless runner)."""

import json
import logging
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

from openclaw.agent.core import AgentCore
from openclaw.agent.planner import TaskPlanner, TaskType
from openclaw.agent.memory import AgentMemory
from openclaw.cloud.imds import IMDSClient, TOKEN_HEADER
from openclaw.cloud import bootstrap
from openclaw.cloud.headless import HeadlessRunner
from openclaw.main import load_config, apply_cli_overrides


# ---- Fake IMDSv2 ------------------------------------------------------------

USER_DATA = """#cloud-config
openclaw:
  agent:
    name: test-agent
  goals:
    - description: Keep disk under 80%
      priority: 2
    - Watch memory
  cloud:
    cycle_interval: 5
"""

IDENTITY = {
    "instanceId": "i-0123456789abcdef0",
    "instanceType": "t3.small",
    "region": "eu-west-2",
    "availabilityZone": "eu-west-2a",
    "imageId": "ami-0abc",
}

METADATA = {
    "instance-id": "i-0123456789abcdef0",
    "instance-type": "t3.small",
    "ami-id": "ami-0abc",
    "placement/availability-zone": "eu-west-2a",
    "placement/region": "eu-west-2",
    "hostname": "ip-10-0-0-1.eu-west-2.compute.internal",
    "local-ipv4": "10.0.0.1",
    "mac": "0a:00:00:00:00:01",
    "iam/security-credentials/": "openclaw-role\n",
    "tags/instance": "Name\nopenclaw:goal\n",
    "tags/instance/Name": "paul-agent",
    "tags/instance/openclaw:goal": "Stay healthy",
}


class FakeIMDSHandler(BaseHTTPRequestHandler):
    TOKEN = "fake-token-123"
    user_data = USER_DATA

    def log_message(self, *args):
        pass

    def _send(self, code, body=""):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_PUT(self):
        if self.path == "/latest/api/token":
            if "X-aws-ec2-metadata-token-ttl-seconds" not in self.headers:
                return self._send(400)
            return self._send(200, self.TOKEN)
        self._send(404)

    def do_GET(self):
        if self.headers.get(TOKEN_HEADER) != self.TOKEN:
            return self._send(401)
        if self.path == "/latest/dynamic/instance-identity/document":
            return self._send(200, json.dumps(IDENTITY))
        if self.path == "/latest/user-data":
            if self.user_data is None:
                return self._send(404)
            return self._send(200, self.user_data)
        prefix = "/latest/meta-data/"
        if self.path.startswith(prefix):
            key = self.path[len(prefix):]
            if key in METADATA:
                return self._send(200, METADATA[key])
        self._send(404)


class FakeIMDS:
    """Context manager running the fake IMDS on a loopback port."""

    def __enter__(self):
        self.server = HTTPServer(("127.0.0.1", 0), FakeIMDSHandler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


# ---- IMDS client -------------------------------------------------------------

class TestIMDSClient(unittest.TestCase):

    def test_summary_and_identity(self):
        with FakeIMDS() as fake:
            imds = IMDSClient(base_url=fake.url)
            self.assertTrue(imds.is_available())
            summary = imds.summary()
            self.assertEqual(summary["provider"], "aws")
            self.assertEqual(summary["instance_id"], "i-0123456789abcdef0")
            self.assertEqual(summary["region"], "eu-west-2")
            self.assertEqual(summary["iam_role"], "openclaw-role")
            self.assertNotIn("public_ipv4", summary)  # 404 -> omitted
            self.assertEqual(imds.identity()["instanceType"], "t3.small")

    def test_user_data_and_tags(self):
        with FakeIMDS() as fake:
            imds = IMDSClient(base_url=fake.url)
            self.assertIn("openclaw:", imds.user_data())
            tags = imds.tags()
            self.assertEqual(tags["Name"], "paul-agent")
            self.assertEqual(tags["openclaw:goal"], "Stay healthy")

    def test_unavailable_endpoint(self):
        # Nothing listens here; client must degrade quietly and quickly.
        imds = IMDSClient(base_url="http://127.0.0.1:9", timeout=0.2)
        self.assertFalse(imds.is_available())
        self.assertEqual(imds.summary(), {})
        self.assertIsNone(imds.user_data())
        self.assertEqual(imds.tags(), {})
        self.assertEqual(imds.identity(), {})

    def test_token_is_cached(self):
        with FakeIMDS() as fake:
            imds = IMDSClient(base_url=fake.url)
            imds.get("instance-id")
            token = imds._token
            imds.get("ami-id")
            self.assertEqual(imds._token, token)


# ---- Bootstrap -----------------------------------------------------------------

class TestBootstrap(unittest.TestCase):

    def test_parse_cloud_config_block(self):
        cfg = bootstrap.parse_user_data(USER_DATA)
        self.assertEqual(cfg["agent"]["name"], "test-agent")
        self.assertEqual(len(cfg["goals"]), 2)

    def test_parse_bare_openclaw_config(self):
        cfg = bootstrap.parse_user_data('{"agent": {"name": "x"}, "goals": ["a"]}')
        self.assertEqual(cfg["agent"]["name"], "x")
        self.assertEqual(cfg["goals"], ["a"])

    def test_parse_ignores_shell_scripts_and_junk(self):
        self.assertEqual(bootstrap.parse_user_data("#!/bin/bash\necho hi\n"), {})
        self.assertEqual(bootstrap.parse_user_data(""), {})
        self.assertEqual(bootstrap.parse_user_data(None), {})
        self.assertEqual(bootstrap.parse_user_data("- just\n- a list\n"), {})
        self.assertEqual(bootstrap.parse_user_data("packages:\n  - htop\n"), {})

    def test_normalise_goals(self):
        goals = bootstrap.normalise_goals([
            "plain text",
            {"description": "dict goal", "priority": "3"},
            {"description": "clamped", "priority": 99},
            {"no": "description"},
            "",
            42,
        ])
        self.assertEqual(goals, [
            {"description": "plain text", "priority": 5},
            {"description": "dict goal", "priority": 3},
            {"description": "clamped", "priority": 10},
        ])

    def test_build_cloud_config_from_fake_imds(self):
        with FakeIMDS() as fake:
            cfg = bootstrap.build_cloud_config(IMDSClient(base_url=fake.url))
        self.assertTrue(cfg["cloud"]["enabled"])
        self.assertEqual(cfg["cloud"]["provider"], "aws")
        self.assertEqual(cfg["cloud"]["instance"]["instance_id"],
                         "i-0123456789abcdef0")
        self.assertEqual(cfg["cloud"]["cycle_interval"], 5)  # user override
        self.assertEqual(cfg["cloud"]["tags"]["Name"], "paul-agent")
        self.assertEqual(cfg["agent"]["name"], "test-agent")
        self.assertEqual(cfg["agent"]["profile"], "cloud")
        descriptions = [g["description"] for g in cfg["goals"]]
        self.assertEqual(descriptions,
                         ["Keep disk under 80%", "Watch memory", "Stay healthy"])

    def test_build_cloud_config_without_imds(self):
        cfg = bootstrap.build_cloud_config(
            IMDSClient(base_url="http://127.0.0.1:9", timeout=0.2))
        self.assertFalse(cfg["cloud"]["enabled"])
        self.assertEqual(cfg["cloud"]["provider"], "none")
        self.assertEqual(cfg["goals"], [])
        self.assertNotIn("agent", cfg)

    def test_write_and_reload_overlay(self):
        with FakeIMDS() as fake, tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "etc", "cloud.yaml")
            rc = bootstrap.main(["--imds-url", fake.url, "--output", out])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(out))
            # The agent must be able to merge what bootstrap wrote.
            config = load_config("/nonexistent/config.yaml", [out])
            self.assertEqual(config["agent"]["name"], "test-agent")
            self.assertEqual(config["agent"]["profile"], "cloud")
            self.assertTrue(config["cloud"]["enabled"])
            self.assertEqual(config["cloud"]["cycle_interval"], 5)
            self.assertEqual(config["cloud"]["status_port"], 8471)  # default kept
            self.assertEqual(len(config["goals"]), 3)


# ---- Config loading --------------------------------------------------------------

class TestConfigOverlays(unittest.TestCase):

    def test_defaults_include_cloud_section(self):
        config = load_config("/nonexistent")
        self.assertFalse(config["cloud"]["headless"])
        self.assertEqual(config["agent"]["profile"], "bare-metal")
        self.assertEqual(config["goals"], [])

    def test_extra_overlays_merge_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = os.path.join(tmp, "a.yaml")
            b = os.path.join(tmp, "b.yaml")
            with open(a, "w") as f:
                f.write("agent:\n  name: A\ncloud:\n  cycle_interval: 1\n")
            with open(b, "w") as f:
                f.write("agent:\n  name: B\n")
            config = load_config(a, [b, os.path.join(tmp, "missing.yaml")])
        self.assertEqual(config["agent"]["name"], "B")
        self.assertEqual(config["cloud"]["cycle_interval"], 1)

    def test_headless_env_and_cli_overrides(self):
        os.environ["OPENCLAW_HEADLESS"] = "1"
        try:
            config = load_config("/nonexistent")
        finally:
            del os.environ["OPENCLAW_HEADLESS"]
        self.assertTrue(config["cloud"]["headless"])

        class Args:
            headless = False
            cycle_interval = 2.5
            max_cycles = 7
            status_port = 0
            status_file = ""
        config = apply_cli_overrides(load_config("/nonexistent"), Args())
        self.assertEqual(config["cloud"]["cycle_interval"], 2.5)
        self.assertEqual(config["cloud"]["max_cycles"], 7)
        self.assertIsNone(config["cloud"]["status_port"])
        self.assertIsNone(config["cloud"]["status_file"])


# ---- Planner cloud profile -----------------------------------------------------

class TestCloudProfile(unittest.TestCase):

    def test_cloud_boot_tasks_skip_display_and_input(self):
        planner = TaskPlanner(AgentMemory(), profile="cloud")
        first = planner.get_next_task()
        self.assertEqual(first.task_type, TaskType.CLOUD_PROBE)
        types = [first.task_type] + [t.task_type for t in planner.pending_tasks]
        self.assertIn(TaskType.SECURITY_SCAN, types)
        descriptions = [first.description] + [t.description for t in planner.pending_tasks]
        self.assertFalse(any("display" in d.lower() or "input" in d.lower()
                             for d in descriptions))

    def test_unknown_profile_falls_back_to_bare_metal(self):
        planner = TaskPlanner(AgentMemory(), profile="moon-base")
        self.assertEqual(planner.profile, "bare-metal")

    def test_cloud_probe_without_imds_still_succeeds(self):
        os.environ["OPENCLAW_IMDS_URL"] = "http://127.0.0.1:9"
        try:
            agent = AgentCore({"name": "t", "profile": "cloud"},
                              {"display": None, "input": None,
                               "memory": None, "storage": None},
                              logging.getLogger("test"))
            result = agent.run_cycle()
        finally:
            del os.environ["OPENCLAW_IMDS_URL"]
        self.assertEqual(result["action"], "Identify cloud instance (IMDS)")
        self.assertTrue(result["result"]["success"])
        self.assertFalse(result["result"]["output"]["available"])
        self.assertEqual(agent.get_status()["profile"], "cloud")

    def test_cloud_probe_with_fake_imds(self):
        with FakeIMDS() as fake:
            os.environ["OPENCLAW_IMDS_URL"] = fake.url
            try:
                agent = AgentCore({"name": "t", "profile": "cloud"},
                                  {"display": None, "input": None,
                                   "memory": None, "storage": None},
                                  logging.getLogger("test"))
                result = agent.run_cycle()
            finally:
                del os.environ["OPENCLAW_IMDS_URL"]
        self.assertEqual(result["result"]["output"]["instance_id"],
                         "i-0123456789abcdef0")
        self.assertEqual(len(agent.memory.recall("cloud_probe")), 1)


# ---- Headless runner -----------------------------------------------------------

class TestHeadlessRunner(unittest.TestCase):

    def _agent(self):
        return AgentCore({"name": "headless-test", "profile": "cloud"},
                         {"display": None, "input": None,
                          "memory": None, "storage": None},
                         logging.getLogger("test"))

    def test_runs_max_cycles_and_writes_status_file(self):
        os.environ["OPENCLAW_IMDS_URL"] = "http://127.0.0.1:9"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                status_file = os.path.join(tmp, "run", "status.json")
                runner = HeadlessRunner(self._agent(), logging.getLogger("test"),
                                        interval=0, max_cycles=3,
                                        status_port=None, status_file=status_file)
                cycles = runner.run()
                self.assertEqual(cycles, 3)
                with open(status_file) as f:
                    status = json.load(f)
        finally:
            del os.environ["OPENCLAW_IMDS_URL"]
        self.assertEqual(status["cycle_count"], 3)
        self.assertEqual(status["runner"]["mode"], "headless")
        self.assertFalse(status["running"])

    def test_status_endpoint(self):
        os.environ["OPENCLAW_IMDS_URL"] = "http://127.0.0.1:9"
        try:
            agent = self._agent()
            with tempfile.TemporaryDirectory() as tmp:
                token_file = os.path.join(tmp, "token")
                runner = HeadlessRunner(agent, logging.getLogger("test"),
                                        interval=0, max_cycles=0, status_port=0,
                                        token_file=token_file)
                port = runner.start_status_server()
                self.assertIsNotNone(port)
                with open(token_file) as f:
                    token = f.read().strip()
                self.assertEqual(token, runner.token)
                self.assertEqual(oct(os.stat(token_file).st_mode & 0o777), "0o600")
            auth = {"Authorization": f"Bearer {token}"}
            try:
                agent.run_cycle()
                base = f"http://127.0.0.1:{port}"
                with urllib.request.urlopen(base + "/health", timeout=2) as r:
                    health = json.loads(r.read())
                with urllib.request.urlopen(base + "/status", timeout=2) as r:
                    status = json.loads(r.read())
                with urllib.request.urlopen(
                        urllib.request.Request(base + "/history", headers=auth), timeout=2) as r:
                    history = json.loads(r.read())
                with urllib.request.urlopen(
                        urllib.request.Request(base + "/memory", headers=auth), timeout=2) as r:
                    memory = json.loads(r.read())
                for path in ("/history", "/memory", "/brain", "/goals"):
                    try:
                        urllib.request.urlopen(base + path, timeout=2)
                        self.fail("expected 401 for " + path)
                    except urllib.error.HTTPError as e:
                        self.assertEqual(e.code, 401)
                try:
                    urllib.request.urlopen(
                        urllib.request.Request(base + "/nope", headers=auth), timeout=2)
                    self.fail("expected 404")
                except urllib.error.HTTPError as e:
                    self.assertEqual(e.code, 404)
            finally:
                runner.stop_status_server()
        finally:
            del os.environ["OPENCLAW_IMDS_URL"]
        self.assertTrue(health["ok"])
        self.assertEqual(status["name"], "headless-test")
        self.assertEqual(status["cycle_count"], 1)
        self.assertEqual(len(history), 1)
        self.assertGreater(memory["total_entries"], 0)

    def test_stop_ends_loop(self):
        os.environ["OPENCLAW_IMDS_URL"] = "http://127.0.0.1:9"
        try:
            runner = HeadlessRunner(self._agent(), logging.getLogger("test"),
                                    interval=30, max_cycles=0, status_port=None)
            threading.Timer(0.2, runner.stop).start()
            cycles = runner.run()
        finally:
            del os.environ["OPENCLAW_IMDS_URL"]
        self.assertGreaterEqual(cycles, 1)
        self.assertFalse(runner.agent.running)


if __name__ == "__main__":
    unittest.main()
