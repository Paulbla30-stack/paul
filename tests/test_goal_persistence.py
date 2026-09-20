"""An instruction has to outlive the process it was given to.

Notes were restored after a restart. Proposals were restored. Goals were not:
they came only from config at boot, and one given through the API went into
volatile working memory and onto the ledger the agent may not read. So an
operator could tell the agent to do something, the box could restart, and the
instruction would be gone -- with no error, no warning, and no trace the agent
could see. That is worse than losing it loudly, because nothing ever asked
where it went.

The tests here are about the restart, the things that must not come back, and
the things that must not be duplicated by coming back.
"""

import logging
import os
import tempfile
import unittest

from jarvis.agent.core import AgentCore
from jarvis.agent.store import build_store

LOG = logging.getLogger("test")
NO_HW = {"display": None, "input": None, "memory": None, "storage": None}


class Base(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "memory.db")
        self.live = []

    def tearDown(self):
        for agent in self.live:
            try:
                agent.store.close()
            except Exception:
                pass

    def agent(self):
        """A fresh process against the same durable memory."""
        a = AgentCore({"name": "t", "profile": "cloud"}, dict(NO_HW), LOG,
                      store=build_store({"path": self.path}, LOG))
        a.planner._boot_tasks_generated = True
        self.live.append(a)
        return a

    def restart(self, agent):
        agent.store.close()
        return self.agent()

    @staticmethod
    def given(agent):
        return sorted(g["description"] for g in agent.planner.goals
                      if not g.get("standing"))


class TestAnInstructionSurvivesARestart(Base):

    def test_a_goal_is_still_there_afterwards(self):
        a = self.agent()
        a.add_goal("Chase the British Gas refund", 1)
        a.add_goal("Book the MOT before November", 3)
        b = self.restart(a)
        self.assertEqual(self.given(b),
                         ["Book the MOT before November",
                          "Chase the British Gas refund"])

    def test_its_priority_survives_too(self):
        """Without somewhere to keep it, a restored goal came back guessed."""
        a = self.agent()
        a.add_goal("Chase the British Gas refund", 1)
        b = self.restart(a)
        goal = next(g for g in b.planner.goals if "refund" in g["description"])
        self.assertEqual(goal["priority"], 1)

    def test_it_survives_more_than_one_restart(self):
        a = self.agent()
        a.add_goal("Chase the British Gas refund", 1)
        for _ in range(3):
            a = self.restart(a)
        self.assertEqual(self.given(a), ["Chase the British Gas refund"])

    def test_a_goal_is_pinned_so_the_row_cap_cannot_prune_it(self):
        """A goal the cap can evict is a goal that quietly stops existing."""
        a = self.agent()
        a.add_goal("Chase the British Gas refund", 1)
        row = a.store.recent(1, kind="goal")[0]
        self.assertTrue(row["pinned"])


class TestWhatMustNotComeBack(Base):

    def test_a_completed_goal_stays_completed(self):
        a = self.agent()
        a.add_goal("Chase the British Gas refund", 1)
        a.complete_goal("Chase the British Gas refund")
        self.assertEqual(self.given(self.restart(a)), [])

    def test_a_withdrawn_goal_stays_withdrawn(self):
        a = self.agent()
        a.add_goal("Book the MOT before November", 3)
        a.withdraw_goal("Book the MOT before November")
        self.assertEqual(self.given(self.restart(a)), [])

    def test_withdrawing_one_does_not_create_another(self):
        """It did. The note about the withdrawal was stored with kind="goal",
        which was harmless while nothing read goals back, and became a real
        bug the moment they were restored: taking an instruction back made a
        new goal named "Operator withdrew the goal: ..."."""
        a = self.agent()
        a.add_goal("Book the MOT before November", 3)
        a.withdraw_goal("Book the MOT before November")
        for description in self.given(self.restart(a)):
            self.assertNotIn("withdrew", description.lower())

    def test_the_agent_is_told_a_goal_was_withdrawn(self):
        a = self.agent()
        a.add_goal("Book the MOT before November", 3)
        a.withdraw_goal("Book the MOT before November")
        self.assertTrue(any("withdrew" in n for n in a.notes))

    def test_a_retired_goal_is_superseded_rather_than_deleted(self):
        """The record of having been asked survives the asking."""
        a = self.agent()
        a.add_goal("Chase the British Gas refund", 1)
        a.complete_goal("Chase the British Gas refund")
        rows = a.store.recent(5, kind="goal")
        self.assertTrue(rows)
        self.assertEqual(rows[0]["state"], "superseded")


class TestRestoringCannotDuplicate(Base):
    """main.py re-adds the configured goals after construction, every boot."""

    def test_the_config_pass_does_not_double_a_restored_goal(self):
        a = self.agent()
        a.add_goal("Book the MOT before November", 3)
        b = self.restart(a)
        for _ in range(3):
            b.planner.add_goal("Book the MOT before November", 3)
        self.assertEqual(len(b.planner.goals), 1)

    def test_standing_goals_are_not_restored_as_operator_goals(self):
        """A standing goal comes from config and is re-added there. Restoring
        it here as well would make it operator-given, which it was not."""
        a = self.agent()
        a.planner.add_goal("Keep the root filesystem under 80% used", 2,
                           standing=True)
        b = self.restart(a)
        self.assertEqual(self.given(b), [])

    def test_restating_a_goal_sharpens_its_priority(self):
        a = self.agent()
        a.add_goal("Chase the British Gas refund", 5)
        a.add_goal("Chase the British Gas refund", 1)
        self.assertEqual(len(a.planner.goals), 1)
        self.assertEqual(a.planner.goals[0]["priority"], 1)
        self.assertEqual(self.restart(a).planner.goals[0]["priority"], 1)


class TestWithoutDurableMemory(unittest.TestCase):
    """A NullStore takes an instruction and returns None without complaint."""

    def test_the_operator_is_warned_rather_than_left_to_find_out(self):
        agent = AgentCore({"name": "t", "profile": "cloud"}, dict(NO_HW), LOG)
        agent.planner._boot_tasks_generated = True
        with self.assertLogs(LOG, level="WARNING") as caught:
            agent.add_goal("Chase the British Gas refund", 1)
        self.assertTrue(any("will not survive a restart" in line
                            for line in caught.output))

    def test_the_goal_still_works_for_this_process(self):
        agent = AgentCore({"name": "t", "profile": "cloud"}, dict(NO_HW), LOG)
        agent.planner._boot_tasks_generated = True
        agent.add_goal("Chase the British Gas refund", 1)
        self.assertEqual(len(agent.planner.goals), 1)


if __name__ == "__main__":
    unittest.main()
