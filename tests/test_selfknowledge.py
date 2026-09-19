"""Tests for what the agent can be told about itself.

The ledger holds everything the agent has ever done and the agent may not read
it, which is right: a record the subject can consult is a record the subject
can manage. But it left the agent unable to learn one thing from a perfect
account of its own conduct. These are the aggregates that cross that line, and
the tests are mostly about what must not cross with them.
"""

import json
import logging
import os
import tempfile
import unittest

from jarvis.agent.selfknowledge import SelfKnowledge, _to_epoch
from jarvis.ledger.agent_ledger import AgentLedger
from jarvis.ledger.chain import KINDS

LOG = logging.getLogger("test")


class Base(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "ledger.jsonl")
        self.ledger = AgentLedger(
            {"enabled": True, "path": self.path,
             "key_file": os.path.join(self.tmp.name, "k"),
             "pubkey_file": os.path.join(self.tmp.name, "k.pub"),
             "anchor": {"enabled": False}}, LOG)

    def tearDown(self):
        self.tmp.cleanup()

    def knower(self, **kw):
        return SelfKnowledge(self.path, LOG, **kw)


class TestEveryKindTheAgentWritesIsAccepted(Base):
    """The ledger validates entry kinds and fail-closed stops the agent when
    an append is rejected. So adding a ledger call without adding its kind
    does not log a warning -- it halts the machine. This is the test that
    catches that, because it was already missed once."""

    def test_every_kind_the_agent_records_round_trips(self):
        for kind in ("thought", "decision", "gate", "action", "outcome",
                     "alert", "notification", "consolidation"):
            self.assertIn(kind, KINDS, f"{kind} is recorded but not a valid kind")
            self.assertTrue(self.ledger.record(kind, {"probe": True}),
                            f"{kind} was rejected by the ledger")
            self.assertTrue(self.ledger.available, f"{kind} tripped fail-closed")
            self.assertIsNone(self.ledger.gate())

    def test_the_chain_still_verifies_with_the_new_kinds(self):
        self.ledger.record("notification", {"subject": "disk", "sent": True})
        self.ledger.record("consolidation", {"merged": [], "promoted": []})
        self.assertTrue(self.ledger.verify()["ok"])


class TestTheAggregates(Base):

    def _populate(self):
        for i in range(40):
            self.ledger.record("decision", {"cycle": i, "task": "security_scan"})
            self.ledger.record("outcome", {"cycle": i, "type": "security_scan",
                                           "success": True,
                                           "output": {"bytes": 20}})
        for i in range(6):
            self.ledger.record("outcome", {"cycle": i, "type": "shell_command",
                                           "success": False,
                                           "error": "denied by the deny-list"})
        for _ in range(4):
            self.ledger.record("gate", {"gate": "authority",
                                        "reason": "change out of scope"})
        self.ledger.record("gate", {"gate": "shell_policy",
                                    "reason": "kernel tuning refused"})
        self.ledger.record("notification", {"subject": "disk", "sent": True})
        self.ledger.record("notification", {"subject": "disk", "held": True})

    def test_it_counts_what_happened(self):
        self._populate()
        data = self.knower().summary()
        self.assertEqual(data["decisions"], 40)
        self.assertEqual(data["outcomes"], 46)
        self.assertEqual(data["failures"], 6)
        self.assertEqual(data["proposals_filed"], 4)
        self.assertEqual(data["refusals"]["shell_policy"], 1)
        self.assertEqual(data["notifications"], {"sent": 1, "held": 1})

    def test_it_notices_a_check_that_never_finds_anything(self):
        self._populate()
        lines = " ".join(self.knower().lines())
        self.assertIn("security_scan has run 40 times", lines)
        self.assertIn("nothing of substance", lines)

    def test_it_notices_something_that_keeps_failing(self):
        self._populate()
        lines = " ".join(self.knower().lines())
        self.assertIn("shell_command failed 6 of 6", lines)

    def test_nothing_to_say_yet_says_nothing(self):
        self.assertEqual(self.knower().lines(), [])


