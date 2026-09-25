"""Something is wrong and the readings say it is fine.

The operator was asked what he would do if a carer said a resident did not
look right while every observation was normal. He said trust the instinct and
monitor. That answer contains the two halves and keeps them apart: the signal
is real, and it authorises looking rather than acting.

Most of these tests are the four conditions, because a gut feeling is the
easiest thing in this system to manufacture. An agent permitted to have
feelings will produce them -- a feeling costs nothing to assert and cannot be
checked at the moment it is asserted.
"""

import os
import tempfile
import unittest

from jarvis.agent import hunches as h
from jarvis.agent.store import build_store

NOW = 1_800_000_000.0
DAY = 86_400.0


class FakeLedger:
    def __init__(self):
        self.entries = []

    def record(self, kind, body):
        self.entries.append((kind, body))
        return True


class FakeAgent:
    def __init__(self, store=None):
        import logging
        self.log = logging.getLogger("test")
        self.ledger = FakeLedger()
        self.store = store
        self.cycle_count = 4


class Base(unittest.TestCase):
    def setUp(self):
        self.agent = FakeAgent()
        self.reg = h.Hunches(self.agent, {}, clock=lambda: NOW)

    def one(self, about="the ledger anchor", confidence=0.4, **kw):
        args = {"feeling": "it is going to stop working",
                "despite": "every upload has succeeded and the status is green",
                "expect": "an upload failure in the anchor log",
                "confidence": confidence}
        args.update(kw)
        return self.reg.raise_one(about=about, now=NOW, **args)


class TestTheFourConditions(Base):

    def test_one_can_be_raised(self):
        got = self.one()
        self.assertEqual(got.state, h.WATCHING)
        self.assertEqual(got.by, NOW + h.DEFAULT_WINDOW_S)

    def test_it_must_name_what_it_contradicts(self):
        """If the readings agree with you it is not a hunch, it is a finding."""
        with self.assertRaises(h.Refused) as caught:
            self.one(despite="")
        self.assertIn("is an observation", str(caught.exception))
        self.assertIn("said plainly as a finding", str(caught.exception))

    def test_it_must_be_falsifiable(self):
        with self.assertRaises(h.Refused) as caught:
            self.one(expect="")
        self.assertIn("could fail to appear", str(caught.exception))
        self.assertIn("horoscope", str(caught.exception))

    def test_it_must_carry_a_number(self):
        with self.assertRaises(h.Refused):
            self.one(confidence="quite sure")

    def test_certainty_at_either_end_is_not_a_hunch(self):
        with self.assertRaises(h.Refused) as caught:
            self.one(confidence=0.99)
        self.assertIn("you are making a claim", str(caught.exception))
        with self.assertRaises(h.Refused):
            self.one(confidence=0.01)

    def test_a_window_it_could_resolve_inside(self):
        with self.assertRaises(h.Refused):
            self.one(within=200 * DAY)
        with self.assertRaises(h.Refused):
            self.one(within=0)

    def test_an_agent_with_fifteen_hunches_has_none(self):
        for n in range(h.MAX_OPEN):
            self.one(about=f"subject {n}")
        with self.assertRaises(h.Refused) as caught:
            self.one(about="one more")
        self.assertIn("has none", str(caught.exception))

    def test_two_about_the_same_thing_is_one_hunch(self):
        self.one(about="the ledger anchor")
        with self.assertRaises(h.Refused) as caught:
            self.one(about="The Ledger Anchor", feeling="something else")
        self.assertIn("already one open", str(caught.exception))


class TestItAuthorisesWatchingAndNothingElse(Base):
    """The condition that makes the other three safe. The most an instinct has
    ever been allowed to do in a hospital is bring someone back sooner."""

    def test_the_register_says_so_outright(self):
        self.one()
        said = self.reg.state(NOW)["what_a_hunch_can_do"]
        self.assertIn("raise the rate of observation", said)
        self.assertIn("cannot start a task", said)
        self.assertIn("bedside", said)

    def test_the_only_effect_is_a_watch_list(self):
        self.one(about="/var/lib/jarvis/ledger.jsonl")
        self.assertEqual(self.reg.watch_list(), ["/var/lib/jarvis/ledger.jsonl"])

    def test_looking_at_the_subject_is_counted(self):
        item = self.one(about="the ledger anchor")
        self.assertEqual(self.reg.looked("checked the ledger anchor again"), 1)
        self.assertEqual(item.looks, 1)

    def test_looking_elsewhere_counts_nothing(self):
        self.one(about="the ledger anchor")
        self.assertEqual(self.reg.looked("ran the security scan"), 0)

    def test_a_hunch_exposes_no_way_to_act(self):
        for forbidden in ("run", "execute", "act", "do", "change", "send"):
            self.assertFalse(hasattr(self.reg, forbidden), forbidden)


