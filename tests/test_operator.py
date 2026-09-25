"""What the agent knows about the person it works for.

The memory design already says an agent writing its own inferences into a
record of a person would destroy the thing that makes such a record worth
keeping -- that it holds what the person actually said. So this profile is
given, not gathered, and the tests are mostly about the three guards that
make that true rather than merely intended: no contact details, never
consolidated, always removable.
"""

import logging
import os
import tempfile
import unittest

from jarvis.agent import operator as op
from jarvis.agent.core import AgentCore
from jarvis.agent.store import build_store

LOG = logging.getLogger("test")
NO_HW = {"display": None, "input": None, "memory": None, "storage": None}


class TestContactDetailsCannotGetIn(unittest.TestCase):
    """Where the agent may reach its operator is settled in config the model
    cannot read. A profile line could undo that by being helpful."""

    def setUp(self):
        self.profile = op.OperatorProfile(name="Paul")

    def test_an_email_address_is_refused(self):
        with self.assertRaises(op.Refused) as why:
            self.profile.add("His email is paul@example.com")
        self.assertIn("email address", str(why.exception))

    def test_a_phone_number_is_refused(self):
        for number in ("+44 7510 504786", "07510504786", "+1 (555) 123-4567"):
            with self.assertRaises(op.Refused):
                self.profile.add(f"Reach him on {number}")

    def test_a_postcode_is_refused(self):
        with self.assertRaises(op.Refused) as why:
            self.profile.add("He lives at SW1A 1AA")
        self.assertIn("postcode", str(why.exception))

    def test_a_card_number_is_refused(self):
        with self.assertRaises(op.Refused):
            self.profile.add("Card 4111 1111 1111 1111")

    def test_a_sort_code_is_refused(self):
        with self.assertRaises(op.Refused):
            self.profile.add("Sort code 12-34-56")

    def test_the_refusal_says_what_to_do_instead(self):
        with self.assertRaises(op.Refused) as why:
            self.profile.add("email paul@example.com")
        self.assertIn("Describe the preference instead", str(why.exception))

    def test_an_ordinary_preference_is_not_caught(self):
        self.profile.add("He would rather be told plainly when something "
                         "cannot be done")
        self.assertEqual(len(self.profile.facts), 1)

    def test_a_date_is_not_mistaken_for_a_sort_code(self):
        """14-10-26 is a date. 12-34-56 is a sort code. Only one is refused."""
        with self.assertRaises(op.Refused):
            self.profile.add("Sort code 12-34-56")
        self.profile.add("He asked about the renewal on 2026-10-14")
        self.assertEqual(len(self.profile.facts), 1)


class TestWhoSaidItSurvives(unittest.TestCase):
    """His words are instruction. Someone's read of him is not."""

    def setUp(self):
        self.profile = op.OperatorProfile(name="Paul")
        self.profile.add("He has said trust is always a two way street",
                         op.STATED, by="paul")
        self.profile.add("He decides quickly and delegates the ordering",
                         op.OBSERVED, by="claude")

    def test_the_two_are_kept_apart_in_context(self):
        got = self.profile.context()
        self.assertEqual(len(got["what_he_has_told_you"]), 1)
        self.assertEqual(len(got["what_others_have_observed"]), 1)

    def test_the_model_is_told_which_one_wins(self):
        how = self.profile.context()["how_to_use_this"]
        self.assertIn("he wins", how)
        self.assertIn("never to be quoted back to him", how)

    def test_the_model_is_told_not_to_add_to_it_by_watching(self):
        how = self.profile.context()["how_to_use_this"]
        self.assertIn("None of this was learned by watching him", how)
        self.assertIn("let him decide", how)

    def test_an_observation_is_labelled_where_it_is_read(self):
        marked = [l for l in self.profile.lines() if "not his words" in l]
        self.assertEqual(len(marked), 1)

    def test_him_saying_it_promotes_an_observation(self):
        self.profile.add("He decides quickly and delegates the ordering",
                         op.STATED, by="paul")
        got = self.profile.context()
        self.assertEqual(len(got["what_he_has_told_you"]), 2)
        self.assertEqual(got["what_others_have_observed"], [])

    def test_an_observation_never_demotes_something_he_said(self):
        self.profile.add("He has said trust is always a two way street",
                         op.OBSERVED, by="claude")
        self.assertIn("He has said trust is always a two way street",
                      self.profile.context()["what_he_has_told_you"])


