"""Tests for filesystem grounding: real paths in, invented paths caught.

The failure these guard against is a model naming a path it never checked.
The fix has three halves, and each is tested here: the agent reports real
paths (including missing ones) to the model, the agent can look at a real
tree on demand, and a path the model names is verified against the disk.
"""

import logging
import os
import tempfile
import unittest

from jarvis.agent import environment as env
from jarvis.agent.core import AgentCore
from jarvis.agent.executor import TaskExecutor
from jarvis.agent.memory import AgentMemory
from jarvis.agent.planner import Task, TaskType
from jarvis.brain.llm import BaseBrain, Decision

LOG = logging.getLogger("test")
NO_HW = {"display": None, "input": None, "memory": None, "storage": None}


class Brain(BaseBrain):
    """BaseBrain is abstract; only the context helpers are under test."""

    def _make_client(self):
        return object()

    def _system_text(self):
        return "system"


# ---- what is really there --------------------------------------------------------

class TestStatPath(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_missing_path_is_reported_as_missing_not_omitted(self):
        info = env.stat_path(os.path.join(self.tmp, "nope.log"))
        self.assertIs(info["exists"], False)
        self.assertEqual(info["kind"], "missing")

    def test_empty_file_is_flagged_empty(self):
        # The distinction the planner got wrong: an empty report file is not
        # a report of zero findings.
        path = os.path.join(self.tmp, "empty.log")
        open(path, "w").close()
        info = env.stat_path(path)
        self.assertEqual(info["kind"], "file")
        self.assertTrue(info["empty"])
        self.assertEqual(info["size"], 0)

    def test_file_reports_size_and_mode(self):
        path = os.path.join(self.tmp, "a.txt")
        with open(path, "w") as fh:
            fh.write("hello")
        info = env.stat_path(path)
        self.assertEqual(info["size"], 5)
        self.assertFalse(info["empty"])
        self.assertRegex(info["mode"], r"^\d{4}$")

    def test_directory_reports_entry_count(self):
        os.mkdir(os.path.join(self.tmp, "sub"))
        info = env.stat_path(self.tmp)
        self.assertEqual(info["kind"], "dir")
        self.assertEqual(info["entries"], 1)

    def test_symlink_reports_target(self):
        target = os.path.join(self.tmp, "real")
        link = os.path.join(self.tmp, "link")
        open(target, "w").close()
        os.symlink(target, link)
        info = env.stat_path(link)
        self.assertEqual(info["kind"], "symlink")
        self.assertEqual(info["target"], target)


class TestFence(unittest.TestCase):

    def test_ledger_and_its_key_are_fenced(self):
        self.assertIsNotNone(env.fenced_reason("/var/lib/jarvis/ledger.jsonl"))
        self.assertIsNotNone(env.fenced_reason("/etc/jarvis/ledger/key"))

    def test_agent_control_plane_is_fenced(self):
        self.assertIsNotNone(env.fenced_reason("/run/jarvis"))
        self.assertIsNotNone(env.fenced_reason("/run/jarvis/token"))

    def test_fence_matches_path_components_not_prefixes(self):
        # /run/jarvisx is someone else's directory, not the agent's.
        self.assertIsNone(env.fenced_reason("/run/jarvisx"))
        self.assertIsNone(env.fenced_reason("/etc/jarvis"))

    def test_the_two_fences_agree(self):
        """One secret, two mechanisms, and they had drifted.

        The shell deny-list refuses the runner token, the UI session key, the
        tunnel token, the notify destination and the memory database. The path
        fence did not name any of them. Nothing could read a file's contents,
        so the gap was latent -- and adding a reader would have made it live.
        Two lists of the same thing is how a fence gets a hole in it, so this
        asserts they cover the same ground.
        """
        from jarvis.agent.executor import DEFAULT_SHELL_DENY_PATTERNS
        import re as _re
        for path in env.SECRET_PATHS:
            self.assertIsNotNone(
                env.fenced_reason(path),
                f"{path} is refused to the shell but readable as a path")
            self.assertTrue(
                any(_re.search(pat, f"cat {path}") for pat in DEFAULT_SHELL_DENY_PATTERNS),
                f"{path} is fenced for reading but not refused to the shell")

    def test_credential_directories_are_fenced_anywhere(self):
        self.assertIsNotNone(env.fenced_reason("/home/anyone/.ssh/id_rsa"))
        self.assertIsNotNone(env.fenced_reason("/root/.aws/credentials"))

    def test_fenced_path_stats_as_fenced_with_a_reason(self):
        info = env.stat_path("/run/jarvis/token")
        self.assertEqual(info["kind"], "fenced")
        self.assertTrue(info["reason"])
        self.assertNotIn("size", info)


class TestTree(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "a", "b"))
        for name in ("one", "two", "three"):
            open(os.path.join(self.tmp, name), "w").close()

    def test_lists_real_entries(self):
        result = env.tree(self.tmp, depth=1)
        names = {os.path.basename(e["path"]) for e in result["entries"]}
        self.assertEqual(names, {"a", "one", "two", "three"})

    def test_depth_limits_descent(self):
        shallow = env.tree(self.tmp, depth=1)
        deep = env.tree(self.tmp, depth=2)
        self.assertNotIn("b", {os.path.basename(e["path"]) for e in shallow["entries"]})
        self.assertIn("b", {os.path.basename(e["path"]) for e in deep["entries"]})

    def test_entry_limit_truncates_and_says_so(self):
        result = env.tree(self.tmp, depth=1, limit=2)
        self.assertEqual(len(result["entries"]), 2)
        self.assertIn("truncated", result)

    def test_missing_directory_is_an_answer_not_an_error(self):
        result = env.tree(os.path.join(self.tmp, "nowhere"), depth=1)
        self.assertEqual(result["entries"], [])
        self.assertIn("note", result)

    def test_fenced_root_is_refused(self):
        result = env.tree("/run/jarvis")
        self.assertIn("refused", result)
        self.assertNotIn("entries", result)

    def test_fenced_children_are_skipped_and_named(self):
        os.makedirs(os.path.join(self.tmp, ".ssh"))
        result = env.tree(self.tmp, depth=1)
        names = {os.path.basename(e["path"]) for e in result["entries"]}
        self.assertNotIn(".ssh", names)
        self.assertTrue(any(p.endswith(".ssh") for p in result["fenced"]))


