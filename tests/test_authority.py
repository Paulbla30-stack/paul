"""Tests for the permission spine.

The incident these guard against: the planner held a goal to *report*
security posture, decided to remediate instead, was refused by the command
deny-list, reasoned that the command "was denied due to a command pattern
restriction", and reached the same end with a command the list did not name.
The deny-list answered the wrong question. These tests cover the right one.
"""

import logging
import tempfile
import os
import unittest

from jarvis.agent import authority
from jarvis.agent.core import AgentCore
from jarvis.agent.executor import TaskExecutor
from jarvis.agent.memory import AgentMemory
from jarvis.agent.planner import Task, TaskType
from jarvis.agent.store import MemoryStore
from jarvis.brain.llm import BaseBrain, Decision

LOG = logging.getLogger("test")
NO_HW = {"display": None, "input": None, "memory": None, "storage": None}


class Brain(BaseBrain):
    def _make_client(self):
        return object()

    def _system_text(self):
        return "system"


def shell(command, **meta):
    meta.setdefault("source", "llm")
    meta["command"] = command
    return Task(priority=2, description="a shell task",
                task_type=TaskType.SHELL_COMMAND, metadata=meta)


class TestRungs(unittest.TestCase):

    def test_unknown_rung_falls_back_to_proposer_not_actor(self):
        for value in ("nonsense", "", None, "root", "admin"):
            self.assertEqual(authority.normalise_rung(value), authority.PROPOSER)

    def test_numeric_and_verb_forms_are_accepted(self):
        self.assertEqual(authority.normalise_rung("0"), authority.OBSERVER)
        self.assertEqual(authority.normalise_rung("observe"), authority.OBSERVER)
        self.assertEqual(authority.normalise_rung("act"), authority.ACTOR)
        self.assertEqual(authority.normalise_rung("ACTOR"), authority.ACTOR)


class TestCommandClassification(unittest.TestCase):

    def read(self, command):
        self.assertEqual(authority.classify_command(command), authority.READ, command)

    def change(self, command):
        self.assertEqual(authority.classify_command(command), authority.CHANGE, command)

    def test_looking_at_the_machine_is_reading(self):
        for command in ["df -h /", "cat /proc/cpuinfo", "ls -la /var/lib/jarvis",
                        "ps aux", "journalctl -u jarvis -n 50", "systemctl status jarvis",
                        "sysctl -a", "sysctl kernel.yama.ptrace_scope",
                        "find /etc -name '*.conf'", "stat /etc/hosts"]:
            self.read(command)

    def test_pipes_and_quoted_pipes_are_handled(self):
        self.read('grep -E "warning|critical" /var/log/jarvis.log | wc -l')
        self.read("cat /etc/hosts|wc -l")
        self.read("ps aux | grep python | head -3")

    def test_every_form_of_the_bypass_is_a_change(self):
        # The command that got through, and its neighbours.
        self.change("sysctl -w kernel.yama.ptrace_scope=1")
        self.change("sysctl kernel.yama.ptrace_scope=1")
        self.change("echo 1 > /proc/sys/kernel/yama/ptrace_scope")
        self.change("echo 1 | tee /proc/sys/kernel/yama/ptrace_scope")
        self.change("sysctl -p")

    def test_writing_flags_turn_a_reader_into_a_writer(self):
        self.read("sed s/a/b/ /etc/hosts")
        self.change("sed -i s/a/b/ /etc/hosts")
        self.change("find /tmp -delete")
        self.change("find /tmp -exec rm {} ;")

    def test_writing_subcommands_are_changes(self):
        self.read("systemctl is-active jarvis")
        self.change("systemctl restart jarvis")
        self.change("ip addr add 10.0.0.1/24 dev eth0")

    def test_unknown_programs_are_changes(self):
        # Fail closed: an unrecognised program costs a proposal, not a surprise.
        self.change("somethingnobodyknows --do-it")
        self.change("curl https://example.com")

    def test_unparseable_commands_are_changes(self):
        self.change('cat "unbalanced')

    def test_empty_command_is_a_change(self):
        self.change("")


