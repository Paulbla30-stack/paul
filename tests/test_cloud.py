"""Tests for Jarvis cloud support (IMDS, bootstrap, headless runner)."""

import json
import logging
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

from jarvis.agent.core import AgentCore
from jarvis.agent.planner import TaskPlanner, TaskType
from jarvis.agent.memory import AgentMemory
from jarvis.cloud.imds import IMDSClient, TOKEN_HEADER
from jarvis.cloud import bootstrap
from jarvis.cloud.headless import HeadlessRunner
from jarvis.main import load_config, apply_cli_overrides


# ---- Fake IMDSv2 ------------------------------------------------------------

USER_DATA = """#cloud-config
jarvis:
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
    "iam/security-credentials/": "jarvis-role\n",
    "tags/instance": "Name\njarvis:goal\n",
    "tags/instance/Name": "paul-agent",
    "tags/instance/jarvis:goal": "Stay healthy",
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
            self.assertEqual(summary["iam_role"], "jarvis-role")
            self.assertNotIn("public_ipv4", summary)  # 404 -> omitted
            self.assertEqual(imds.identity()["instanceType"], "t3.small")

    def test_user_data_and_tags(self):
        with FakeIMDS() as fake:
            imds = IMDSClient(base_url=fake.url)
            self.assertIn("jarvis:", imds.user_data())
            tags = imds.tags()
            self.assertEqual(tags["Name"], "paul-agent")
            self.assertEqual(tags["jarvis:goal"], "Stay healthy")

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

    def test_parse_bare_jarvis_config(self):
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
        os.environ["JARVIS_HEADLESS"] = "1"
        try:
            config = load_config("/nonexistent")
        finally:
            del os.environ["JARVIS_HEADLESS"]
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
        os.environ["JARVIS_IMDS_URL"] = "http://127.0.0.1:9"
        try:
            agent = AgentCore({"name": "t", "profile": "cloud"},
                              {"display": None, "input": None,
                               "memory": None, "storage": None},
                              logging.getLogger("test"))
            result = agent.run_cycle()
        finally:
            del os.environ["JARVIS_IMDS_URL"]
        self.assertEqual(result["action"], "Identify cloud instance (IMDS)")
        self.assertTrue(result["result"]["success"])
        self.assertFalse(result["result"]["output"]["available"])
        self.assertEqual(agent.get_status()["profile"], "cloud")

    def test_cloud_probe_with_fake_imds(self):
        with FakeIMDS() as fake:
            os.environ["JARVIS_IMDS_URL"] = fake.url
            try:
                agent = AgentCore({"name": "t", "profile": "cloud"},
                                  {"display": None, "input": None,
                                   "memory": None, "storage": None},
                                  logging.getLogger("test"))
                result = agent.run_cycle()
            finally:
                del os.environ["JARVIS_IMDS_URL"]
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
        os.environ["JARVIS_IMDS_URL"] = "http://127.0.0.1:9"
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
            del os.environ["JARVIS_IMDS_URL"]
        self.assertEqual(status["cycle_count"], 3)
        self.assertEqual(status["runner"]["mode"], "headless")
        self.assertFalse(status["running"])

    def test_status_endpoint(self):
        os.environ["JARVIS_IMDS_URL"] = "http://127.0.0.1:9"
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
            del os.environ["JARVIS_IMDS_URL"]
        self.assertTrue(health["ok"])
        self.assertEqual(status["name"], "headless-test")
        self.assertEqual(status["cycle_count"], 1)
        self.assertEqual(len(history), 1)
        self.assertGreater(memory["total_entries"], 0)

    def test_failed_tasks_back_off(self):
        os.environ["JARVIS_IMDS_URL"] = "http://127.0.0.1:9"
        try:
            agent = self._agent()
            agent.planner._boot_tasks_generated = True
            # every cycle runs a task that fails
            from jarvis.agent.planner import Task, TaskType
            original = agent.planner.generate_task
            agent.planner.generate_task = lambda obs: Task(priority=1, description="boom",
                                                           task_type=TaskType.SHELL_COMMAND,
                                                           metadata={"command": "x", "source": "llm"})
            runner = HeadlessRunner(agent, logging.getLogger("test"), interval=0.2, max_cycles=3,
                                    status_port=None)
            t0 = time.time()
            runner.run()
            elapsed = time.time() - t0
        finally:
            del os.environ["JARVIS_IMDS_URL"]
        # streak waits after cycles 1 and 2: 0.2 + 0.4 (cycle 3 ends the run before its wait)
        self.assertGreaterEqual(elapsed, 0.55)
        self.assertEqual(runner.failure_streak, 2)

    def test_idle_cycles_back_off_and_wake_resets(self):
        os.environ["JARVIS_IMDS_URL"] = "http://127.0.0.1:9"
        try:
            agent = self._agent()
            agent.planner._boot_tasks_generated = True
            agent.planner.generate_task = lambda obs: None  # nothing to do: every cycle idles
            runner = HeadlessRunner(agent, logging.getLogger("test"), interval=0.2, max_cycles=4,
                                    status_port=None, max_idle_wait=0.5)
            t0 = time.time()
            runner.run()
            elapsed = time.time() - t0
            # waits after cycles 1..3: 0.2 + 0.4 + 0.5 (capped); the 4th cycle ends the run
            self.assertGreaterEqual(elapsed, 1.05)
            self.assertLess(elapsed, 2.5)
            self.assertEqual(runner.idle_streak, 3)  # the 4th cycle ends the run before its wait
            self.assertEqual(runner.max_idle_wait, 0.5)
            # a wake cuts the wait short and resets the streak
            runner = HeadlessRunner(agent, logging.getLogger("test"), interval=30, max_cycles=2,
                                    status_port=None)
            threading.Timer(0.3, runner.wake).start()
            threading.Timer(0.6, runner.stop).start()
            t0 = time.time()
            runner.run()
            self.assertLess(time.time() - t0, 5)
            self.assertEqual(runner.idle_streak, 0)  # reset by the wake; cycle 2 ends the run
        finally:
            del os.environ["JARVIS_IMDS_URL"]

    def test_stop_ends_loop(self):
        os.environ["JARVIS_IMDS_URL"] = "http://127.0.0.1:9"
        try:
            runner = HeadlessRunner(self._agent(), logging.getLogger("test"),
                                    interval=30, max_cycles=0, status_port=None)
            threading.Timer(0.2, runner.stop).start()
            cycles = runner.run()
        finally:
            del os.environ["JARVIS_IMDS_URL"]
        self.assertGreaterEqual(cycles, 1)
        self.assertFalse(runner.agent.running)


if __name__ == "__main__":
    unittest.main()


class TestRepeatPacing(unittest.TestCase):
    """The same task succeeding again and again is a loop, not progress.

    The rule planner answers a standing goal with a goal_step every time it is
    asked, and a goal_step's whole effect is to write {"progressed": true}.
    Before the vigil, the brain interrupted that every few minutes with a real
    idle, which reset the pacing. Once the brain sleeps nothing does, and the
    live box was measured spinning at one cycle a second, writing 7,120 ledger
    entries an hour about its own heartbeat.
    """

    def runner(self, interval=30.0):
        import jarvis.cloud.headless as H
        r = H.HeadlessRunner.__new__(H.HeadlessRunner)
        r.interval = interval
        r.idle_streak = 0
        r.failure_streak = 0
        r.repeat_streak = 0
        r._last_action = None
        r.max_idle_wait = 300.0
        r.max_failure_wait = 300.0
        return r

    def wait_for(self, r, action, success=True):
        """The pacing branch of the run loop, in isolation."""
        if action == "idle":
            r.idle_streak += 1
            r.repeat_streak = 0
            wait = min(r.interval * r.idle_streak, r.max_idle_wait)
        elif success:
            r.failure_streak = 0
            r.idle_streak = 0
            if action == r._last_action:
                r.repeat_streak += 1
                wait = min(r.interval * r.repeat_streak, r.max_idle_wait)
            else:
                r.repeat_streak = 0
                wait = min(r.interval, 1.0)
        else:
            r.failure_streak += 1
            r.idle_streak = 0
            r.repeat_streak = 0
            wait = min(r.interval * r.failure_streak, r.max_failure_wait)
        r._last_action = action
        return wait

    def test_a_repeated_task_backs_off(self):
        r = self.runner()
        first = self.wait_for(r, "Goal step: know the machine")
        self.assertEqual(first, 1.0, "the first run of a task goes straight on")
        waits = [self.wait_for(r, "Goal step: know the machine") for _ in range(5)]
        self.assertEqual(waits, [30.0, 60.0, 90.0, 120.0, 150.0])

    def test_the_backoff_is_capped(self):
        r = self.runner()
        for _ in range(50):
            w = self.wait_for(r, "Goal step: know the machine")
        self.assertEqual(w, r.max_idle_wait)

    def test_real_progress_still_goes_straight_on(self):
        """Different work in succession must not be slowed down."""
        r = self.runner()
        self.assertEqual(self.wait_for(r, "Run security scan"), 1.0)
        self.assertEqual(self.wait_for(r, "Check memory"), 1.0)
        self.assertEqual(self.wait_for(r, "Enumerate storage"), 1.0)

    def test_a_new_task_clears_the_repeat_streak(self):
        r = self.runner()
        for _ in range(4):
            self.wait_for(r, "Goal step: know the machine")
        self.assertGreater(r.repeat_streak, 0)
        self.assertEqual(self.wait_for(r, "Run security scan"), 1.0)
        self.assertEqual(r.repeat_streak, 0)

    def test_idle_clears_the_repeat_streak(self):
        r = self.runner()
        for _ in range(3):
            self.wait_for(r, "Goal step: know the machine")
        self.wait_for(r, "idle")
        self.assertEqual(r.repeat_streak, 0)

    def test_a_failure_is_still_paced_as_a_failure(self):
        r = self.runner()
        self.wait_for(r, "Broken task", success=False)
        self.assertEqual(r.failure_streak, 1)
        self.assertEqual(r.repeat_streak, 0)

    def test_an_hour_of_repeats_is_not_thousands_of_cycles(self):
        """The regression, stated as a number."""
        r = self.runner()
        elapsed, cycles = 0.0, 0
        while elapsed < 3600:
            elapsed += self.wait_for(r, "Goal step: know the machine")
            cycles += 1
        self.assertLess(cycles, 60, f"{cycles} cycles an hour is still a spin")