class TestSnapshot(unittest.TestCase):

    def test_missing_paths_are_listed_explicitly(self):
        snap = env.snapshot(["/etc/hosts", "/definitely/not/here"])
        self.assertIn("/definitely/not/here", snap["missing"])
        self.assertEqual(len(snap["paths"]), 2)

    def test_reports_host_identity(self):
        snap = env.snapshot(["/etc/hosts"])
        self.assertIn("kernel", snap["host"])
        self.assertIn("python", snap["host"])


# ---- catching an invented path ---------------------------------------------------

class TestExtractAndVerify(unittest.TestCase):

    def test_extracts_absolute_paths(self):
        found = env.extract_paths("write /opt/jarvis/run.sh then read /var/log/app/x.log")
        self.assertEqual(found, ["/opt/jarvis/run.sh", "/var/log/app/x.log"])

    def test_ignores_urls_and_relative_paths(self):
        found = env.extract_paths("see https://example.com/a/b or docs/readme.md")
        self.assertEqual(found, [])

    def test_single_segment_is_ignored_by_default(self):
        # The UI's own /goal and /think are not paths.
        self.assertEqual(env.extract_paths("/goal do a thing and /think"), [])

    def test_single_segment_is_kept_when_the_operator_asks(self):
        found = env.extract_paths("what is in /opt", min_segments=env.MIN_SEGMENTS_ASKED)
        self.assertEqual(found, ["/opt"])

    def test_verify_separates_real_missing_and_fenced(self):
        result = env.verify("/etc/hosts and /no/such/place and /run/jarvis/token")
        self.assertIn("/etc/hosts", result["present"])
        self.assertIn("/no/such/place", result["missing"])
        self.assertIn("/run/jarvis/token", result["fenced"])

    def test_note_is_none_when_nothing_was_invented(self):
        self.assertIsNone(env.verification_note(env.verify("/etc/hosts")))

    def test_note_names_the_invented_paths(self):
        # The exact failure: both of these were invented by the planner.
        note = env.verification_note(
            env.verify("save to /opt/jarvis/check.sh, grep /var/log/jarvis/security_scan.log"))
        self.assertIn("/opt/jarvis/check.sh", note)
        self.assertIn("/var/log/jarvis/security_scan.log", note)
        self.assertIn("do not exist", note)


# ---- the agent's use of it -------------------------------------------------------

