"""Opening one capability without opening the machine.

Paul wanted the browser to act rather than file a card for every click. The
obvious way to give him that was `rung: actor`, which would also have opened
shell_command and maintenance -- the whole machine -- as the price of being
able to press a button on a web page.

So a grant opens exactly one named capability at the proposer rung. The tests
that matter here are the ones asserting what a grant CANNOT do, because a
grant that can name shell_command is not a grant, it is the actor rung spelled
in a way nobody will notice in review.
"""

import unittest

from jarvis.agent import authority
from jarvis.agent.planner import Task, TaskType


def task(which, **meta):
    return Task(priority=5, description="t", task_type=which, metadata=meta)


class TestWhatAGrantCannotDo(unittest.TestCase):
    """The load-bearing half."""

    def test_shell_command_is_not_grantable(self):
        self.assertNotIn("shell_command", authority.GRANTABLE)

    def test_a_grant_naming_shell_is_discarded_not_honoured(self):
        self.assertEqual(authority.normalise_grants(["shell_command"]), frozenset())

    def test_and_the_spine_still_refuses_that_shell_command(self):
        verdict = authority.review(task(TaskType.SHELL_COMMAND, command="rm -rf /tmp/x"),
                                   "proposer", ["shell_command"])
        self.assertFalse(verdict.allowed)

    def test_maintenance_is_not_grantable_either(self):
        self.assertNotIn("maintenance", authority.GRANTABLE)
        verdict = authority.review(task(TaskType.MAINTENANCE), "proposer",
                                   ["maintenance", "browse_act"])
        self.assertFalse(verdict.allowed)

    def test_nothing_that_changes_this_machine_is_grantable(self):
        """Stated as the rule, so a later addition has to argue with it.

        A capability qualifies only if its blast radius is fenced somewhere
        other than the spine. browse_act qualifies because it drives a separate
        process under a separate uid inside Chromium's sandbox, holding no
        credentials and no route to the metadata service.
        """
        from jarvis.agent import tools
        by_name = {t.name: t for t in tools.TOOLS}
        for name in authority.GRANTABLE:
            self.assertIn(name, by_name, f"{name} is grantable and is not a tool")
            self.assertTrue(name.startswith("browse_"),
                            f"{name} is grantable but does not live behind the "
                            f"browser's own fences; justify it here or drop it")


class TestNormalisingGrants(unittest.TestCase):
    """Same discipline as normalise_rung: exact strings, fails closed."""

    def test_nothing_is_the_empty_set(self):
        for value in (None, (), [], 0, False):
            self.assertEqual(authority.normalise_grants(value), frozenset())

    def test_a_bare_string_does_not_grant_its_letters(self):
        # "browse_act" iterated character by character would grant nothing, but
        # a string that happened to equal a tool name must not grant either:
        # the config shape is a list.
        self.assertEqual(authority.normalise_grants("browse_act"), frozenset())

    def test_whitespace_and_case_do_not_grant(self):
        for spelling in (" browse_act", "browse_act ", "BROWSE_ACT", "Browse_Act",
                         "browse_act\n"):
            self.assertEqual(authority.normalise_grants([spelling]), frozenset(),
                             spelling)

    def test_a_non_string_entry_is_ignored(self):
        self.assertEqual(authority.normalise_grants([None, 1, {"browse_act": True}]),
                         frozenset())

    def test_the_real_spelling_works(self):
        self.assertEqual(authority.normalise_grants(["browse_act"]),
                         frozenset({"browse_act"}))

    def test_unknown_names_are_dropped_and_known_ones_kept(self):
        self.assertEqual(authority.normalise_grants(["browse_act", "fly_to_mars"]),
                         frozenset({"browse_act"}))


class TestWhatAGrantDoes(unittest.TestCase):

    def test_without_it_acting_is_a_proposal(self):
        verdict = authority.review(task(TaskType.BROWSE_ACT, kind="click"), "proposer")
        self.assertFalse(verdict.allowed)
        self.assertTrue(verdict.proposal)

    def test_with_it_acting_is_allowed(self):
        verdict = authority.review(task(TaskType.BROWSE_ACT, kind="click"),
                                   "proposer", ["browse_act"])
        self.assertTrue(verdict.allowed)

    def test_the_reason_says_it_was_the_operator(self):
        verdict = authority.review(task(TaskType.BROWSE_ACT, kind="click"),
                                   "proposer", ["browse_act"])
        self.assertIn("operator", verdict.reason)

    def test_a_grant_does_not_lift_the_observer_rung(self):
        """observer is 'look and report'. A grant is not a way round that."""
        verdict = authority.review(task(TaskType.BROWSE_ACT, kind="click"),
                                   "observer", ["browse_act"])
        self.assertFalse(verdict.allowed)

    def test_reading_needs_no_grant(self):
        verdict = authority.review(task(TaskType.BROWSE_OPEN, url="https://e.test/"),
                                   "proposer")
        self.assertTrue(verdict.allowed)

    def test_moving_about_needs_no_grant(self):
        verdict = authority.review(task(TaskType.BROWSE_MOVE, kind="back"), "proposer")
        self.assertTrue(verdict.allowed)


if __name__ == "__main__":
    unittest.main()
