"""Spans rather than moments, and the lie that comes with them.

The diary holds points: a moment and a message. An appointment starts, lasts
and ends, and three things follow that a point cannot express -- two of them
can collide, between them there are gaps, and while one is running he is in
it.

The failure this module is really built against is the third kind of honesty
problem in this codebase, after "unread is never empty" and "configured is not
proven". It is this: an empty calendar is not a free day. Every assistant that
has said "you're free Thursday" from an empty Thursday was guessing about a
person's life from a record it knows is incomplete -- and unlike a wrong
reminder, that mistake is invisible until he has already said yes.
"""

import os
import tempfile
import unittest
from datetime import datetime

from jarvis.agent import diary as d
from jarvis.agent import schedule as sc
from jarvis.agent import timesense
from jarvis.agent.store import build_store

TZ = "Europe/London"


def at(year, month, day, hour=0, minute=0) -> float:
    return datetime(year, month, day, hour, minute,
                    tzinfo=timesense.zone(TZ)).timestamp()


# A Monday morning.
NOW = at(2026, 9, 21, 10, 0)


class FakeNotifier:
    def __init__(self):
        self.sent = []
        self.calls = []
        self.hold = None

    def send(self, subject, body="", severity="notice", key=None, **kw):
        self.calls.append(dict(kw, subject=subject, severity=severity))
        if self.hold:
            return {"sent": False, "held": True, "reason": self.hold}
        self.sent.append({"subject": subject, "body": body, "key": key})
        return {"sent": True, "held": False, "reason": None}


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
        self.notifier = FakeNotifier()
        self.ledger = FakeLedger()
        self.store = store
        self.cycle_count = 3
        self.notes = []

    def remember(self, text, kind="note", source="brain", pinned=False, meta=None):
        self.notes.append(text)
        return None


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.agent = FakeAgent()
        self.agent.diary = d.Diary(self.agent, {"timezone": TZ},
                                   clock=lambda: NOW,
                                   state_file=os.path.join(self.tmp, "d.state"))
        self.cal = sc.Schedule(self.agent, {"timezone": TZ}, clock=lambda: NOW)

    def clock(self, when):
        self.cal.clock = lambda: when
        self.agent.diary.clock = lambda: when


class TestReadingASpan(unittest.TestCase):

    def span(self, text, now=NOW):
        return sc.parse_span(text, now, TZ)

    def test_a_length_in_words(self):
        got = self.span("friday 2pm for 1 hour")
        self.assertEqual(got["start_local"], "2026-09-25T14:00")
        self.assertEqual(got["minutes"], 60)

    def test_a_length_in_minutes(self):
        self.assertEqual(self.span("tomorrow 9am for 90 minutes")["minutes"], 90)

    def test_an_end_time_instead_of_a_length(self):
        got = self.span("friday 2pm to 4pm")
        self.assertEqual(got["start_local"], "2026-09-25T14:00")
        self.assertEqual(got["minutes"], 120)

    def test_a_dash_between_two_clock_times(self):
        got = self.span("friday 14:00-16:30")
        self.assertEqual(got["minutes"], 150)

    def test_an_evening_that_runs_past_midnight(self):
        got = self.span("friday 10pm to 1am")
        self.assertEqual(got["minutes"], 180)
        self.assertIn("following day", got["note"])

    def test_no_length_is_assumed_and_said(self):
        got = self.span("friday 2pm")
        self.assertEqual(got["minutes"], sc.DEFAULT_MINUTES)
        self.assertIn("no length given", got["assumed_length"])

    def test_a_day_with_no_time_sits_on_the_day(self):
        """He knows the day and not the hour. Blocking the whole day would
        make every other entry that Thursday a clash."""
        got = self.span("thursday")
        self.assertTrue(got["all_day"])
        self.assertTrue(got["start_local"].endswith("T00:00"))
        self.assertIn("sits on the day rather than in it", got["note"])

    def test_a_place_is_taken_out_of_the_name(self):
        got = self.span("friday 2pm for 1 hour at the dentist")
        self.assertEqual(got["where"], "the dentist")
        self.assertEqual(got["start_local"], "2026-09-25T14:00")

    def test_a_time_is_not_mistaken_for_a_place(self):
        self.assertEqual(self.span("friday at 2pm")["where"], "")

    def test_something_with_no_length_at_all_is_sent_to_the_diary(self):
        with self.assertRaises(d.Refused) as caught:
            self.span("friday 2pm to 2pm")
        self.assertIn("put it in the diary", str(caught.exception))

    def test_a_fortnight_long_appointment_is_refused(self):
        with self.assertRaises(d.Refused):
            self.span("friday 9am for 30 days")


