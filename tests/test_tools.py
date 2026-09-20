"""Tests for the tool register.

The bug this exists to prevent shipped, twice. notify_operator and
estate_report each had a handler in the executor, a classification in the
authority spine, an IAM grant in Terraform and passing tests, and the planner
could not choose either of them, because both were missing from the one list
the model actually reads. The capability was complete everywhere except where
it counted, and nothing in the test suite noticed, because every piece was
tested in isolation and no test asked whether the pieces were connected.

So the important test here is not that any one tool works. It is that a tool
cannot be half-wired: a handler without a declaration, or a declaration
without a handler, fails the suite.
"""

import logging
import subprocess
import unittest
from unittest import mock

from jarvis.agent import authority, tools
from jarvis.agent.executor import TaskExecutor
from jarvis.agent.memory import AgentMemory
from jarvis.agent.planner import Task, TaskType

LOG = logging.getLogger("test")


class TestTheRegisterIsSound(unittest.TestCase):
    def test_register_validates(self):
        self.assertEqual(tools.validate(), [])

    def test_no_tool_is_declared_twice(self):
        names = [t.name for t in tools.TOOLS]
        self.assertEqual(len(names), len(set(names)))

    def test_every_tool_says_what_it_is_for(self):
        for t in tools.TOOLS:
            self.assertTrue(t.summary.strip(), f"{t.name} has no summary")
            self.assertTrue(t.summary.endswith(".") or t.summary.endswith("]"),
                            f"{t.name}: summary should be a sentence")


class TestNothingIsHalfWired(unittest.TestCase):
    """The drift that shipped. Both directions, so neither half can go missing."""

    def executor(self):
        return TaskExecutor({}, AgentMemory(max_entries=10), LOG)

    def test_every_declared_tool_has_a_task_type(self):
        values = {t.value for t in TaskType}
        for t in tools.TOOLS:
            self.assertIn(t.name, values,
                          f"{t.name} is declared but has no TaskType")

    def test_every_declared_tool_has_a_handler(self):
        handlers = self.executor()._handlers
        have = {k.value for k in handlers}
        for t in tools.TOOLS:
            self.assertIn(t.name, have,
                          f"{t.name} is declared but the executor cannot run it")

    def test_every_handler_has_a_declaration(self):
        """The other direction: code the model could never reach."""
        for kind in self.executor()._handlers:
            self.assertIsNotNone(tools.get(kind.value),
                                 f"{kind.value} has a handler but is not declared")

    def test_every_plannable_tool_reaches_the_model(self):
        """The exact failure: complete everywhere, absent from the one list read."""
        from jarvis.brain.llm import PLANNABLE_TASK_TYPES
        for t in tools.TOOLS:
            if not t.plannable:
                continue
            self.assertIn(t.name, PLANNABLE_TASK_TYPES,
                          f"{t.name} is declared plannable and the model cannot choose it")

    def test_an_operator_only_tool_is_not_offered_to_the_model(self):
        from jarvis.brain.llm import PLANNABLE_TASK_TYPES
        self.assertNotIn("user_command", PLANNABLE_TASK_TYPES)

    def test_the_authority_spine_reads_the_register(self):
        for t in tools.TOOLS:
            if t.effect != tools.READ or t.reporting:
                continue
            task = Task(description="x", task_type=TaskType(t.name), priority=5)
            self.assertEqual(authority.classify_task(task), authority.READ,
                             f"{t.name} reads only but the spine calls it a change")

    def test_a_changing_tool_is_classified_as_a_change(self):
        task = Task(description="x", task_type=TaskType.MAINTENANCE, priority=5)
        self.assertEqual(authority.classify_task(task), authority.CHANGE)

    def test_an_unreadable_register_makes_everything_a_change(self):
        """Fails closed: a broken register costs proposals, never surprises."""
        task = Task(description="x", task_type=TaskType.SYSTEM_CHECK, priority=5)
        with mock.patch.object(authority, "_looks_only", side_effect=lambda k: False):
            self.assertEqual(authority.classify_task(task), authority.CHANGE)


