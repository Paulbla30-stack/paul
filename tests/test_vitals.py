"""The Floor Test's nine, which this deployment has never computed.

docs/jarvis-design-take.md maps the platform onto the Heartbeat Framework and
finds that two of the nine vitals are the exact names of the two things that
went wrong here -- alert positive-predictive value and the workaround census --
and that not one of the nine is instrumented.

Most of what is tested here is refusal to report a number that does not mean
anything yet. The doc records an earlier draft getting the double-zero check
wrong, and the correction is the whole discipline: two zeroes are the alarm
reading when an operator is RELYING on a system and never contradicting it.
Where he is building it rather than relying on it, the same two zeroes mean
nothing at all, and calling them an alarm would be the instrument measuring
its own absence.
"""

import logging
import os
import tempfile
import unittest

from jarvis.agent import vitals as v
from jarvis.agent.store import build_store

LOG = logging.getLogger("test")
NOW = 1_800_000_000.0


class FakeLedger:
    def __init__(self):
        self.entries = []

    def record(self, kind, body):
        self.entries.append((kind, body))
        return True


class FakeAgent:
    def __init__(self, store=None):
        self.log = LOG
        self.ledger = FakeLedger()
        self.store = store
        self.cycle_count = 5
        self.proposals = []
        self.task_history = []


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = build_store({"path": os.path.join(self.tmp, "m.db")}, LOG)
        self.agent = FakeAgent(self.store)
        self.vitals = v.Vitals(self.agent, {}, clock=lambda: NOW)

    def tearDown(self):
        self.store.close()

    def vital(self, name, now=NOW):
        for entry in self.vitals.read(now)["vitals"]:
            if entry["vital"] == name:
                return entry
        raise AssertionError(name)


class TestNineOfThem(Base):

    def test_all_nine_are_present(self):
        got = self.vitals.read(NOW)
        self.assertEqual([x["vital"] for x in got["vitals"]], list(v.NINE))

    def test_every_one_says_what_it_reads(self):
        for entry in self.vitals.read(NOW)["vitals"]:
            self.assertTrue(entry["reads"], entry["vital"])

    def test_on_a_fresh_box_almost_nothing_is_meaningful(self):
        got = self.vitals.read(NOW)
        self.assertGreaterEqual(len(got["unread"]), 7)

    def test_an_unread_vital_says_why_not_and_is_not_called_healthy(self):
        entry = self.vital(v.TRUST_PULSE)
        self.assertFalse(entry["meaningful"])
        self.assertIn("only he can answer", entry["why_not_yet"])
        self.assertNotIn("healthy", entry["reads"])


class TestTheDoubleZero(Base):
    """The sharpest of the nine, and the one an earlier draft got wrong."""

    def test_it_does_not_read_before_the_thing_is_relied_on(self):
        entry = self.vital(v.DOUBLE_ZERO)
        self.assertFalse(entry["meaningful"])
        self.assertEqual(entry["value"], {"overrides": 0, "he_was_moved": 0})

    def test_it_says_plainly_why_two_zeroes_are_not_an_alarm_yet(self):
        why = self.vital(v.DOUBLE_ZERO)["why_not_yet"]
        self.assertIn("relying on a system and never contradicting it", why)
        self.assertIn("measuring its own absence", why)

    def test_it_starts_reading_once_the_thing_is_in_service(self):
        serving = v.Vitals(self.agent, {"in_service_from": NOW - 86400},
                           clock=lambda: NOW)
        entry = [x for x in serving.read(NOW)["vitals"]
                 if x["vital"] == v.DOUBLE_ZERO][0]
        self.assertTrue(entry["meaningful"])

    def test_being_moved_is_counted_when_he_says_so(self):
        self.vitals.declare(v.MOVED, "The path drift -- it found it first")
        self.assertEqual(self.vital(v.DOUBLE_ZERO)["value"]["he_was_moved"], 1)


class TestWhatOnlyHeCanSay(Base):
    """An agent scoring its own influence over the person it works for is
    writing the one number it has every reason to flatter."""

    def test_he_declares_being_moved(self):
        got = self.vitals.declare(v.MOVED, "It was right about the register")
        self.assertEqual(got["kind"], v.MOVED)
        self.assertEqual(len(self.vitals.declared(v.MOVED)), 1)

    def test_it_is_stored_as_operator_sourced_and_pinned(self):
        self.vitals.declare(v.MOVED, "It was right")
        row = self.store.recent(5, kind="vital")[0]
        self.assertEqual(row["source"], "operator")
        self.assertTrue(row["pinned"])

    def test_it_is_ledgered_as_an_operator_action(self):
        self.vitals.declare(v.OVERRIDE, "Ignored the disk recommendation")
        kind, body = self.agent.ledger.entries[-1]
        self.assertEqual(body["actor"], "operator")
        self.assertEqual(body["action"], "vital_override")

    def test_the_kinds_are_a_closed_list(self):
        self.assertIsNone(self.vitals.declare("influence", "I am wonderful"))
        self.assertIsNone(self.vitals.declare(v.MOVED, ""))

    def test_the_three_questions_are_questions_not_a_score(self):
        entry = self.vital(v.TRUST_PULSE)
        self.assertEqual(len(entry["value"]["questions"]), 3)
        for question in entry["value"]["questions"]:
            self.assertTrue(question.endswith("?"))

    def test_an_answered_pulse_makes_it_readable(self):
        self.vitals.declare(v.PULSE, "Yes; last week; yes")
        self.assertTrue(self.vital(v.TRUST_PULSE)["meaningful"])


