"""Agent state that only ever grew, and one line that forgot the wrong thing.

Regression tests for the state-hygiene fixes of 27 September 2026: the
operator profile, the task history, the diary's writes, the calendar's
reminders, the channel's dedupe keys and the question register. Each class
names the finding it guards.
"""

import logging
import os
import tempfile
import unittest
from datetime import datetime

from jarvis.agent import diary as d
from jarvis.agent import notify
from jarvis.agent import operator as op
from jarvis.agent import questions as q
from jarvis.agent import schedule as sc
from jarvis.agent import timesense

TZ = "Europe/London"
LOG = logging.getLogger("test")


def at(year, month, day, hour=0, minute=0) -> float:
    return datetime(year, month, day, hour, minute,
                    tzinfo=timesense.zone(TZ)).timestamp()


# A Monday morning.
NOW = at(2026, 9, 21, 10, 0)


class FakeNotifier:
    def __init__(self):
        self.sent = []
        self.hold = None

    def send(self, subject, body="", severity="notice", key=None, **kw):
        if self.hold:
            return {"sent": False, "held": True, "reason": self.hold}
        self.sent.append({"subject": subject, "body": body, "key": key})
        return {"sent": True, "held": False, "reason": None}


class FakeLedger:
    def record(self, kind, body):
        return True


class CountingStore:
    """Records every write, so a test can see what reached the disk."""

    def __init__(self):
        self.writes = []

    def remember(self, text, kind="note", source="brain", cycle=None,
                 pinned=False, meta=None):
        self.writes.append({"text": text, "kind": kind, "meta": meta})
        return {"id": len(self.writes)}

    def recent(self, limit=20, kind=None):
        return []

    def of_kind(self, kind):
        return [w for w in self.writes if w["kind"] == kind]


class FakeAgent:
    def __init__(self, store=None):
        self.log = LOG
        self.notifier = FakeNotifier()
        self.ledger = FakeLedger()
        self.store = store
        self.cycle_count = 1

    def remember(self, text, kind="note", source="brain", pinned=False, meta=None):
        return None


# ---- C1: forget('') took out the first line -------------------------------

class TestForgettingNothingForgetsNothing(unittest.TestCase):

    def setUp(self):
        self.profile = op.OperatorProfile(name="Paul")
        self.profile.add("Prefers short messages")
        self.profile.add("Works best in the morning")

    def test_an_empty_request_takes_nothing_out(self):
        for empty in ("", "   ", None):
            self.assertFalse(self.profile.forget(empty))
        self.assertEqual([f.text for f in self.profile.facts],
                         ["Prefers short messages", "Works best in the morning"])

    def test_a_real_request_still_works(self):
        self.assertTrue(self.profile.forget("short messages"))
        self.assertEqual([f.text for f in self.profile.facts],
                         ["Works best in the morning"])


# ---- C2: task history grew for the life of the process --------------------

class TestTaskHistoryIsBounded(unittest.TestCase):

    def test_it_keeps_the_last_thousand(self):
        from jarvis.agent.core import AgentCore
        from jarvis.agent.planner import Task, TaskType
        agent = AgentCore({"name": "t", "profile": "cloud"},
                          {"display": None, "input": None, "memory": None,
                           "storage": None}, LOG)
        history = agent.task_history
        for i in range(1000):
            history.append({"task": {"description": f"old {i}"},
                            "result": {"success": True}, "timestamp": 0})
        agent.act(Task(priority=5, description="look around",
                       task_type=TaskType.OBSERVATION))
        self.assertEqual(len(agent.task_history), 1000)
        self.assertIs(agent.task_history, history)   # trimmed in place
        self.assertEqual(agent.task_history[-1]["task"]["description"],
                         "look around")
        self.assertEqual(agent.task_history[0]["task"]["description"], "old 1")


# ---- C3: the diary wrote every commitment back on every tick --------------

