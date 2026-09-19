"""Tests for the one channel out to the operator.

Everything the agent worked out was stuck inside the box: it noticed the disk,
the scan findings and the failed logins, and could only say so if someone
opened the UI. This is the way out, and the tests here are mostly about the
limits rather than the sending, because the failure that matters is not the
agent going quiet. It is the agent becoming something the operator mutes.
"""

import os
import tempfile
import unittest

from jarvis.agent import authority, notify
from jarvis.agent.executor import check_command_allowed, normalise_shell_policy
from jarvis.agent.planner import Task, TaskType


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class Outbox:
    """A backend that records instead of texting anyone."""

    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def __call__(self, destination, text):
        if self.fail:
            raise RuntimeError("carrier said no")
        self.sent.append((destination, text))


def make(clock=None, outbox=None, **cfg):
    settings = {"enabled": True, "destination": "+447700900123",
                "quiet_hours": None}
    settings.update(cfg)
    return notify.Notifier(settings, backend=outbox or Outbox(),
                           clock=clock or FakeClock())


class TestItSends(unittest.TestCase):

    def test_a_message_goes_out_and_is_composed_for_a_phone(self):
        box = Outbox()
        n = make(outbox=box)
        verdict = n.send("disk at 84%", "root filesystem, threshold 80")
        self.assertTrue(verdict["sent"])
        destination, text = box.sent[0]
        self.assertEqual(destination, "+447700900123")
        self.assertTrue(text.startswith("Jarvis: disk at 84%"))
        self.assertIn("threshold 80", text)

    def test_a_long_body_is_truncated_not_split(self):
        """A text nobody reads to the end is not a message, and three of
        them is worse than one."""
        box = Outbox()
        n = make(outbox=box)
        n.send("something", "word " * 500)
        _, text = box.sent[0]
        self.assertEqual(len(box.sent), 1)
        self.assertLessEqual(len(text), notify.SMS_LIMIT)
        self.assertTrue(text.endswith("…"))

    def test_a_failing_carrier_is_a_verdict_not_an_exception(self):
        n = make(outbox=Outbox(fail=True))
        verdict = n.send("disk at 84%")
        self.assertFalse(verdict["sent"])
        self.assertFalse(verdict["held"])
        self.assertIn("carrier said no", verdict["reason"])
        self.assertEqual(n.stats["failed"], 1)


class TestItHolds(unittest.TestCase):
    """Proportionality is code, not a request. A prompt asking a model to be
    judicious is a hope; a rate limit is a fact."""

    def test_below_the_severity_floor(self):
        n = make(min_severity="alert")
        self.assertFalse(n.send("fyi", severity="notice")["sent"])
        self.assertTrue(n.send("fire", severity="alert")["sent"])

    def test_the_same_subject_twice(self):
        clock = FakeClock()
        n = make(clock=clock, min_gap_s=0)
        self.assertTrue(n.send("disk at 84%")["sent"])
        held = n.send("disk at 84%")
        self.assertIn("already sent", held["reason"])
        clock.advance(notify.DEFAULT_DEDUPE_WINDOW_S + 1)
        self.assertTrue(n.send("disk at 84%")["sent"])

    def test_the_hourly_cap(self):
        clock = FakeClock()
        n = make(clock=clock, max_per_hour=2, min_gap_s=0)
        self.assertTrue(n.send("one")["sent"])
        self.assertTrue(n.send("two")["sent"])
        self.assertIn("hourly cap", n.send("three")["reason"])
        clock.advance(3601)
        self.assertTrue(n.send("four")["sent"])

    def test_the_gap_between_messages(self):
        clock = FakeClock()
        n = make(clock=clock, min_gap_s=900)
        self.assertTrue(n.send("one")["sent"])
        self.assertIn("since the last message", n.send("two")["reason"])
        clock.advance(901)
        self.assertTrue(n.send("three")["sent"])

    def test_quiet_hours_hold_a_notice_and_let_an_alert_through(self):
        """The thing quiet hours must not swallow is the reason you would
        want to be woken."""
        # 02:00 UTC, inside a 22->07 window
        clock = FakeClock(1_700_000_000.0)
        n = notify.Notifier({"enabled": True, "destination": "+447700900123",
                             "quiet_hours": [22, 7], "timezone": "UTC",
                             "min_gap_s": 0},
                            backend=Outbox(), clock=clock)
        # find an hour inside the window to be certain of the assertion
        from datetime import datetime, timezone as _tz
        while datetime.fromtimestamp(clock.t, _tz.utc).hour != 2:
            clock.advance(3600)
        self.assertIn("quiet hours", n.send("routine thing")["reason"])
        self.assertTrue(n.send("the box is on fire", severity="alert")["sent"])

    def test_nothing_configured_holds_everything_and_says_why(self):
        n = notify.Notifier({}, backend=Outbox())
        verdict = n.send("anything")
        self.assertFalse(verdict["sent"])
        self.assertIn("disabled", verdict["reason"])
        self.assertFalse(n.configured)


class TestTheDestinationIsNotTheModelsToChoose(unittest.TestCase):

    def test_it_comes_from_a_file_before_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "notify.dest")
            with open(path, "w") as f:
                f.write("+447700900999\n")
            n = notify.Notifier({"enabled": True, "destination": "+440000000000",
                                 "destination_file": path}, backend=Outbox())
            self.assertEqual(n.destination, "+447700900999")

    def test_status_never_gives_the_whole_address_away(self):
        """The status endpoint is behind a token, but the ledger copy of a
        notification is not: it goes to a bucket nobody can delete from."""
        n = make()
        self.assertEqual(n.destination_hint(), "+44***123")
        self.assertNotIn("7700900123", str(n.status()))
        e = notify.Notifier({"enabled": True, "destination": "paul@example.com",
                             "channel": "ses_email"}, backend=Outbox())
        self.assertEqual(e.destination_hint(), "pa***@example.com")

    def test_the_agent_cannot_read_or_edit_the_destination_file(self):
        policy = normalise_shell_policy({"enabled": True})
        for command in ("cat /etc/jarvis/notify.dest",
                        "echo +44777 > /etc/jarvis/notify.dest",
                        "grep . /etc/jarvis/notify.dest"):
            self.assertIsNotNone(check_command_allowed(command, policy), command)


class TestReportingIsWithinEveryRung(unittest.TestCase):
    """An observer that may look but not say what it saw is not an observer."""

    def _task(self):
        return Task(task_type=TaskType.NOTIFY_OPERATOR,
                    description="tell Paul the disk is filling", priority=5)

    def test_notifying_is_a_read_not_a_change(self):
        self.assertEqual(authority.classify_task(self._task()), authority.READ)

    def test_allowed_at_every_rung_including_observer(self):
        for rung in (authority.OBSERVER, authority.PROPOSER, authority.ACTOR):
            self.assertTrue(authority.review(self._task(), rung).allowed, rung)


if __name__ == "__main__":
    unittest.main()