class TestHoldingOne(Base):

    def test_it_holds_a_span(self):
        got = self.cal.add("Dentist", "friday 2pm for 30 minutes")
        self.assertEqual(got.start_local, "2026-09-25T14:00")
        self.assertEqual(got.end_local(), "2026-09-25T14:30")
        self.assertIn("14:00-14:30", got.reads_as())

    def test_something_already_over_is_refused(self):
        with self.assertRaises(d.Refused) as caught:
            self.cal.add("Dentist", "21 September 2026 8am for 30 minutes")
        self.assertIn("already over", str(caught.exception))

    def test_something_running_right_now_is_not_refused(self):
        """Half past ten, in a ten-to-eleven meeting. Entering it late is a
        normal thing a person does."""
        item = self.cal.add("Standup", "21 September 2026 09:30 for 90 minutes")
        self.assertTrue(item.covers(NOW))
        self.assertIs(self.cal.running(NOW), item)

    def test_cancelling_is_his_and_takes_the_reminder_with_it(self):
        item = self.cal.add("Dentist", "friday 2pm for 30 minutes")
        self.assertTrue(item.commitment)
        self.assertTrue(self.cal.cancel(item.id))
        self.assertEqual(self.cal.standing(), [])
        self.assertEqual(self.agent.diary.standing(), [])

    def test_the_calendar_is_bounded(self):
        self.cal.max_items = 1
        self.cal.add("One", "friday 2pm for 1 hour")
        with self.assertRaises(d.Refused):
            self.cal.add("Two", "friday 4pm for 1 hour")


class TestTwoThingsAtOnce(Base):
    """A collision is a fact about his life, not a data problem to tidy up."""

    def test_an_overlap_is_reported(self):
        self.cal.add("Dentist", "friday 2pm for 1 hour")
        self.cal.add("Call with the accountant", "friday 14:30 for 1 hour")
        clash = self.cal.clashes(30, NOW)
        self.assertEqual(len(clash), 1)
        self.assertEqual(clash[0]["minutes"], 30)
        self.assertIn("overlaps", clash[0]["reads_as"])
        self.assertIn("30 minutes", clash[0]["reads_as"])

    def test_both_are_still_standing(self):
        """Refusing the second, or moving it, is how an appointment goes
        missing."""
        self.cal.add("Dentist", "friday 2pm for 1 hour")
        self.cal.add("Call", "friday 14:30 for 1 hour")
        self.assertEqual(len(self.cal.standing()), 2)

    def test_touching_is_not_overlapping(self):
        self.cal.add("First", "friday 2pm for 1 hour")
        self.cal.add("Second", "friday 3pm for 1 hour")
        self.assertEqual(self.cal.clashes(30, NOW), [])

    def test_an_all_day_entry_blocks_nothing(self):
        """Otherwise every other entry that day is a clash, and the register
        becomes noise on exactly the days he is busiest."""
        self.cal.add("Away in Leeds", "thursday")
        self.cal.add("Call", "thursday 2pm for 1 hour")
        self.assertEqual(self.cal.clashes(30, NOW), [])

    def test_a_cancelled_one_cannot_clash(self):
        first = self.cal.add("Dentist", "friday 2pm for 1 hour")
        self.cal.add("Call", "friday 14:30 for 1 hour")
        self.cal.cancel(first.id)
        self.assertEqual(self.cal.clashes(30, NOW), [])