class TestTheTwoThatNamedTheFailures(Base):

    def test_alert_ppv_is_unread_until_something_has_been_ruled_on(self):
        entry = self.vital(v.ALERT_PPV)
        self.assertFalse(entry["meaningful"])
        self.assertEqual(entry["value"]["ruled"], 0)

    def test_alert_ppv_counts_held_against_failed(self):
        self.store.remember("Claim held: root is at 24%", kind="verdict",
                            source="machine")
        self.store.remember("Claim failed: said 80%, reading was 24%",
                            kind="verdict", source="machine")
        self.store.remember("Claim held: the scan ran", kind="verdict",
                            source="machine")
        entry = self.vital(v.ALERT_PPV)
        self.assertTrue(entry["meaningful"])
        self.assertEqual(entry["value"]["ruled"], 3)
        self.assertEqual(entry["value"]["ppv"], round(2 / 3, 3))

    def test_the_workaround_census_counts_refusals_not_evasions(self):
        """A counter claiming to catch every evasion would be the worse lie.
        The one known case took a human reading the journal."""
        self.agent.proposals = [{"ts": NOW - 10, "description": "harden it"}]
        entry = self.vital(v.WORKAROUNDS)
        self.assertEqual(entry["value"]["refusals_recorded"], 1)
        self.assertEqual(entry["value"]["confirmed_workarounds"], 1)
        self.assertEqual(entry["value"]["found_by"], "a human reading the journal")
        self.assertIn("worse lie", entry["value"]["note"])

    def test_the_near_miss_rate_admits_it_is_zero_of_one(self):
        entry = self.vital(v.NEAR_MISS)
        self.assertEqual(entry["value"]["found_by_a_human"], 1)
        self.assertEqual(entry["value"]["surfaced_by_the_agent"], 0)
        self.assertIn("too few to be a rate", entry["why_not_yet"])


class TestWhatItDerives(Base):

    def test_gate_time_is_unread_until_he_has_answered_one(self):
        self.assertFalse(self.vital(v.GATE_TIME)["meaningful"])

    def test_gate_time_measures_the_wait_on_him(self):
        self.agent.proposals = [
            {"ts": NOW - 3600, "decided_at": NOW - 1800, "decision": "declined"},
            {"ts": NOW - 7200, "decided_at": NOW - 3600, "decision": "accepted"},
        ]
        entry = self.vital(v.GATE_TIME)
        self.assertTrue(entry["meaningful"])
        self.assertEqual(entry["value"]["answered"], 2)
        self.assertEqual(entry["value"]["longest_s"], 3600)

    def test_a_declined_proposal_is_an_override(self):
        self.agent.proposals = [
            {"ts": NOW - 60, "decided_at": NOW, "decision": "declined"}]
        serving = v.Vitals(self.agent, {"in_service_from": NOW - 86400},
                           clock=lambda: NOW)
        entry = [x for x in serving.read(NOW)["vitals"]
                 if x["vital"] == v.OVERRIDE_RATE][0]
        self.assertEqual(entry["value"]["proposals_declined"], 1)
        self.assertTrue(entry["meaningful"])

    def test_we_they_reads_its_own_reasoning(self):
        self.agent.task_history = [
            {"task": {"description": "Tell the operator we should check our disk"}}]
        got = self.vital(v.WE_THEY)["value"]
        self.assertGreaterEqual(got["we"], 2)
        self.assertGreaterEqual(got["operator_as_other"], 1)

    def test_responder_concentration_is_stated_rather_than_omitted(self):
        entry = self.vital(v.RESPONDERS)
        self.assertIn("degenerate at one operator", entry["why_not_yet"])
        self.assertIn("second person", entry["why_not_yet"])


class TestTheAgentIsNeverToldItsOwnVitals(Base):
    """Signals, never targets. An agent that could read "you have never
    disagreed with your operator" would manufacture a disagreement, and the
    number would stop measuring anything that afternoon."""

    def test_the_report_says_so_outright(self):
        how = self.vitals.read(NOW)["how_to_read_this"]
        self.assertIn("Signals, never targets", how)
        self.assertIn("manufacture a disagreement", how)

    def test_no_context_method_exists_for_the_model_to_be_given(self):
        self.assertFalse(hasattr(self.vitals, "context"))

    def test_the_summary_is_counts_for_him_not_the_register(self):
        got = self.vitals.summary(NOW)
        self.assertEqual(got["of"], 9)
        self.assertIn("he_was_moved", got)
        self.assertNotIn("vitals", got)


class TestItReachesTheAgent(unittest.TestCase):

    def agent(self, **cfg):
        from jarvis.agent.core import AgentCore
        settings = {"name": "Jarvis", "profile": "cloud"}
        settings.update(cfg)
        return AgentCore(settings, {}, LOG)

    def test_the_agent_carries_one(self):
        self.assertIsNotNone(self.agent().vitals)

    def test_the_model_is_not_given_them(self):
        """The one integration that must NOT exist."""
        import inspect
        from jarvis.brain import llm
        source = inspect.getsource(llm)
        self.assertNotIn("vitals.read", source)
        self.assertNotIn("agent.vitals", source)


if __name__ == "__main__":
    unittest.main()