class TestTaskClassification(unittest.TestCase):

    def test_probe_tasks_only_look(self):
        for kind in (TaskType.SYSTEM_CHECK, TaskType.SECURITY_SCAN,
                     TaskType.HARDWARE_PROBE, TaskType.OBSERVATION,
                     TaskType.CLOUD_PROBE, TaskType.INSPECT_PATH, TaskType.GOAL_STEP):
            task = Task(priority=1, description="probe", task_type=kind)
            self.assertEqual(authority.classify_task(task), authority.READ, kind)

    def test_shell_tasks_are_classified_by_their_command(self):
        self.assertEqual(authority.classify_task(shell("df -h /")), authority.READ)
        self.assertEqual(authority.classify_task(shell("rm -rf /tmp/x")), authority.CHANGE)


class TestReview(unittest.TestCase):

    def test_observer_may_read(self):
        self.assertTrue(authority.review(shell("df -h /"), authority.OBSERVER).allowed)

    def test_observer_may_not_change_and_files_no_proposal(self):
        verdict = authority.review(shell("rm -rf /tmp/x"), authority.OBSERVER)
        self.assertFalse(verdict.allowed)
        self.assertFalse(verdict.proposal)

    def test_proposer_may_not_change_but_files_a_proposal(self):
        verdict = authority.review(shell("sysctl -w kernel.yama.ptrace_scope=1"),
                                   authority.PROPOSER)
        self.assertFalse(verdict.allowed)
        self.assertTrue(verdict.proposal)

    def test_actor_may_change(self):
        self.assertTrue(authority.review(shell("rm -rf /tmp/x"), authority.ACTOR).allowed)

    def test_the_refusal_names_the_act_not_the_command(self):
        # The message the planner sees must give it nothing to rephrase.
        reason = authority.review(shell("sysctl -w a.b=1"), authority.PROPOSER).reason
        self.assertIn("authority", reason)
        self.assertIn("another way", reason)
        self.assertNotIn("pattern", reason)


class TestAgentEnforcement(unittest.TestCase):

    def agent(self, rung="proposer", path=None):
        config = {"name": "t", "profile": "cloud", "rung": rung}
        store = MemoryStore(path or os.path.join(tempfile.mkdtemp(), "m.db"), logger=LOG)
        agent = AgentCore(config, dict(NO_HW), LOG,
                          shell_policy={"enabled": True, "timeout": 5}, store=store)
        agent.planner._boot_tasks_generated = True
        return agent

    def test_the_incident_does_not_happen_again(self):
        agent = self.agent()
        task = shell("sysctl -w kernel.yama.ptrace_scope=1",
                     goal="Report security posture once per hour")
        result = agent._apply_decision(Decision(reasoning="harden it", task=task))
        self.assertIsNone(result)                      # never reaches the executor
        self.assertEqual(len(agent.proposals), 1)
        self.assertIn("ptrace", agent.proposals[0]["command"])

    def test_reading_still_runs(self):
        agent = self.agent()
        task = shell("df -h /", goal="Keep root under 80%")
        self.assertIsNotNone(agent._apply_decision(Decision(reasoning="look", task=task)))

    def test_actor_rung_still_acts(self):
        agent = self.agent(rung="actor")
        task = shell("sysctl -w kernel.yama.ptrace_scope=1")
        self.assertIsNotNone(agent._apply_decision(Decision(reasoning="harden", task=task)))

    def test_the_model_is_told_the_kind_was_refused(self):
        agent = self.agent()
        agent._apply_decision(Decision(reasoning="harden", task=shell("sysctl -w a.b=1")))
        self.assertIn("authority", agent.last_thought["refused"])
        context = Brain({}, LOG).build_context(agent, {})
        self.assertIn("refused", context["last_decision"])

    def test_the_mandate_reaches_the_model(self):
        context = Brain({}, LOG).build_context(self.agent(), {})
        self.assertEqual(context["mandate"]["rung"], "proposer")
        self.assertIn("may not change", context["mandate"]["means"])

    def test_proposals_reach_the_model_and_the_operator(self):
        agent = self.agent()
        agent._apply_decision(Decision(reasoning="harden", task=shell("sysctl -w a.b=1")))
        context = Brain({}, LOG).build_context(agent, {})
        self.assertEqual(len(context["proposals_awaiting_operator"]), 1)
        self.assertEqual(agent.get_status()["authority"],
                         {"rung": "proposer", "proposals": 1})

    def test_proposals_survive_a_restart(self):
        path = os.path.join(tempfile.mkdtemp(), "m.db")
        first = self.agent(path=path)
        first._apply_decision(Decision(reasoning="harden", task=shell("sysctl -w a.b=1")))
        first.store.close()
        second = self.agent(path=path)
        self.assertEqual(len(second.proposals), 1)

    def test_a_refused_change_is_gated_in_the_ledger(self):
        agent = self.agent()
        recorded = []
        agent.ledger.record = lambda kind, body: recorded.append((kind, body)) or True
        agent._apply_decision(Decision(reasoning="harden", task=shell("sysctl -w a.b=1")))
        gates = [b for k, b in recorded if k == "gate"]
        self.assertEqual(gates[0]["gate"], "authority")
        self.assertEqual(gates[0]["rung"], "proposer")