class TestTheDiaryWritesOnlyWhatChanged(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = CountingStore()
        self.agent = FakeAgent(self.store)
        self.diary = d.Diary(self.agent, {"timezone": TZ}, clock=lambda: NOW,
                             state_file=os.path.join(self.tmp, "d.state"))

    def writes(self):
        return self.store.of_kind("commitment")

    def test_unchanged_ticks_do_not_write(self):
        self.diary.add("Renew the car tax", "friday 9am")
        self.diary.tick(NOW)                    # first look writes once
        settled = len(self.writes())
        for n in range(1, 60):
            self.diary.tick(NOW + n * 5)
        self.assertEqual(len(self.writes()), settled)

    def test_a_reminder_spoken_is_written(self):
        item = self.diary.add("Renew the car tax", "friday 9am")
        self.diary.tick(NOW)
        before = len(self.writes())
        self.diary.tick(at(2026, 9, 25, 9, 0))
        self.assertEqual(len(self.agent.notifier.sent), 1)
        self.assertGreater(len(self.writes()), before)
        last = self.writes()[-1]["meta"]
        self.assertEqual(last["state"], d.DONE)
        self.assertEqual(last["fired"]["0.0"]["how"], "said")
        self.assertEqual(item.state, d.DONE)

    def test_a_held_reminder_that_passed_an_earlier_one_is_written(self):
        self.diary.add("Dentist", "friday 9am", remind_before=["1 day", 0])
        self.diary.tick(NOW)
        before = len(self.writes())
        self.agent.notifier.hold = "quiet hours"
        # Both moments ripe at once: the earlier is marked passed, the later
        # is held. The passed mark is a change and has to reach the store.
        self.diary.tick(at(2026, 9, 25, 9, 5))
        self.assertGreater(len(self.writes()), before)
        fired = self.writes()[-1]["meta"]["fired"]
        self.assertEqual(fired[str(86400.0)]["how"], "passed")
        # Held again with nothing new: no write.
        settled = len(self.writes())
        self.diary.tick(at(2026, 9, 25, 9, 6))
        self.assertEqual(len(self.writes()), settled)

    def test_a_roll_is_written(self):
        item = self.diary.add("Bins out", "tuesday 7pm", repeat="weekly")
        self.diary.tick(NOW)
        self.diary.tick(at(2026, 9, 22, 19, 0))
        self.assertEqual(item.due_local, "2026-09-29T19:00")
        self.assertEqual(self.writes()[-1]["meta"]["due_local"],
                         "2026-09-29T19:00")

    def test_an_unchanged_one_is_still_refreshed_daily(self):
        """load() reads back only the most recent commitment rows. One nobody
        touched for months must not fall out of that window."""
        self.diary.add("Birthday", "2027-03-01 9am", repeat="yearly")
        self.diary.tick(NOW)
        before = len(self.writes())
        self.diary.tick(NOW + 3600)
        self.assertEqual(len(self.writes()), before)
        self.diary.tick(NOW + d.REFRESH_S + 1)
        self.assertEqual(len(self.writes()), before + 1)


# ---- C4: every occurrence of a repeat left a dropped commitment behind ----

class TestARepeatingAppointmentKeepsOneReminder(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.agent = FakeAgent()
        self.agent.diary = d.Diary(self.agent, {"timezone": TZ},
                                   clock=lambda: NOW,
                                   state_file=os.path.join(self.tmp, "d.state"))
        self.cal = sc.Schedule(self.agent, {"timezone": TZ}, clock=lambda: NOW)

    def run_until(self, end, step=300):
        """The loop's order: the calendar rolls first, then the diary looks."""
        t = NOW
        while t <= end:
            self.cal.clock = self.agent.diary.clock = (lambda v=t: v)
            self.cal.tick(t)
            self.agent.diary.tick(t)
            t += step

    def test_four_weeks_four_reminders_one_commitment(self):
        item = self.cal.add("Standup", "tuesday 9am for 15 minutes",
                            repeat="weekly")
        self.assertEqual(len(self.agent.diary.items), 1)
        self.run_until(at(2026, 10, 14, 12, 0))
        sent = self.agent.notifier.sent
        self.assertEqual(len(sent), 4, [s["body"] for s in sent])
        for day, body in zip((22, 29, 6, 13), (s["body"] for s in sent)):
            self.assertIn(f"{day:02d}", body)
        self.assertEqual(len(self.agent.diary.items), 1)
        self.assertEqual(len(self.agent.diary.standing()), 1)
        self.assertEqual(item.start_local, "2026-10-20T09:00")
        self.assertEqual(self.agent.diary.standing()[0].due_local,
                         "2026-10-20T09:00")
        self.assertEqual(item.commitment, self.agent.diary.standing()[0].id)

    def test_it_re_arms_when_its_reminder_was_dropped(self):
        """Guard, not a regression: the old behaviour for this case is kept."""
        item = self.cal.add("Standup", "tuesday 9am for 15 minutes",
                            repeat="weekly")
        self.agent.diary.drop(item.commitment)
        self.run_until(at(2026, 9, 22, 10, 0))
        held = self.agent.diary.standing()
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0].due_local, "2026-09-29T09:00")
        self.assertEqual(item.commitment, held[0].id)


# ---- C5: a place ran to the end of the line --------------------------------

