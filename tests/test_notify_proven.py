"""Configured is not proven.

This channel ran for days reporting itself ready: SES verified, settings
right, status green, and not one message ever sent through it. The first real
use would have been a 3am alert, which is the worst possible moment to find
out that a destination has a typo in it or an IAM action is missing.

So there are two questions and they get two fields. ``configured`` asks
whether the settings are there. ``proven`` asks whether anything has ever come
out of the far end. Only one of them is worth anything at 3am, and it is not
the one that was being reported.
"""

import json
import logging
import os
import tempfile
import unittest

from jarvis.agent.notify import Notifier

LOG = logging.getLogger("test")
NOW = 1_800_000_000.0


def make(tmp, **cfg):
    settings = {"enabled": True, "channel": "ses_email",
                "destination": "paul@example.com",
                "state_file": os.path.join(tmp, "notify.state"),
                "destination_file": os.path.join(tmp, "absent.dest"),
                "quiet_hours": None}
    settings.update(cfg)
    sent = []
    n = Notifier(settings, LOG, backend=lambda d, t: sent.append((d, t)),
                 clock=lambda: NOW)
    return n, sent


class TestConfiguredIsNotProven(unittest.TestCase):

    def test_a_fresh_channel_is_configured_and_unproven(self):
        with tempfile.TemporaryDirectory() as tmp:
            n, _ = make(tmp)
            self.assertTrue(n.configured)
            self.assertFalse(n.proven)
            self.assertFalse(n.status()["proven"])

    def test_a_delivered_message_proves_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            n, sent = make(tmp)
            self.assertTrue(n.send("disk filling", "root at 91%")["sent"])
            self.assertTrue(n.proven)
            self.assertEqual(n.status()["delivered_ever"], 1)
            self.assertEqual(len(sent), 1)

    def test_a_held_message_proves_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            n, sent = make(tmp, min_severity="alert")
            self.assertTrue(n.send("something", severity="notice")["held"])
            self.assertFalse(n.proven)
            self.assertEqual(sent, [])

    def test_a_failed_send_proves_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            n, _ = make(tmp)
            n._backend = lambda d, t: (_ for _ in ()).throw(RuntimeError("no route"))
            self.assertFalse(n.send("x", severity="alert")["sent"])
            self.assertFalse(n.proven)
            self.assertIn("no route", n.last_error)

    def test_proof_survives_a_restart(self):
        """In-process counters reset on every deploy. This must not."""
        with tempfile.TemporaryDirectory() as tmp:
            n, _ = make(tmp)
            n.send("first", severity="alert")
            again, _ = make(tmp)
            self.assertTrue(again.proven)
            self.assertEqual(again.stats["sent"], 0)      # the counter did reset
            self.assertEqual(again.delivered, 1)          # the fact did not

    def test_an_unwritable_state_file_costs_a_field_not_a_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            n, sent = make(tmp, state_file="/proc/nowhere/notify.state")
            self.assertTrue(n.send("x", severity="alert")["sent"])
            self.assertEqual(len(sent), 1)

    def test_a_corrupt_state_file_reads_as_unproven(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "notify.state")
            with open(path, "w") as f:
                f.write("{not json")
            n, _ = make(tmp, state_file=path)
            self.assertFalse(n.proven)


class TestRingingTheBell(unittest.TestCase):

    def test_a_test_goes_out_through_quiet_hours_and_the_floor(self):
        """A test swallowed by policy answers nothing about the transport."""
        with tempfile.TemporaryDirectory() as tmp:
            n, sent = make(tmp, min_severity="alert", quiet_hours=[0, 23],
                           max_per_hour=0, min_gap_s=99999)
            result = n.prove()
            self.assertTrue(result["sent"])
            self.assertTrue(n.proven)
            self.assertEqual(len(sent), 1)

    def test_it_says_in_its_own_words_that_it_is_a_test(self):
        """A proof mistaken for the agent speaking makes the next real one noise."""
        with tempfile.TemporaryDirectory() as tmp:
            n, sent = make(tmp)
            n.prove()
            self.assertIn("test", sent[0][1].lower())
            self.assertIn("not the agent reporting", sent[0][1])

    def test_an_operator_note_replaces_the_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            n, sent = make(tmp)
            n.prove("checking this after the sender id registration")
            self.assertIn("sender id registration", sent[0][1])

    def test_a_disabled_channel_stays_disabled_even_for_a_test(self):
        """Or the test is measuring something that is not the channel."""
        with tempfile.TemporaryDirectory() as tmp:
            n, sent = make(tmp, enabled=False)
            self.assertFalse(n.prove()["sent"])
            self.assertEqual(sent, [])
            self.assertFalse(n.proven)

    def test_a_channel_with_no_destination_cannot_be_proved(self):
        with tempfile.TemporaryDirectory() as tmp:
            n, _ = make(tmp, destination="")
            self.assertIn("destination", n.prove()["reason"])

    def test_a_test_does_not_spend_the_agents_message_budget(self):
        """The next real message should not be held because of a test."""
        with tempfile.TemporaryDirectory() as tmp:
            n, sent = make(tmp, max_per_hour=1, min_gap_s=0)
            n.prove()
            self.assertTrue(n.send("a real one", severity="alert")["sent"])
            self.assertEqual(len(sent), 2)

    def test_a_failing_transport_is_reported_rather_than_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            n, _ = make(tmp)
            n._backend = lambda d, t: (_ for _ in ()).throw(RuntimeError("AccessDenied"))
            result = n.prove()
            self.assertFalse(result["sent"])
            self.assertIn("AccessDenied", result["reason"])
            self.assertFalse(n.proven)

    def test_the_destination_is_hinted_not_disclosed(self):
        with tempfile.TemporaryDirectory() as tmp:
            n, _ = make(tmp)
            self.assertNotIn("paul@example.com", json.dumps(n.prove()))


if __name__ == "__main__":
    unittest.main()