class TestPacing(unittest.TestCase):
    """min_rung is the dial: capability opens deliberately, one tool at a time."""

    def gated(self, **kw):
        return tools.Tool("calendar_read", "Read the operator's calendar.", **kw)

    def test_a_tool_above_the_rung_is_absent_not_refused(self):
        t = self.gated(min_rung=tools.ACTOR)
        self.assertFalse(t.available_at(tools.OBSERVER))
        self.assertFalse(t.available_at(tools.PROPOSER))
        self.assertTrue(t.available_at(tools.ACTOR))

    def test_config_may_tighten(self):
        t = tools.get("shell_command")
        self.assertEqual(tools.effective_rung(t, {"shell_command": "actor"}),
                         tools.ACTOR)

    def test_config_may_not_loosen(self):
        """A typo in a YAML file must not hand anything out."""
        t = self.gated(min_rung=tools.ACTOR)
        with mock.patch.object(tools, "TOOLS", tools.TOOLS + (t,)):
            self.assertEqual(tools.effective_rung(t, {"calendar_read": "observer"}),
                             tools.ACTOR)
            self.assertNotIn("calendar_read", tools.plannable(
                tools.PROPOSER, restrict={"calendar_read": "observer"}))

    def test_a_restricted_tool_leaves_the_offered_list(self):
        self.assertIn("read_logs", tools.plannable(tools.PROPOSER))
        self.assertNotIn("read_logs", tools.plannable(
            tools.PROPOSER, restrict={"read_logs": "actor"}))

    def test_a_withheld_tool_is_never_described_to_the_model(self):
        text = tools.describe(tools.PROPOSER, restrict={"read_logs": "actor"})
        self.assertNotIn("read_logs", text)

    def test_the_operator_can_still_see_what_is_withheld(self):
        self.assertIn("read_logs", tools.withheld(
            tools.PROPOSER, restrict={"read_logs": "actor"}))

    def test_idle_is_never_withheld(self):
        """Deciding there is nothing to do is not a capability to ration."""
        for rung in tools.RUNGS if hasattr(tools, "RUNGS") else authority.RUNGS:
            self.assertIn(tools.IDLE, tools.plannable(rung, restrict={"none": "actor"}))

    def test_nothing_is_withheld_today(self):
        """The dial exists and is currently fully open; changing that is a decision."""
        self.assertEqual(tools.withheld(tools.PROPOSER), [])


class TestGatedVersusBroken(unittest.TestCase):
    """The agent's own ask, and it was right.

    Put the design to it and it answered, among other things: "I need the
    ability to see when a capability is gated by design versus broken by
    accident." Hiding the gate entirely leaves an unexplained absence, and
    this codebase already holds that an absence a model cannot see is one it
    invents something to fill. Withheld tools were the one place that
    principle was not applied.
    """

    def test_it_is_told_that_capability_is_gated(self):
        note = tools.gating_note(tools.PROPOSER, restrict={"read_logs": "actor"})
        self.assertIn("1 further capability is", note)
        self.assertIn("by design", note)

    def test_it_is_never_told_which(self):
        note = tools.gating_note(tools.PROPOSER,
                                 restrict={"read_logs": "actor", "estate_report": "actor"})
        self.assertNotIn("read_logs", note)
        self.assertNotIn("estate_report", note)
        self.assertIn("2 further capabilities are", note)

    def test_a_fault_is_distinguished_from_a_boundary(self):
        """The distinction is the whole point: one is a wall, the other is a bug."""
        for note in (tools.gating_note(tools.PROPOSER),
                     tools.gating_note(tools.PROPOSER, restrict={"read_logs": "actor"})):
            self.assertIn("fault", note)
            self.assertIn("boundary", note)

    def test_with_nothing_gated_it_is_told_so_plainly(self):
        note = tools.gating_note(tools.PROPOSER)
        self.assertIn("Every capability", note)
        self.assertNotIn("withheld", note)

    def test_conversation_is_told_it_cannot_act(self):
        """The confabulation this caused, caught on the live box.

        Within minutes of the tool list reaching the context, the agent was
        asked in conversation to use read_logs. It answered "I used read_logs
        ... and found no entries", having run nothing, attributed the emptiness
        to a different file that happened to exist and be empty, and concluded
        the system was stable. The journal had 53 lines in that window.

        It was not inventing for the sake of it. It had been handed a list of
        its tools in a context that never said it could not reach them, which
        is the same shape as the path problem this codebase already fixed: an
        absence a model cannot see is one it fills in.
        """
        brain, agent = self._brain_and_agent()
        answering = brain.build_context(agent, {}, mode="answer")["mandate"]["right_now"]
        self.assertIn("not acting", answering)
        self.assertIn("never report having used one", answering)

    def test_planning_is_told_its_choice_is_real(self):
        brain, agent = self._brain_and_agent()
        planning = brain.build_context(agent, {}, mode="plan")["mandate"]["right_now"]
        self.assertIn("will be run", planning)
        self.assertNotIn("not acting", planning)

    def test_planning_is_the_default(self):
        brain, agent = self._brain_and_agent()
        self.assertEqual(brain.build_context(agent, {})["mandate"]["right_now"],
                         brain.build_context(agent, {}, mode="plan")["mandate"]["right_now"])

    def test_an_unknown_mode_falls_back_to_planning(self):
        brain, agent = self._brain_and_agent()
        self.assertEqual(brain.build_context(agent, {}, mode="nonsense")["mandate"]["right_now"],
                         brain.build_context(agent, {}, mode="plan")["mandate"]["right_now"])

    def _brain_and_agent(self, **cfg):
        from jarvis.agent.core import AgentCore
        from jarvis.brain.llm import BaseBrain

        class Offline(BaseBrain):
            provider = "test"
            def _make_client(self): return object()
            def _complete(self, *a, **k): raise AssertionError("no call expected")
            def _handle_error(self, exc): return None

        brain = Offline({"max_calls_per_hour": 10}, LOG)
        conf = {"name": "t", "profile": "cloud", "rung": "proposer"}
        conf.update(cfg)
        agent = AgentCore(conf, {"display": None, "input": None,
                                 "memory": None, "storage": None}, LOG, brain=brain)
        agent.planner._boot_tasks_generated = True
        return brain, agent

    def test_the_note_reaches_the_model(self):
        from jarvis.agent.core import AgentCore
        from jarvis.brain.llm import BaseBrain

        class Offline(BaseBrain):
            provider = "test"
            def _make_client(self): return object()
            def _complete(self, *a, **k): raise AssertionError("no call expected")
            def _handle_error(self, exc): return None

        brain = Offline({"max_calls_per_hour": 10}, LOG)
        agent = AgentCore({"name": "t", "profile": "cloud", "rung": "proposer",
                           "tools": {"read_logs": "actor"}},
                          {"display": None, "input": None, "memory": None,
                           "storage": None}, LOG, brain=brain)
        agent.planner._boot_tasks_generated = True
        mandate = brain.build_context(agent, {})["mandate"]
        self.assertIn("1 further capability is", mandate["capability"])
        self.assertNotIn("read_logs", mandate["tools"])