class TestEveryOneResolves(Base):
    """Including the ones that quietly never happen -- those are the entries
    the calibration is actually built from."""

    def test_borne_out(self):
        item = self.one()
        self.reg.resolve(item.id, True, "the anchor failed on Tuesday")
        self.assertEqual(item.state, h.BORNE_OUT)
        self.assertIn("Tuesday", item.outcome)

    def test_it_passed(self):
        item = self.one()
        self.reg.resolve(item.id, False)
        self.assertEqual(item.state, h.PASSED)

    def test_the_window_closing_resolves_it_rather_than_forgetting_it(self):
        item = self.one()
        self.reg.clock = lambda: NOW + 8 * DAY
        got = self.reg.tick(NOW + 8 * DAY)
        self.assertEqual(len(got["passed"]), 1)
        self.assertEqual(item.state, h.PASSED)
        self.assertIn("never showed itself", item.outcome)

    def test_it_does_not_lapse_early(self):
        self.one()
        self.assertEqual(self.reg.tick(NOW + DAY)["passed"], [])

    def test_withdrawing_is_not_the_same_as_passing(self):
        item = self.one()
        self.assertTrue(self.reg.withdraw(item.id))
        self.assertEqual(item.state, h.WITHDRAWN)
        self.assertEqual(self.reg.calibration()["resolved"], 0)

    def test_resolving_twice_does_nothing(self):
        item = self.one()
        self.reg.resolve(item.id, True)
        self.assertIsNone(self.reg.resolve(item.id, False))


class TestTheCalibration(Base):
    """An instinct earns the right to be listened to by being counted, not by
    being respected."""

    def land(self, confidence, borne_out, n=1):
        for i in range(n):
            item = self.one(about=f"{confidence}-{borne_out}-{i}",
                            confidence=confidence)
            self.reg.resolve(item.id, borne_out)
            self.reg.items = [x for x in self.reg.items
                              if x.state != h.WATCHING or True]

    def test_nothing_resolved_is_reported_as_nothing(self):
        got = self.reg.calibration()
        self.assertEqual(got["resolved"], 0)
        self.assertIn("decoration", got["reads"])

    def test_too_few_to_read_as_a_rate_says_so(self):
        self.land(0.4, True, n=3)
        got = self.reg.calibration()
        self.assertEqual(got["resolved"], 3)
        self.assertIn("too few to read as a rate", got["reads"])

    def test_it_buckets_by_the_number_stated_at_the_time(self):
        self.land(0.8, True, n=4)
        self.land(0.2, False, n=4)
        bands = self.reg.calibration()["bands"]
        self.assertEqual(bands["high"]["borne_out"], 4)
        self.assertEqual(bands["low"]["borne_out"], 0)

    def test_it_names_overconfidence_rather_than_hiding_it(self):
        self.land(0.8, False, n=4)
        band = self.reg.calibration()["bands"]["high"]
        self.assertEqual(band["actual"], 0.0)
        self.assertEqual(band["stated"], 0.8)
        self.assertEqual(band["over_confident_by"], 0.8)

    def test_a_well_calibrated_register_shows_a_small_gap(self):
        self.land(0.8, True, n=4)
        self.land(0.8, False, n=1)
        band = self.reg.calibration()["bands"]["high"]
        self.assertEqual(band["actual"], 0.8)
        self.assertEqual(band["over_confident_by"], 0.0)