class TestAnEmptyCalendarIsNotAFreeDay(Base):
    """The lie this module exists to not tell."""

    def test_every_answer_says_what_it_is_a_statement_about(self):
        got = self.cal.free("2026-09-24")
        self.assertIn("not the same as being free", got["what_this_is"])
        self.assertIn("invisible", got["what_this_is"])

    def test_an_empty_day_still_carries_the_caveat(self):
        got = self.cal.free("2026-09-24")
        self.assertEqual(len(got["gaps"]), 1)
        self.assertEqual(got["gaps"][0]["from"], "09:00")
        self.assertIn("not the same as being free", got["what_this_is"])

    def test_the_gaps_are_around_what_is_written_down(self):
        self.cal.add("Dentist", "24 September 2026 11:00 for 1 hour")
        self.cal.add("Call", "24 September 2026 15:00 for 30 minutes")
        gaps = self.cal.free("2026-09-24")["gaps"]
        self.assertEqual([(g["from"], g["to"]) for g in gaps],
                         [("09:00", "11:00"), ("12:00", "15:00"), ("15:30", "18:00")])

    def test_a_gap_too_short_to_use_is_not_offered(self):
        self.cal.add("One", "24 September 2026 09:00 for 2 hours")
        self.cal.add("Two", "24 September 2026 11:15 for 2 hours")
        gaps = self.cal.free("2026-09-24", minimum_minutes=30)["gaps"]
        self.assertNotIn(("11:00", "11:15"), [(g["from"], g["to"]) for g in gaps])

    def test_today_does_not_offer_hours_already_gone(self):
        """It is ten in the morning. Nine o'clock is not available."""
        gaps = self.cal.free("2026-09-21", now=NOW)["gaps"]
        self.assertEqual(gaps[0]["from"], "10:00")

    def test_it_names_what_it_worked_around(self):
        self.cal.add("Dentist", "24 September 2026 11:00 for 1 hour")
        self.cal.add("Away", "24 September 2026")
        got = self.cal.free("2026-09-24")
        self.assertTrue(any("Dentist" in line for line in got["around"]))
        self.assertEqual(got["all_day"], ["Away"])

    def test_an_all_day_entry_does_not_swallow_the_gaps(self):
        self.cal.add("Away in Leeds", "24 September 2026")
        self.assertTrue(self.cal.free("2026-09-24")["gaps"])