class TestInspectPathTask(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.executor = TaskExecutor(dict(NO_HW), AgentMemory(), LOG)

    def _run(self, path, **meta):
        meta["path"] = path
        return self.executor.execute(Task(priority=1, description="look",
                                          task_type=TaskType.INSPECT_PATH, metadata=meta))

    def test_lists_a_real_directory(self):
        open(os.path.join(self.tmp, "here.txt"), "w").close()
        result = self._run(self.tmp)
        self.assertTrue(result["success"])
        names = {os.path.basename(e["path"]) for e in result["output"]["entries"]}
        self.assertIn("here.txt", names)

    def test_missing_path_succeeds_with_the_absence_as_the_answer(self):
        result = self._run(os.path.join(self.tmp, "gone"))
        self.assertTrue(result["success"])
        self.assertIs(result["output"]["exists"], False)

    def test_refuses_a_fenced_path(self):
        result = self._run("/var/lib/jarvis/ledger.jsonl")
        self.assertFalse(result["success"])
        self.assertIn("refused", result["error"])

    def test_refuses_a_relative_path(self):
        result = self._run("etc/passwd")
        self.assertFalse(result["success"])
        self.assertIn("absolute", result["error"])

    def test_needs_a_path(self):
        result = self.executor.execute(Task(priority=1, description="look",
                                            task_type=TaskType.INSPECT_PATH, metadata={}))
        self.assertFalse(result["success"])

    def test_accepts_the_path_in_the_command_field(self):
        # The model puts it in "command"; the schema says so.
        result = self.executor.execute(Task(priority=1, description="look",
                                            task_type=TaskType.INSPECT_PATH,
                                            metadata={"command": self.tmp}))
        self.assertTrue(result["success"])


class TestContextGrounding(unittest.TestCase):

    def setUp(self):
        self.agent = AgentCore({"name": "t", "profile": "cloud"}, dict(NO_HW), LOG)
        self.agent.planner._boot_tasks_generated = True

    def test_context_carries_checked_paths(self):
        brain = Brain({"environment_paths": ["/etc/hosts", "/nowhere/at/all"]}, LOG)
        context = brain.build_context(self.agent, {})
        paths = context["environment"]["paths"]
        self.assertEqual({p["path"] for p in paths}, {"/etc/hosts", "/nowhere/at/all"})
        self.assertIn("/nowhere/at/all", context["environment"]["missing"])

    def test_operator_question_resolves_a_real_directory(self):
        tmp = tempfile.mkdtemp()
        open(os.path.join(tmp, "found.txt"), "w").close()
        looked = Brain._look_up_paths(f"what is in {tmp}?")
        self.assertEqual(looked[0]["root"], tmp)
        names = {os.path.basename(e["path"]) for e in looked[0]["entries"]}
        self.assertIn("found.txt", names)

    def test_operator_question_about_a_missing_path_says_missing(self):
        looked = Brain._look_up_paths("check /no/such/directory please")
        self.assertIs(looked[0]["exists"], False)


class TestPlanAndAnswerChecks(unittest.TestCase):

    def setUp(self):
        self.agent = AgentCore({"name": "t", "profile": "cloud"}, dict(NO_HW), LOG)
        self.agent.planner._boot_tasks_generated = True

    def test_command_naming_a_missing_path_is_flagged(self):
        task = Task(priority=1, description="run it", task_type=TaskType.SHELL_COMMAND,
                    metadata={"command": "grep warning /var/log/jarvis/security_scan.log"})
        self.assertEqual(self.agent._unverified_paths(task),
                         ["/var/log/jarvis/security_scan.log"])

    def test_command_naming_a_real_path_is_not_flagged(self):
        task = Task(priority=1, description="run it", task_type=TaskType.SHELL_COMMAND,
                    metadata={"command": "wc -l /etc/hosts"})
        self.assertEqual(self.agent._unverified_paths(task), [])

    def test_inspect_path_is_exempt(self):
        # Asking whether a path exists is the cure, not the disease.
        task = Task(priority=1, description="look", task_type=TaskType.INSPECT_PATH,
                    metadata={"path": "/var/log/jarvis/security_scan.log"})
        self.assertEqual(self.agent._unverified_paths(task), [])

    def test_flagged_paths_reach_the_model_and_the_ledger(self):
        recorded = []
        self.agent.ledger.record = lambda kind, body: recorded.append((kind, body)) or True
        task = Task(priority=1, description="check the log", task_type=TaskType.SHELL_COMMAND,
                    metadata={"command": "cat /var/log/jarvis/security_scan.log", "source": "llm"})
        self.agent._apply_decision(Decision(reasoning="r", task=task))
        self.assertEqual(self.agent.last_thought["unverified_paths"],
                         ["/var/log/jarvis/security_scan.log"])
        alerts = [b for k, b in recorded if k == "alert"]
        self.assertEqual(alerts[0]["alert"], "unverified_path")

    def test_answer_inventing_a_path_gets_a_visible_check(self):
        answer, result = self.agent._check_answer_paths(
            "Save the script to /opt/jarvis/system_health_check.sh and run it.")
        self.assertIn("[path check]", answer)
        self.assertIn("/opt/jarvis/system_health_check.sh", result["missing"])

    def test_answer_with_only_real_paths_is_left_alone(self):
        answer, result = self.agent._check_answer_paths("The file is at /etc/hosts.")
        self.assertNotIn("[path check]", answer)
        self.assertEqual(result["missing"], [])


if __name__ == "__main__":
    unittest.main()


class TestReadingAFile(unittest.TestCase):
    """inspect_path says what is at a path. This says what is in it.

    The operator could hand the agent a document and the agent could see its
    name, size and timestamp and not one word of it. That is the half-built
    capability most likely to be filled in with invention.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def write(self, name, data, mode="w"):
        path = os.path.join(self.tmp, name)
        with open(path, mode) as f:
            f.write(data)
        return path

    def test_it_reads_a_text_file(self):
        path = self.write("notes.txt", "line one\nline two\nline three\n")
        got = env.read_file(path)
        self.assertTrue(got["read"])
        self.assertIn("line two", got["text"])
        self.assertEqual(got["lines_in_file"], 3)

    def test_an_empty_file_says_so_rather_than_returning_nothing(self):
        got = env.read_file(self.write("empty.txt", ""))
        self.assertTrue(got["read"])
        self.assertTrue(got["empty"])
        self.assertIn("nothing in it", got["note"])

    def test_a_missing_file_is_an_answer_not_a_crash(self):
        got = env.read_file(os.path.join(self.tmp, "nope.txt"))
        self.assertFalse(got["read"])
        self.assertIn("nothing at this path", got["reason"])

    def test_a_directory_is_refused_with_advice(self):
        got = env.read_file(self.tmp)
        self.assertFalse(got["read"])
        self.assertIn("directory", got["reason"])

    def test_binary_is_reported_as_binary_rather_than_decoded(self):
        """A model shown mojibake will describe it, confidently."""
        path = self.write("blob.bin", bytes(range(256)) * 20, mode="wb")
        got = env.read_file(path)
        self.assertFalse(got["read"])
        self.assertIn("binary", got["reason"])

    def test_a_big_file_is_truncated_and_says_where_it_stopped(self):
        path = self.write("big.txt", "x" * 200_000)
        got = env.read_file(path, max_bytes=1000)
        self.assertTrue(got["read"])
        self.assertTrue(got["truncated"])
        self.assertIn("has not been seen", got["note"])
        self.assertLessEqual(len(got["text"]), 1000)

    def test_the_cap_cannot_be_raised_past_the_ceiling(self):
        path = self.write("big.txt", "y" * 200_000)
        got = env.read_file(path, max_bytes=10_000_000)
        self.assertLessEqual(len(got["text"]), env.MAX_READ_BYTES)

    def test_it_can_start_part_way_down_and_says_what_it_skipped(self):
        path = self.write("many.txt", "\n".join(f"line {i}" for i in range(1, 51)))
        got = env.read_file(path, start_line=10, max_lines=5)
        self.assertTrue(got["text"].startswith("line 10"))
        self.assertEqual(got["lines_shown"], 5)
        self.assertIn("of 50", got["note"])

    def test_a_fenced_path_is_refused_by_reason(self):
        got = env.read_file("/etc/shadow")
        self.assertFalse(got["read"])
        self.assertTrue(got["fenced"])
        self.assertIn("credentials", got["reason"])

    def test_every_secret_is_refused(self):
        for path in env.SECRET_PATHS:
            got = env.read_file(path)
            self.assertFalse(got["read"], f"{path} was readable")
            self.assertTrue(got.get("fenced"), f"{path} was not fenced")