class TestToolSpecs(unittest.TestCase):
    def test_specs_are_well_formed_for_converse(self):
        for spec in tools.specs(tools.ACTOR):
            ts = spec["toolSpec"]
            self.assertTrue(ts["name"] and ts["description"])
            schema = ts["inputSchema"]["json"]
            self.assertEqual(schema["type"], "object")
            for r in schema["required"]:
                self.assertIn(r, schema["properties"])

    def test_a_required_argument_must_be_declared(self):
        bad = tools.Tool("x", "y.", required=("nope",))
        with mock.patch.object(tools, "TOOLS", (bad,)):
            self.assertTrue(any("nope" in p for p in tools.validate()))


class TestReadLogs(unittest.TestCase):
    """A narrow window on its own journal, not a way to read the machine."""

    def executor(self):
        return TaskExecutor({}, AgentMemory(max_entries=50), LOG,
                            shell_policy={"max_output": 500})

    def task(self, **meta):
        return Task(description="look", task_type=TaskType.READ_LOGS,
                    priority=5, metadata=meta)

    def test_it_only_reads_its_own_units(self):
        out = self.executor().execute(self.task(unit="sshd"))
        self.assertFalse(out["success"])
        self.assertIn("not a unit this agent may read", out["error"])

    def test_the_refusal_names_what_is_allowed(self):
        out = self.executor().execute(self.task(unit="audit"))
        for unit in ("jarvis", "cloudflared"):
            self.assertIn(unit, out["error"])

    def test_the_window_is_capped(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "line\n", "")
            self.executor().execute(self.task(minutes=99999))
            argv = run.call_args[0][0]
            self.assertIn("-180min", argv)

    def test_a_silly_window_does_not_crash_it(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            for bad in ("soon", None, -5, 0):
                out = self.executor().execute(self.task(minutes=bad))
                self.assertTrue(out["success"], f"minutes={bad!r} broke it")

    def test_no_shell_is_involved(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "x\n", "")
            self.executor().execute(self.task(unit="jarvis", grep="a; rm -rf /"))
            argv = run.call_args[0][0]
            self.assertIsInstance(argv, list)
            self.assertEqual(argv[0], "journalctl")
            self.assertNotIn("rm -rf /", " ".join(argv),
                             "the filter must never reach a command line")

    def test_grep_filters_in_python_and_reports_what_it_scanned(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                [], 0, "alpha\nbeta\ngamma beta\n", "")
            out = self.executor().execute(self.task(grep="beta"))["output"]
        self.assertEqual(out["lines"], 2)
        self.assertEqual(out["scanned"], 3)

    def test_output_is_bounded(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "x" * 5000 + "\n", "")
            out = self.executor().execute(self.task())["output"]
        self.assertLessEqual(len(out["log"]), 500)
        self.assertTrue(out["truncated"])

    def test_an_empty_window_is_a_fact_not_a_clean_bill_of_health(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            out = self.executor().execute(self.task())["output"]
        self.assertEqual(out["lines"], 0)
        self.assertIn("not a clean bill of health", out["note"])

    def test_a_missing_journalctl_is_reported_plainly(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
            out = self.executor().execute(self.task())
        self.assertFalse(out["success"])
        self.assertIn("journalctl", out["error"])

    def test_a_timeout_is_reported_plainly(self):
        with mock.patch("subprocess.run",
                        side_effect=subprocess.TimeoutExpired("journalctl", 20)):
            out = self.executor().execute(self.task())
        self.assertFalse(out["success"])
        self.assertIn("timed out", out["error"])

    def test_it_reads_only_and_needs_no_proposal(self):
        task = self.task()
        self.assertEqual(authority.classify_task(task), authority.READ)
        self.assertTrue(authority.review(task, authority.PROPOSER).allowed)
        self.assertTrue(authority.review(task, authority.OBSERVER).allowed)


if __name__ == "__main__":
    unittest.main()