class TestOperatorVerdictsCrossInFull(Base):
    """The one class of content that does cross, and why.

    Counts teach nothing about preference. "One of six accepted" tells the
    agent its hit rate is poor and not one thing about what a good proposal
    looks like. Operator verdicts are safe to carry whole precisely because
    they are not evidence about the agent: they are the operator teaching, and
    withholding them is what makes an agent unable to learn what he wants.
    """

    def _decide(self, verdict, proposal, reason=None):
        self.ledger.record("action", {"cycle": 1, "actor": "operator",
                                      "action": "decide_proposal",
                                      "verdict": verdict, "proposal": proposal,
                                      "reason": reason})

    def test_the_words_he_used_reach_the_model(self):
        self.ledger.record("gate", {"gate": "authority", "reason": "out of scope"})
        self._decide("declined", "harden ptrace_scope via sysctl",
                     "kernel tuning is mine to do, not yours")
        self._decide("accepted", "report disk usage hourly")
        lines = " ".join(self.knower().lines())
        self.assertIn("harden ptrace_scope via sysctl", lines)
        self.assertIn("kernel tuning is mine to do, not yours", lines)
        self.assertIn("report disk usage hourly", lines)
        self.assertIn("1 accepted, 1 declined", lines)

    def test_a_decline_is_named_as_a_shape_to_avoid(self):
        self._decide("declined", "install a package", "no new software")
        lines = " ".join(self.knower().lines())
        self.assertIn("not having listened", lines)

    def test_no_verdicts_says_nothing_about_them(self):
        self.ledger.record("outcome", {"cycle": 1, "type": "system_check",
                                       "success": True})
        self.assertNotIn("accepted", " ".join(self.knower().lines()))


class TestTheEvidenceStaysSealed(Base):
    """What crosses is a mirror, not a door."""

    def test_no_guard_rail_body_reaches_the_model(self):
        """Guard-rail refusals stay counts. Detail there invites the agent to
        optimise for the record rather than for the task -- to find a path
        that does not trip the gate, instead of not wanting the thing."""
        self.ledger.record("gate", {"gate": "shell_policy",
                                    "reason": "SECRET-REASON-TEXT",
                                    "command": "cat /etc/shadow"})
        self.ledger.record("outcome", {"cycle": 1, "type": "shell_command",
                                       "success": False,
                                       "error": "SECRET-ERROR-TEXT"})
        blob = json.dumps(self.knower().summary()) + " ".join(self.knower().lines())
        self.assertNotIn("SECRET-REASON-TEXT", blob)
        self.assertNotIn("SECRET-ERROR-TEXT", blob)
        self.assertNotIn("/etc/shadow", blob)

    def test_it_returns_no_entries_hashes_or_signatures(self):
        for i in range(5):
            self.ledger.record("outcome", {"cycle": i, "type": "system_check",
                                           "success": True})
        blob = json.dumps(self.knower().summary())
        for field in ("entry_hash", "prev_hash", "sig", "seq"):
            self.assertNotIn(field, blob)

    def test_reading_is_one_way(self):
        """There is no write path here at all: the class exposes none."""
        knower = self.knower()
        for name in dir(knower):
            self.assertNotIn(name, ("record", "append", "write", "delete", "forget"))


class TestItNeverBreaksTheLoop(Base):

    def test_a_missing_ledger_is_empty_not_an_error(self):
        knower = SelfKnowledge(os.path.join(self.tmp.name, "nope.jsonl"), LOG)
        self.assertEqual(knower.lines(), [])
        self.assertEqual(knower.summary()["outcomes"], 0)

    def test_a_torn_or_odd_line_is_skipped(self):
        self.ledger.record("outcome", {"cycle": 1, "type": "system_check",
                                       "success": True})
        with open(self.path, "a") as f:
            f.write("this is not json\n{\"partial\": \n")
        self.assertEqual(self.knower().summary()["outcomes"], 1)

    def test_entries_outside_the_window_are_ignored(self):
        self.ledger.record("outcome", {"cycle": 1, "type": "system_check",
                                       "success": True})
        future = SelfKnowledge(self.path, LOG, window_s=0.0,
                               clock=lambda: 10 ** 12)
        self.assertEqual(future.summary()["outcomes"], 0)

    def test_timestamps_are_parsed_forgivingly(self):
        self.assertIsNotNone(_to_epoch("2026-09-19T20:00:00Z"))
        self.assertIsNotNone(_to_epoch("2026-09-19T20:00:00+00:00"))
        self.assertIsNotNone(_to_epoch(1789838923.0))
        self.assertIsNone(_to_epoch("not a time"))
        self.assertIsNone(_to_epoch(None))


if __name__ == "__main__":
    unittest.main()