class TestAPlaceStopsWhereTheWhenBegins(unittest.TestCase):

    def span(self, text):
        return sc.parse_span(text, NOW, TZ)

    def test_a_place_before_a_day(self):
        got = self.span("dentist in Leeds tomorrow")
        self.assertEqual(got["where"], "Leeds")
        self.assertEqual(got["start_local"], "2026-09-22T00:00")

    def test_a_place_before_a_weekday(self):
        got = self.span("lunch at Nandos friday")
        self.assertEqual(got["where"], "Nandos")
        self.assertEqual(got["start_local"], "2026-09-25T00:00")

    def test_a_place_before_a_day_and_a_time(self):
        got = self.span("meeting at the office friday 2pm")
        self.assertEqual(got["where"], "the office")
        self.assertEqual(got["start_local"], "2026-09-25T14:00")

    def test_the_old_forms_still_read_the_same(self):
        got = self.span("friday 2pm for 1 hour at the dentist")
        self.assertEqual((got["where"], got["start_local"], got["minutes"]),
                         ("the dentist", "2026-09-25T14:00", 60))
        self.assertEqual(self.span("friday at 2pm")["where"], "")
        got = self.span("friday 2pm to 4pm at the office")
        self.assertEqual((got["where"], got["minutes"]), ("the office", 120))

    def test_a_time_of_day_is_not_a_place(self):
        self.assertEqual(self.span("friday in the morning")["where"], "")
        self.assertEqual(self.span("tomorrow at noon")["where"], "")


# ---- C6: dedupe keys were never let go --------------------------------------

class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class TestDedupeKeysAreLetGo(unittest.TestCase):

    def make(self, **cfg):
        tmp = tempfile.mkdtemp()
        settings = {"enabled": True, "destination": "+447700900123",
                    "quiet_hours": None, "min_gap_s": 0,
                    "dedupe_window_s": 600,
                    "state_file": os.path.join(tmp, "notify.state")}
        settings.update(cfg)
        self.clock = Clock()
        self.outbox = []
        return notify.Notifier(settings, clock=self.clock,
                               backend=lambda dest, text: self.outbox.append(text))

    def test_old_keys_are_dropped(self):
        for cap in (10, 0):                 # with and without an hourly cap
            n = self.make(max_per_hour=cap)
            self.assertTrue(n.send("one", key="a")["sent"])
            self.assertTrue(n.send("two", key="b")["sent"])
            self.clock.t += 700
            self.assertTrue(n.send("three", key="c")["sent"])
            self.assertEqual(set(n._recent_keys), {"c"})

    def test_a_key_inside_the_window_still_holds(self):
        n = self.make(max_per_hour=10)
        self.assertTrue(n.send("one", key="a")["sent"])
        self.clock.t += 300
        n.status()                          # prunes; must not forget "a"
        verdict = n.send("one again", key="a")
        self.assertFalse(verdict["sent"])
        self.assertIn("already sent", verdict["reason"])


# ---- C7: the question register kept everything ------------------------------

class QClock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class TestTheRegisterLetsHistoryGo(unittest.TestCase):

    def setUp(self):
        self.clock = QClock()
        self.reg = q.QuestionRegister(clock=self.clock)

    def ask(self, i, **kw):
        return self.reg.ask(f"Should the interval for service{i} alpha{i} "
                            f"beta{i} change?", blocked_on=f"a ruling on {i}",
                            **kw)

    def test_only_the_last_fifty_answers_are_kept(self):
        for i in range(60):
            self.clock.t += 1
            self.reg.answer(self.ask(i).id, "No.", by="Paul")
        self.reg.sweep()
        answered = [x for x in self.reg.questions if x.state == q.ANSWERED]
        self.assertEqual(len(answered), q.KEEP_ANSWERED)
        self.assertIn("service59", answered[-1].text)
        self.assertNotIn("service9 ", " ".join(x.text for x in answered))
        # A recent ruling is still remembered.
        with self.assertRaises(q.Refused) as why:
            self.reg.ask("Should the interval for service59 alpha59 beta59 "
                         "change, put another way?", blocked_on="still")
        self.assertIn("already ruled on", str(why.exception))

    def test_closed_questions_go_after_a_week(self):
        expired = self.ask(1, ttl_s=60)
        withdrawn = self.ask(2)
        still_open = self.ask(3, ttl_s=30 * 86400)
        self.reg.withdraw(withdrawn.id)
        self.clock.t += 3600
        self.reg.sweep()
        self.assertEqual(expired.state, q.EXPIRED)
        self.assertEqual(len(self.reg.questions), 3)     # within the week
        self.clock.t += 8 * 86400
        self.reg.sweep()
        self.assertEqual(self.reg.questions, [still_open])


if __name__ == "__main__":
    unittest.main()