class TestItIsHisToCorrect(unittest.TestCase):

    def setUp(self):
        self.profile = op.OperatorProfile(name="Paul")
        self.profile.add("He decides quickly", op.OBSERVED, by="claude")

    def test_he_can_read_every_line_held_about_him(self):
        state = self.profile.state()
        self.assertEqual(state["count"], 1)
        self.assertEqual(state["facts"][0]["source"], "observed")
        self.assertEqual(state["facts"][0]["by"], "claude")

    def test_he_can_take_a_line_out(self):
        self.assertTrue(self.profile.forget("He decides quickly"))
        self.assertEqual(self.profile.facts, [])

    def test_a_partial_match_is_enough_to_remove_it(self):
        self.assertTrue(self.profile.forget("decides quickly"))

    def test_removing_something_absent_is_not_a_crash(self):
        self.assertFalse(self.profile.forget("something he never said"))

    def test_the_profile_has_a_ceiling(self):
        profile = op.OperatorProfile(name="Paul")
        for i in range(op.MAX_FACTS):
            profile.add(f"A distinct preference number {i}")
        with self.assertRaises(op.Refused) as why:
            profile.add("One more preference")
        self.assertIn("take one out", str(why.exception))

    def test_an_empty_profile_puts_nothing_in_context(self):
        self.assertIsNone(op.OperatorProfile(name="Paul").context())


class TestItSurvivesARestart(unittest.TestCase):

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "memory.db")
        self.live = []

    def tearDown(self):
        for a in self.live:
            try:
                a.store.close()
            except Exception:
                pass

    def agent(self):
        a = AgentCore({"name": "t", "profile": "cloud", "operator_name": "Paul"},
                      dict(NO_HW), LOG, store=build_store({"path": self.path}, LOG))
        a.planner._boot_tasks_generated = True
        self.live.append(a)
        return a

    def test_the_profile_comes_back(self):
        a = self.agent()
        a.operator.add("He has said trust is always a two way street",
                       op.STATED, by="paul")
        a.operator.add("He decides quickly", op.OBSERVED, by="claude")
        a.store.close()
        b = self.agent()
        got = b.operator.context()
        self.assertIn("He has said trust is always a two way street",
                      got["what_he_has_told_you"])
        self.assertIn("He decides quickly", got["what_others_have_observed"])

    def test_a_removed_line_does_not_come_back(self):
        a = self.agent()
        a.operator.add("He decides quickly", op.OBSERVED, by="claude")
        a.operator.forget("He decides quickly")
        a.store.close()
        self.assertIsNone(self.agent().operator.context())

    def test_it_is_marked_so_consolidation_leaves_it_alone(self):
        """A derived summary of a person is how "he writes quickly" becomes
        "he is careless"."""
        a = self.agent()
        a.operator.add("He writes fast and informally", op.OBSERVED, by="claude")
        row = next(r for r in a.store.recent(10, kind="operator")
                   if (r.get("meta") or {}).get("profile"))
        self.assertTrue(row["meta"]["never_consolidate"])
        self.assertTrue(row["pinned"])

    def test_the_agent_knows_who_it_works_for(self):
        a = self.agent()
        a.operator.add("He is in the UK", op.STATED, by="paul")
        self.assertEqual(a.operator.context()["who"], "Paul")


if __name__ == "__main__":
    unittest.main()