class TestTheReminderIsTheDiarysJob(Base):
    """One firing path. Held messages, missed moments, catching up and
    lateness were solved once; an appointment that spoke for itself would
    have to solve all of it again and be wrong somewhere."""

    def test_adding_one_arms_a_commitment(self):
        item = self.cal.add("Dentist", "friday 2pm for 30 minutes")
        held = self.agent.diary.standing()
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0].id, item.commitment)
        self.assertEqual(held[0].leads, (1800.0,))

    def test_it_speaks_through_the_diary_at_the_lead(self):
        self.cal.add("Dentist", "friday 2pm for 30 minutes", where="Queen Street")
        self.clock(at(2026, 9, 25, 13, 30))
        self.agent.diary.tick(at(2026, 9, 25, 13, 30))
        self.assertEqual(len(self.agent.notifier.sent), 1)
        self.assertIn("Dentist", self.agent.notifier.sent[0]["subject"])
        self.assertIn("Queen Street", self.agent.notifier.sent[0]["subject"])

    def test_it_does_not_speak_twice_for_one_appointment(self):
        self.cal.add("Dentist", "friday 2pm for 30 minutes")
        for when in (at(2026, 9, 25, 13, 30), at(2026, 9, 25, 14, 0),
                     at(2026, 9, 25, 14, 30)):
            self.clock(when)
            self.agent.diary.tick(when)
        self.assertEqual(len(self.agent.notifier.sent), 1)

    def test_cancelling_silences_it(self):
        item = self.cal.add("Dentist", "friday 2pm for 30 minutes")
        self.cal.cancel(item.id)
        self.clock(at(2026, 9, 25, 13, 30))
        self.agent.diary.tick(at(2026, 9, 25, 13, 30))
        self.assertEqual(self.agent.notifier.sent, [])

    def test_moving_it_moves_the_reminder(self):
        item = self.cal.add("Dentist", "friday 2pm for 30 minutes")
        moved = self.cal.move(item.id, "friday 4pm")
        self.assertEqual(moved.start_local, "2026-09-25T16:00")
        self.assertEqual(moved.minutes, 30)
        self.clock(at(2026, 9, 25, 13, 30))
        self.agent.diary.tick(at(2026, 9, 25, 13, 30))
        self.assertEqual(self.agent.notifier.sent, [])
        self.clock(at(2026, 9, 25, 15, 30))
        self.agent.diary.tick(at(2026, 9, 25, 15, 30))
        self.assertEqual(len(self.agent.notifier.sent), 1)

    def test_it_reports_the_reminder_it_has_not_the_one_configured(self):
        """An all-day entry arms nothing, and so does one entered after its
        warning has gone by. Both were saying "he says something 30 minutes
        before", and neither would."""
        timed = self.cal.add("Dentist", "friday 2pm for 30 minutes")
        self.assertEqual(timed.state_dict()["reminder"], "30 minutes")
        banner = self.cal.add("Away in Leeds", "thursday")
        self.assertIsNone(banner.state_dict()["reminder"])
        late = self.cal.add("Rush", "21 September 2026 10:20 for 30 minutes")
        self.assertIsNone(late.state_dict()["reminder"])

    def test_a_cancelled_appointment_reports_no_reminder(self):
        item = self.cal.add("Dentist", "friday 2pm for 30 minutes")
        self.cal.cancel(item.id)
        self.assertIsNone(item.state_dict()["reminder"])

    def test_the_reminder_is_marked_as_the_calendars_voice(self):
        """Shown in both registers it reads as two things, and dropping the
        reminder would leave an appointment still booked and now silent."""
        self.cal.add("Dentist", "friday 2pm for 30 minutes")
        held = self.agent.diary.standing()[0]
        self.assertEqual(held.origin, sc.CALENDAR)
        self.assertEqual(held.state_dict()["from"], "calendar")

    def test_his_own_diary_entries_are_not_marked_that_way(self):
        self.agent.diary.add("Pay the gas bill", "14 Oct at 9am", now=NOW)
        mine = [c for c in self.agent.diary.standing() if c.origin != sc.CALENDAR]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0].what, "Pay the gas bill")

    def test_an_all_day_entry_does_not_warn_half_an_hour_before_midnight(self):
        self.cal.add("Away in Leeds", "thursday")
        self.assertEqual(self.agent.diary.standing(), [])

    def test_a_proposal_arms_nothing(self):
        self.cal.propose("Something from a letter", "friday 2pm for 1 hour")
        self.assertEqual(self.agent.diary.standing(), [])
        self.clock(at(2026, 9, 25, 13, 30))
        self.agent.diary.tick(at(2026, 9, 25, 13, 30))
        self.assertEqual(self.agent.notifier.sent, [])

    def test_confirming_it_arms_the_reminder(self):
        made = self.cal.propose("From a letter", "friday 2pm for 1 hour")
        self.assertIsNotNone(self.cal.confirm(made.id))
        self.assertEqual(len(self.agent.diary.standing()), 1)

    def test_one_entered_after_its_warning_has_passed_arms_nothing(self):
        """Half ten, for an eleven o'clock. There is no half-hour warning
        left to give, and a reminder that fires the moment it is entered is
        not a reminder."""
        self.cal.add("Dentist", "21 September 2026 10:20 for 30 minutes")
        self.assertEqual(self.agent.diary.standing(), [])


class TestRepeating(Base):

    def test_a_weekly_one_rolls_after_it_ends(self):
        item = self.cal.add("Standup", "tuesday 9am for 15 minutes",
                            repeat="weekly")
        self.assertEqual(item.start_local, "2026-09-22T09:00")
        self.clock(at(2026, 9, 22, 9, 30))
        self.cal.tick(at(2026, 9, 22, 9, 30))
        self.assertEqual(item.start_local, "2026-09-29T09:00")

    def test_it_does_not_roll_while_it_is_running(self):
        item = self.cal.add("Standup", "tuesday 9am for 60 minutes",
                            repeat="weekly")
        self.clock(at(2026, 9, 22, 9, 30))
        self.cal.tick(at(2026, 9, 22, 9, 30))
        self.assertEqual(item.start_local, "2026-09-22T09:00")

    def test_a_month_away_catches_up_rather_than_replaying(self):
        item = self.cal.add("Standup", "tuesday 9am for 15 minutes",
                            repeat="weekly")
        self.clock(at(2026, 10, 27, 12, 0))
        self.cal.tick(at(2026, 10, 27, 12, 0))
        self.assertEqual(item.start_local, "2026-11-03T09:00")

    def test_its_identity_survives_the_roll(self):
        item = self.cal.add("Standup", "tuesday 9am for 15 minutes",
                            repeat="weekly")
        before = item.id
        self.clock(at(2026, 9, 22, 9, 30))
        self.cal.tick(at(2026, 9, 22, 9, 30))
        self.assertEqual(item.id, before)

    def test_the_rolled_occurrence_gets_its_own_reminder(self):
        self.cal.add("Standup", "tuesday 9am for 15 minutes", repeat="weekly")
        self.clock(at(2026, 9, 22, 9, 30))
        self.cal.tick(at(2026, 9, 22, 9, 30))
        held = self.agent.diary.standing()
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0].due_local, "2026-09-29T09:00")