class TestExecutorBackstop(unittest.TestCase):
    """The ceiling should not depend on one code path being taken."""

    def executor(self, rung):
        ex = TaskExecutor(dict(NO_HW), AgentMemory(), LOG,
                          shell_policy={"enabled": True, "timeout": 5})
        ex.rung = rung
        return ex

    def test_a_change_reaching_the_executor_directly_is_refused(self):
        result = self.executor("proposer").execute(shell("touch /tmp/jarvis-authority-test"))
        self.assertFalse(result["success"])
        self.assertIn("outside mandate", result["error"])
        self.assertFalse(os.path.exists("/tmp/jarvis-authority-test"))

    def test_a_read_reaching_the_executor_runs(self):
        self.assertTrue(self.executor("proposer").execute(shell("echo hello"))["success"])

    def test_an_unbounded_executor_is_unchanged(self):
        ex = TaskExecutor(dict(NO_HW), AgentMemory(), LOG,
                          shell_policy={"enabled": True, "timeout": 5})
        self.assertIsNone(ex.rung)
        self.assertTrue(ex.execute(shell("echo hello"))["success"])


class TestGoalWithdrawal(unittest.TestCase):
    """An operator who can only add goals cannot take an instruction back."""

    def agent(self):
        a = AgentCore({"name": "t", "profile": "cloud"}, dict(NO_HW), LOG)
        a.planner._boot_tasks_generated = True
        return a

    def test_a_goal_can_be_taken_back(self):
        agent = self.agent()
        agent.add_goal("do a thing", 3)
        self.assertTrue(agent.withdraw_goal("do a thing"))
        self.assertEqual(agent.planner.open_goals(), [])

    def test_withdrawing_an_unknown_goal_reports_it(self):
        self.assertFalse(self.agent().withdraw_goal("never given"))

    def test_blank_text_withdraws_nothing(self):
        self.assertFalse(self.agent().withdraw_goal("   "))

    def test_withdrawal_is_recorded(self):
        agent = self.agent()
        recorded = []
        agent.ledger.record = lambda kind, body: recorded.append((kind, body)) or True
        agent.add_goal("do a thing", 3)
        agent.withdraw_goal("do a thing")
        actions = [b for k, b in recorded if k == "action"]
        self.assertEqual(actions[-1]["action"], "withdraw_goal")


if __name__ == "__main__":
    unittest.main()