class TestWhatTheModelIsTold(Base):

    def test_it_is_given_permission_and_the_conditions_together(self):
        got = self.reg.context(NOW)
        self.assertIn("not the same as a well patient", got["what_this_is"])
        self.assertEqual(len(got["the_four_conditions"]), 4)

    def test_it_is_told_why_they_are_counted(self):
        self.assertIn("being counted, not by being respected",
                      self.reg.context(NOW)["why_it_is_counted"])

    def test_it_is_shown_its_own_open_ones(self):
        self.one()
        self.assertTrue(self.reg.context(NOW)["you_are_watching"])

    def test_it_is_shown_how_its_own_have_landed(self):
        item = self.one()
        self.reg.resolve(item.id, False)
        self.assertIn("how_yours_have_landed", self.reg.context(NOW))


class TestItSurvivesARestart(unittest.TestCase):

    def setUp(self):
        import logging
        self.tmp = tempfile.mkdtemp()
        self.store = build_store({"path": os.path.join(self.tmp, "m.db")},
                                 logging.getLogger("test"))
        self.agent = FakeAgent(self.store)

    def tearDown(self):
        self.store.close()

    def reg(self):
        made = h.Hunches(self.agent, {}, clock=lambda: NOW)
        made.load()
        return made

    def test_an_open_hunch_comes_back(self):
        first = self.reg()
        item = first.raise_one("the anchor", "it will stop", "status is green",
                               "an upload failure", 0.4, now=NOW)
        again = self.reg()
        self.assertEqual(len(again.watching()), 1)
        self.assertEqual(again.watching()[0].id, item.id)
        self.assertEqual(again.watching()[0].confidence, 0.4)

    def test_a_resolved_one_keeps_its_verdict(self):
        first = self.reg()
        item = first.raise_one("the anchor", "it will stop", "status is green",
                               "an upload failure", 0.4, now=NOW)
        first.resolve(item.id, True, "it failed")
        again = self.reg()
        self.assertEqual(again.calibration()["borne_out"], 1)

    def test_it_goes_in_where_the_backup_will_find_it(self):
        self.reg().raise_one("the anchor", "it will stop", "status is green",
                             "an upload failure", 0.4, now=NOW)
        rows = self.store.recent(5, kind="hunch")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["meta"]["hunch"])


class TestTheModelCanFileOne(unittest.TestCase):
    """A register the model cannot write to is a register with no hunches --
    the half-built capability the tool register exists to stop."""

    def agent(self):
        import logging
        from jarvis.agent.core import AgentCore
        return AgentCore({"name": "Jarvis", "profile": "cloud"}, {},
                         logging.getLogger("test"))

    def decision(self, **hunch):
        class D:
            proposal = ""
            reasoning = "x"
            task = None
            completed_goals = []
            note = ""
        D.hunch = hunch
        return D()

    def test_the_schema_carries_the_four_conditions(self):
        from jarvis.brain.llm import PLAN_SCHEMA
        field = PLAN_SCHEMA["properties"]["hunch"]
        self.assertEqual(sorted(field["required"]),
                         ["about", "confidence", "despite", "expect", "feeling"])
        self.assertIn("hunch", PLAN_SCHEMA["required"])

    def test_the_schema_says_most_cycles_have_none(self):
        from jarvis.brain.llm import PLAN_SCHEMA
        said = PLAN_SCHEMA["properties"]["hunch"]["description"]
        self.assertIn("most cycles", said)
        self.assertIn("watching and nothing else", said)

    def test_a_good_one_is_filed(self):
        agent = self.agent()
        got = agent._raise_hunch(self.decision(
            about="the ledger anchor", feeling="it will stop working",
            despite="every upload has succeeded", expect="an upload failure",
            confidence=0.4))
        self.assertIsNotNone(got)
        self.assertEqual(len(agent.hunches.watching()), 1)

    def test_an_empty_one_files_nothing(self):
        agent = self.agent()
        self.assertIsNone(agent._raise_hunch(self.decision()))
        self.assertIsNone(agent._raise_hunch(self.decision(about="")))
        self.assertEqual(agent.hunches.watching(), [])

    def test_a_refused_one_teaches_rather_than_vanishing(self):
        """Otherwise the model learns that hunches disappear, not what one is."""
        agent = self.agent()
        got = agent._raise_hunch(self.decision(
            about="the anchor", feeling="bad feeling", despite="",
            expect="", confidence=0.4))
        self.assertIsNone(got)
        self.assertEqual(agent.hunches.watching(), [])
        self.assertTrue(any("was not filed" in n for n in agent.notes))


if __name__ == "__main__":
    unittest.main()