class TestTheShapeOfADay(Base):

    def setUp(self):
        super().setUp()
        self.cal.add("Standup", "21 September 2026 11:00 for 15 minutes")
        self.cal.add("Dentist", "21 September 2026 15:00 for 30 minutes")
        self.cal.add("Away in Leeds", "22 September 2026")

    def test_a_day_reads_in_order(self):
        shape = self.cal.day_shape("2026-09-21", NOW)
        self.assertLess(shape.index("Standup"), shape.index("Dentist"))
        self.assertIn("11:00", shape)

    def test_a_banner_day_says_so(self):
        self.assertIn("all day: Away in Leeds",
                      self.cal.day_shape("2026-09-22", NOW))

    def test_an_empty_day_says_nothing_is_written_down(self):
        """Not "free". The whole point."""
        shape = self.cal.day_shape("2026-09-23", NOW)
        self.assertIn("nothing written down", shape)
        self.assertNotIn("free", shape)

    def test_what_is_next(self):
        self.assertEqual(self.cal.next_up(NOW).what, "Standup")

    def test_what_he_is_in_now(self):
        self.assertIsNone(self.cal.running(NOW))
        self.assertEqual(self.cal.running(at(2026, 9, 21, 11, 5)).what, "Standup")

    def test_a_banner_is_not_something_he_is_sitting_in(self):
        self.assertIsNone(self.cal.running(at(2026, 9, 22, 11, 0)))

    def test_the_week_is_seven_lines(self):
        self.assertEqual(len(self.cal.state(NOW)["week"]), 7)

    def test_the_summary_is_counts_not_the_calendar(self):
        got = self.cal.summary(NOW)
        self.assertEqual(got["today"], 2)
        self.assertIn("Standup", got["next"])
        self.assertNotIn("today_list", got)


class TestWhatTheModelIsTold(Base):

    def test_an_empty_calendar_says_nothing(self):
        self.assertIsNone(self.cal.context(NOW))

    def test_it_is_his_time_not_a_task_list(self):
        self.cal.add("Dentist", "friday 2pm for 1 hour")
        got = self.cal.context(NOW)
        self.assertIn("not your task list", got["how_to_use_this"])
        self.assertIn("you may not confirm one", got["how_to_use_this"])

    def test_it_repeats_the_caveat_to_the_model_too(self):
        """The model is the most likely thing in the system to say "you're
        free Thursday"."""
        self.cal.add("Dentist", "friday 2pm for 1 hour")
        self.assertIn("not the same as free time",
                      self.cal.context(NOW)["how_to_use_this"])

    def test_it_says_when_he_is_in_something(self):
        self.cal.add("Standup", "21 September 2026 09:30 for 90 minutes")
        self.assertIn("Standup", self.cal.context(NOW)["he_is_in_something_now"])

    def test_a_clash_reaches_the_model(self):
        self.cal.add("Dentist", "friday 2pm for 1 hour")
        self.cal.add("Call", "friday 14:30 for 1 hour")
        self.assertTrue(self.cal.context(NOW)["two_things_at_once"])


class TestItSurvivesARestart(unittest.TestCase):

    def setUp(self):
        import logging
        self.tmp = tempfile.mkdtemp()
        self.store = build_store({"path": os.path.join(self.tmp, "m.db")},
                                 logging.getLogger("test"))
        self.agent = FakeAgent(self.store)

    def tearDown(self):
        self.store.close()

    def build(self):
        self.agent.diary = d.Diary(self.agent, {"timezone": TZ}, clock=lambda: NOW,
                                   state_file=os.path.join(self.tmp, "d.state"))
        self.agent.diary.load()
        cal = sc.Schedule(self.agent, {"timezone": TZ}, clock=lambda: NOW)
        cal.load()
        return cal

    def test_an_appointment_comes_back(self):
        first = self.build()
        item = first.add("Dentist", "friday 2pm for 30 minutes at Queen Street")
        again = self.build()
        back = again.standing()[0]
        self.assertEqual(back.id, item.id)
        self.assertEqual(back.start_local, "2026-09-25T14:00")
        self.assertEqual(back.minutes, 30)
        self.assertEqual(back.where, "Queen Street")

    def test_it_comes_back_with_its_voice(self):
        """An appointment restored without its reminder is a calendar entry
        that has quietly stopped being a reminder."""
        first = self.build()
        first.add("Dentist", "friday 2pm for 30 minutes")
        again = self.build()
        self.assertEqual(len(again.standing()), 1)
        self.assertEqual(len(self.agent.diary.standing()), 1)
        self.assertEqual(again.standing()[0].commitment,
                         self.agent.diary.standing()[0].id)

    def test_a_cancelled_one_stays_cancelled_and_stays_silent(self):
        first = self.build()
        item = first.add("Dentist", "friday 2pm for 30 minutes")
        first.cancel(item.id)
        again = self.build()
        self.assertEqual(again.standing(), [])
        self.assertEqual(self.agent.diary.standing(), [])

    def test_it_goes_in_where_the_backup_will_find_it(self):
        self.build().add("Dentist", "friday 2pm for 30 minutes")
        rows = self.store.recent(10, kind="appointment")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["pinned"])
        self.assertTrue(rows[0]["meta"]["appointment"])


class TestTheChannelKnowsWhenHeIsInSomething(unittest.TestCase):
    """Quiet hours are a guess at when he is unavailable. The calendar is a
    statement of it. The split is the contract, not the content: a reminder
    he asked for arrives when he asked for it; an observation the agent chose
    to raise can wait twenty minutes."""

    def setUp(self):
        import logging
        from jarvis.agent.notify import Notifier
        self.sent = []
        self.notifier = Notifier(
            {"enabled": True, "channel": "sns_sms", "destination": "+440000",
             "min_gap_s": 0, "max_per_hour": 0, "dedupe_window_s": 0,
             "quiet_hours": [22, 7], "timezone": TZ},
            logging.getLogger("test"), clock=lambda: at(2026, 9, 21, 11, 5))
        self.notifier._backend = lambda dest, text: self.sent.append(text)
        self.busy = None
        self.notifier.busy_check = lambda: self.busy

    def test_with_no_calendar_nothing_changes(self):
        self.assertIsNone(self.notifier.hold_reason("notice", "k"))

    def test_an_agent_notice_waits_until_he_is_out(self):
        self.busy = {"what": "Standup", "until": "11:15"}
        got = self.notifier.hold_reason("notice", "k")
        self.assertIn("Standup", got)
        self.assertIn("11:15", got)

    def test_a_reminder_he_asked_for_goes_through_anyway(self):
        self.busy = {"what": "Standup", "until": "11:15"}
        self.assertIsNone(self.notifier.hold_reason("notice", "k",
                                                    time_critical=True))

    def test_an_alert_is_never_held_by_it(self):
        self.busy = {"what": "Standup", "until": "11:15"}
        self.assertIsNone(self.notifier.hold_reason("alert", "k"))

    def test_a_broken_calendar_does_not_break_the_channel(self):
        def boom():
            raise RuntimeError("no")
        self.notifier.busy_check = boom
        self.assertIsNone(self.notifier.hold_reason("notice", "k"))

    def test_the_hold_is_reported_not_swallowed(self):
        self.busy = {"what": "Standup", "until": "11:15"}
        got = self.notifier.send("disk is filling up", severity="notice")
        self.assertFalse(got["sent"])
        self.assertTrue(got["held"])
        self.assertIn("Standup", got["reason"])
        self.assertEqual(self.sent, [])


class TestTheDiaryMarksItsOwnSends(Base):

    def test_a_reminder_is_sent_as_time_critical(self):
        self.cal.add("Dentist", "friday 2pm for 30 minutes")
        self.clock(at(2026, 9, 25, 13, 30))
        self.agent.diary.tick(at(2026, 9, 25, 13, 30))
        self.assertTrue(self.agent.notifier.calls[-1]["time_critical"])


if __name__ == "__main__":
    unittest.main()
