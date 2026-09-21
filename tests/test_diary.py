"""Keeping a date, which is a different job from reading one.

The agent could already turn "amount due 14 Oct" into "Wednesday 14 October,
24 days from now". That fact was true for one cycle and then nothing happened
on the fourteenth, because knowing a date and keeping it are not the same
thing.

Most of what is tested here is the register refusing to lie about what it
did: a message the channel held is not a message that was sent, a reminder
that came round while nothing was running says so rather than arriving as
though it were on time, and a month of downtime on a daily commitment is one
message and a count rather than thirty messages.
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta

from jarvis.agent import diary as d
from jarvis.agent import timesense
from jarvis.agent.store import build_store

TZ = "Europe/London"
HAVE_TZ = timesense.zone(TZ).utcoffset(datetime(2026, 7, 1)) == timedelta(hours=1)


def at(year, month, day, hour=0, minute=0) -> float:
    """A wall-clock reading in his timezone, as an instant."""
    return datetime(year, month, day, hour, minute,
                    tzinfo=timesense.zone(TZ)).timestamp()


# A Monday morning.
NOW = at(2026, 9, 21, 10, 0)


class FakeNotifier:
    """Enough of the channel to be unhelpful in the ways the channel is."""

    def __init__(self, hold=None):
        self.sent = []
        self.hold = hold                       # a reason, or None

    def send(self, subject, body="", severity="notice", key=None):
        if self.hold:
            return {"sent": False, "held": True, "reason": self.hold}
        self.sent.append({"subject": subject, "body": body,
                          "severity": severity, "key": key})
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
        self.cycle_count = 7
        self.notes = []

    def remember(self, text, kind="note", source="brain", pinned=False, meta=None):
        self.notes.append(text)
        if self.store is not None:
            return self.store.remember(text, kind=kind, source=source, meta=meta)
        return None


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.agent = FakeAgent()
        self.diary = d.Diary(self.agent, {"timezone": TZ},
                             clock=lambda: NOW,
                             state_file=os.path.join(self.tmp, "diary.state"))

    @property
    def sent(self):
        return self.agent.notifier.sent


class TestReadingWhatHeTyped(unittest.TestCase):
    """The forms a person actually writes, not the form a parser would like."""

    def when(self, text, now=NOW):
        return d.parse_when(text, now, TZ)

    def test_a_date_with_a_time(self):
        got = self.when("14 Oct at 9am")
        self.assertEqual(got["local"], "2026-10-14T09:00")

    def test_a_date_without_a_time_says_what_it_assumed(self):
        got = self.when("14 October 2026")
        self.assertEqual(got["local"], "2026-10-14T09:00")
        self.assertIn("no time of day given", got["assumed_time"])

    def test_the_date_is_not_mistaken_for_the_time(self):
        """'14 Oct' has a 14 in it, and a careless time parser takes it."""
        self.assertEqual(self.when("14 Oct")["local"], "2026-10-14T09:00")
        self.assertEqual(self.when("3 Nov 17:45")["local"], "2026-11-03T17:45")

    def test_twenty_four_hour_and_twelve_hour_both_read(self):
        self.assertEqual(self.when("tomorrow 17:45")["local"], "2026-09-22T17:45")
        self.assertEqual(self.when("tomorrow 5.45pm")["local"], "2026-09-22T17:45")
        self.assertEqual(self.when("tomorrow 12am")["local"], "2026-09-22T00:00")
        self.assertEqual(self.when("tomorrow 12pm")["local"], "2026-09-22T12:00")

    def test_words_for_times_of_day(self):
        self.assertEqual(self.when("tomorrow evening")["local"], "2026-09-22T18:00")
        self.assertEqual(self.when("tomorrow morning")["local"], "2026-09-22T09:00")

    def test_tomorrow_and_the_day_after(self):
        self.assertEqual(self.when("tomorrow")["local"], "2026-09-22T09:00")
        self.assertEqual(self.when("the day after tomorrow")["local"],
                         "2026-09-23T09:00")

    def test_a_weekday_means_the_next_one(self):
        """Today is a Monday. 'Monday' means the one coming, not this morning."""
        self.assertEqual(self.when("friday 2pm")["local"], "2026-09-25T14:00")
        self.assertEqual(self.when("monday")["local"], "2026-09-28T09:00")

    def test_in_n_days_and_hours(self):
        self.assertEqual(self.when("in 3 days")["local"], "2026-09-24T09:00")
        self.assertEqual(self.when("in 2 hours")["local"], "2026-09-21T12:00")

    def test_an_ambiguous_date_stays_ambiguous(self):
        got = self.when("05/10/2026")
        self.assertEqual(got["local"], "2026-10-05T09:00")
        self.assertIn("worth confirming", got["ambiguous"])

    def test_nothing_to_go_on_is_refused_with_what_would_work(self):
        with self.assertRaises(d.Refused) as caught:
            self.when("sometime soon-ish")
        self.assertIn("tomorrow", str(caught.exception))

    def test_lengths_of_time(self):
        self.assertEqual(d.parse_duration("3 days"), 3 * 86400)
        self.assertEqual(d.parse_duration("90 minutes"), 5400)
        self.assertEqual(d.parse_duration(2), 2 * 86400)
        self.assertEqual(d.parse_duration("on the day"), 0)
        with self.assertRaises(d.Refused):
            d.parse_duration("a bit before")

    def test_repeats_are_a_closed_list(self):
        self.assertEqual(d.parse_every("every month"), d.MONTHLY)
        self.assertEqual(d.parse_every(None), d.ONCE)
        with self.assertRaises(d.Refused) as caught:
            d.parse_every("0 9 * * 1-5")
        self.assertIn("scheduling language", str(caught.exception))


class TestHoldingOne(Base):

    def test_it_holds_what_he_asked_for(self):
        item = self.diary.add("Pay the British Gas bill", "14 Oct at 9am")
        self.assertEqual(item.state, d.STANDING)
        self.assertEqual(item.due_local, "2026-10-14T09:00")
        self.assertEqual(self.diary.upcoming(30)[0].what, "Pay the British Gas bill")

    def test_a_date_already_gone_is_refused_rather_than_held(self):
        """A one-off in the past would sit there forever firing nothing."""
        with self.assertRaises(d.Refused) as caught:
            self.diary.add("Pay it", "14 August 2026")
        self.assertIn("already passed", str(caught.exception))

    def test_a_repeat_with_a_past_anchor_is_caught_up_not_refused(self):
        """'The rent, first of every month' is said in the middle of one."""
        item = self.diary.add("Rent", "1 August 2026", repeat="monthly")
        self.assertEqual(item.anchor_local, "2026-08-01T09:00")
        self.assertEqual(item.due_local, "2026-10-01T09:00")

    def test_a_mistyped_year_is_caught(self):
        with self.assertRaises(d.Refused) as caught:
            self.diary.add("MOT", "14 Oct 2036")
        self.assertIn("mistyped year", str(caught.exception))

    def test_the_same_thing_twice_is_one_commitment(self):
        first = self.diary.add("Dentist", "14 Oct at 9am")
        again = self.diary.add("Dentist", "14 Oct at 9am")
        self.assertIs(first, again)
        self.assertEqual(len(self.diary.standing()), 1)

    def test_asking_again_for_something_dropped_revives_it(self):
        item = self.diary.add("Dentist", "14 Oct at 9am")
        self.diary.drop(item.id)
        self.assertEqual(self.diary.add("Dentist", "14 Oct at 9am").state,
                         d.STANDING)

    def test_dropping_is_his_and_needs_no_reason(self):
        item = self.diary.add("Dentist", "14 Oct")
        self.assertTrue(self.diary.drop(item.id))
        self.assertEqual(self.diary.standing(), [])

    def test_the_register_is_bounded(self):
        self.diary.max_items = 2
        self.diary.add("One", "14 Oct")
        self.diary.add("Two", "15 Oct")
        with self.assertRaises(d.Refused) as caught:
            self.diary.add("Three", "16 Oct")
        self.assertIn("before adding another", str(caught.exception))


class TestSpeakingWhenItComesRound(Base):

    def fire(self, now):
        self.diary.clock = lambda: now
        return self.diary.tick(now)

    def test_nothing_happens_before_the_moment(self):
        self.diary.add("Dentist", "14 Oct at 9am")
        self.fire(at(2026, 10, 13, 23, 0))
        self.assertEqual(self.sent, [])

    def test_it_speaks_when_the_moment_arrives(self):
        self.diary.add("Dentist", "14 Oct at 9am")
        self.fire(at(2026, 10, 14, 8, 59))       # the loop, looking as it does
        report = self.fire(at(2026, 10, 14, 9, 0))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["subject"], "Dentist")
        self.assertIn("Due now", self.sent[0]["body"])
        self.assertFalse(report["sent"][0]["late"])

    def test_it_does_not_say_the_same_thing_twice(self):
        self.diary.add("Dentist", "14 Oct at 9am")
        self.fire(at(2026, 10, 14, 9, 0))
        self.fire(at(2026, 10, 14, 9, 1))
        self.fire(at(2026, 10, 14, 10, 0))
        self.assertEqual(len(self.sent), 1)

    def test_a_lead_speaks_early_and_says_how_early(self):
        self.diary.add("MOT due", "14 Oct at 9am", remind_before=["3 days", 0])
        self.fire(at(2026, 10, 11, 9, 0))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("in 3 days", self.sent[0]["body"])
        self.fire(at(2026, 10, 14, 9, 0))
        self.assertEqual(len(self.sent), 2)
        self.assertIn("Due now", self.sent[1]["body"])

    def test_the_two_reminders_are_not_deduped_against_each_other(self):
        """They share a subject, and the channel dedupes by key for six
        hours. Keyed by commitment alone, the reminder on the day would be
        swallowed by the one three days earlier."""
        self.diary.add("MOT due", "14 Oct at 9am", remind_before=["3 days", 0])
        self.fire(at(2026, 10, 11, 9, 0))
        self.fire(at(2026, 10, 14, 9, 0))
        self.assertNotEqual(self.sent[0]["key"], self.sent[1]["key"])

    def test_a_one_off_is_finished_once_it_has_been_said(self):
        item = self.diary.add("Dentist", "14 Oct at 9am")
        self.fire(at(2026, 10, 14, 9, 0))
        self.assertEqual(item.state, d.DONE)
        self.assertEqual(self.diary.standing(), [])

    def test_marking_it_dealt_with_stops_it_speaking(self):
        item = self.diary.add("Dentist", "14 Oct at 9am")
        self.assertTrue(self.diary.done(item.id))
        self.fire(at(2026, 10, 14, 9, 0))
        self.assertEqual(self.sent, [])


class TestAHeldMessageIsNotADeliveredOne(Base):
    """The channel has quiet hours, an hourly cap and a gap between messages,
    and ``send`` reports rather than raises. Marking a reminder done because
    send returned is how a 9am reminder becomes silently nothing."""

    def fire(self, now):
        self.diary.clock = lambda: now
        return self.diary.tick(now)

    def test_a_held_reminder_is_not_marked_said(self):
        item = self.diary.add("Dentist", "14 Oct at 9am")
        self.agent.notifier.hold = "quiet hours 22:00-07:00 Europe/London"
        report = self.fire(at(2026, 10, 14, 9, 0))
        self.assertEqual(report["sent"], [])
        self.assertIn("quiet hours", report["held"][0]["reason"])
        self.assertFalse(item.spoken_for(0.0))
        self.assertEqual(item.state, d.STANDING)

    def test_it_tries_again_and_lands(self):
        self.diary.add("Dentist", "14 Oct at 9am")
        self.agent.notifier.hold = "hourly cap reached (4)"
        self.fire(at(2026, 10, 14, 9, 0))
        self.agent.notifier.hold = None
        self.fire(at(2026, 10, 14, 9, 30))
        self.assertEqual(len(self.sent), 1)

    def test_a_reminder_that_never_lands_is_recorded_as_unsaid(self):
        """Not retried forever and not quietly forgotten: he can see that the
        register tried and failed, which is the only honest third option."""
        item = self.diary.add("Dentist", "14 Oct at 9am")
        self.agent.notifier.hold = "no destination configured"
        self.fire(at(2026, 10, 14, 9, 0))
        report = self.fire(at(2026, 10, 18, 9, 0))
        self.assertEqual(report["unsaid"][0]["what"], "Dentist")
        self.assertIn("no destination", item.fired["0.0"]["why"])
        self.assertIn("never delivered", " ".join(self.agent.notes))

    def test_no_channel_at_all_is_a_reason_not_a_crash(self):
        self.agent.notifier = None
        self.diary.add("Dentist", "14 Oct at 9am")
        report = self.fire(at(2026, 10, 14, 9, 0))
        self.assertIn("no channel", report["held"][0]["reason"])


class TestBeingLateAboutIt(Base):
    """Nothing running when the moment came round is a different apology from
    the channel holding it, and the heartbeat is what tells them apart."""

    def fire(self, now):
        self.diary.clock = lambda: now
        return self.diary.tick(now)

    def test_a_moment_missed_while_down_says_so(self):
        self.diary.add("Dentist", "14 Oct at 9am")
        self.fire(at(2026, 10, 13, 9, 0))        # last seen the day before
        report = self.fire(at(2026, 10, 16, 9, 0))
        self.assertTrue(report["sent"][0]["late"])
        self.assertIn("Nothing was running", self.sent[0]["body"])
        self.assertIn("2 days ago", self.sent[0]["body"])

    def test_a_moment_reached_while_up_is_not_late(self):
        self.diary.add("Dentist", "14 Oct at 9am")
        self.fire(at(2026, 10, 14, 8, 59))
        report = self.fire(at(2026, 10, 14, 9, 0))
        self.assertFalse(report["sent"][0]["late"])
        self.assertNotIn("Nothing was running", self.sent[0]["body"])

    def test_the_heartbeat_survives_a_restart(self):
        self.diary.add("Dentist", "14 Oct at 9am")
        self.fire(at(2026, 10, 14, 8, 59))
        again = d.Diary(self.agent, {"timezone": TZ},
                        state_file=self.diary.state_file)
        self.assertEqual(again.last_tick, at(2026, 10, 14, 8, 59))

    def test_three_leads_at_once_is_one_message_not_three(self):
        """A week of downtime past two reminders and the due date. Three
        messages arriving together is not three reminders, it is noise."""
        self.diary.add("MOT due", "14 Oct at 9am",
                       remind_before=["7 days", "3 days", 0])
        report = self.fire(at(2026, 10, 14, 12, 0))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(report["passed"]), 2)
        self.assertIn("Was due", self.sent[0]["body"])
        item = self.diary.items[0]
        self.assertEqual(item.fired[str(7 * 86400.0)]["how"], "passed")
        self.assertIn("covers it", item.fired[str(7 * 86400.0)]["why"])


class TestRepeating(Base):

    def fire(self, now):
        self.diary.clock = lambda: now
        return self.diary.tick(now)

    def test_a_daily_one_rolls_to_tomorrow(self):
        item = self.diary.add("Take the tablets", "tomorrow 8am", repeat="daily")
        self.fire(at(2026, 9, 22, 8, 0))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(item.due_local, "2026-09-23T08:00")
        self.fire(at(2026, 9, 23, 8, 0))
        self.assertEqual(len(self.sent), 2)

    def test_a_month_of_downtime_is_one_message_not_thirty(self):
        item = self.diary.add("Take the tablets", "tomorrow 8am", repeat="daily")
        self.fire(at(2026, 10, 22, 8, 0))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(item.missed, 30)
        self.assertIn("30 earlier ones went by unsaid", self.sent[0]["body"])
        self.assertEqual(item.due_local, "2026-10-23T08:00")

    def test_catching_up_speaks_about_today_not_about_a_month_ago(self):
        """One message is right and a message about a morning four weeks gone
        is not. It is this morning's tablets that still need taking."""
        self.diary.add("Take the tablets", "tomorrow 8am", repeat="daily")
        self.fire(at(2026, 10, 22, 8, 0))
        self.assertIn("22 October", self.sent[0]["body"])
        self.assertNotIn("September", self.sent[0]["body"])

    def test_a_monthly_one_anchored_late_in_the_month_is_clamped_and_says_so(self):
        item = self.diary.add("Rent", "31 October 2026", repeat="monthly")
        self.fire(at(2026, 10, 31, 9, 1))
        self.assertEqual(item.due_local, "2026-11-30T09:00")
        self.assertIn("which this month does not have", item.note)

    def test_the_clamp_does_not_drift(self):
        """Rolling from the clamped date rather than the anchor is how the
        31st becomes the 28th and then stays there for good."""
        item = self.diary.add("Rent", "31 December 2026", repeat="monthly")
        self.fire(at(2026, 12, 31, 9, 1))
        self.assertEqual(item.due_local, "2027-01-31T09:00")
        self.fire(at(2027, 1, 31, 9, 1))
        self.assertEqual(item.due_local, "2027-02-28T09:00")
        self.fire(at(2027, 2, 28, 9, 1))
        self.assertEqual(item.due_local, "2027-03-31T09:00")

    def test_its_identity_survives_the_roll(self):
        """Keyed on the occurrence in front, a monthly commitment would be a
        new row every month and nothing he had said about it would follow."""
        item = self.diary.add("Rent", "1 October 2026", repeat="monthly")
        before = item.id
        self.fire(at(2026, 10, 1, 9, 1))
        self.assertEqual(item.id, before)
        self.assertEqual(item.due_local, "2026-11-01T09:00")

    def test_a_yearly_one_keeps_its_date(self):
        item = self.diary.add("Car insurance renews", "5 December 2026",
                              repeat="yearly")
        self.fire(at(2026, 12, 5, 9, 1))
        self.assertEqual(item.due_local, "2027-12-05T09:00")


@unittest.skipUnless(HAVE_TZ, "no tzdata on this machine")
class TestNineInTheMorningStaysNineInTheMorning(Base):
    """The clocks change. An instant computed once does not, which is how a
    9am reminder becomes an 8am one at the end of October."""

    def test_the_instant_moves_with_the_zone(self):
        item = self.diary.add("Take the tablets", "tomorrow 9am", repeat="daily")
        summer = item.due_at()
        item.due_local = "2026-11-10T09:00"
        winter = item.due_at()
        self.assertEqual(
            datetime.fromtimestamp(summer, timesense.zone(TZ)).hour, 9)
        self.assertEqual(
            datetime.fromtimestamp(winter, timesense.zone(TZ)).hour, 9)
        # An hour more than a whole number of days apart, because one of them
        # is BST and the other is not.
        self.assertNotEqual((winter - summer) % 86400, 0)

    def test_it_fires_at_nine_on_the_other_side_of_the_change(self):
        self.diary.add("Take the tablets", "10 November 2026 9am")
        self.diary.clock = lambda: at(2026, 11, 10, 8, 59)
        self.diary.tick(at(2026, 11, 10, 8, 59))
        self.assertEqual(self.sent, [])
        self.diary.tick(at(2026, 11, 10, 9, 0))
        self.assertEqual(len(self.sent), 1)


class TestReadingItOffAPage(Base):
    """A date next to a due-ish word, ahead of now, at most two, and even
    then only a suggestion. A register that fills itself is one nobody
    trusts, and the first wrong 7am reminder teaches him to ignore the next
    right one."""

    BILL = ("British Gas. Amount due 92.50 by 14 Oct 2026. Statement period "
            "2026-08-01 to 31 Aug 2026. Contract renews 3 November 2026.")

    def test_a_due_date_is_suggested(self):
        made = self.diary.suggest_from_document("british-gas.pdf", self.BILL)
        self.assertTrue(made)
        self.assertIn("14 Oct", made[0].note or made[0].what + " ")
        self.assertEqual(made[0].due_local[:10], "2026-10-14")

    def test_a_suggestion_fires_nothing_until_he_says(self):
        made = self.diary.suggest_from_document("british-gas.pdf", self.BILL)
        self.assertEqual(made[0].state, d.PROPOSED)
        self.diary.clock = lambda: at(2026, 10, 14, 9, 0)
        self.diary.tick(at(2026, 10, 14, 9, 0))
        self.assertEqual(self.sent, [])

    def test_confirming_it_makes_it_stand(self):
        made = self.diary.suggest_from_document("british-gas.pdf", self.BILL)
        self.assertIsNotNone(self.diary.confirm(made[0].id))
        self.diary.clock = lambda: at(2026, 10, 14, 9, 0)
        self.diary.tick(at(2026, 10, 14, 9, 0))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("british-gas.pdf", self.sent[0]["body"])

    def test_a_statement_period_does_not_become_a_reminder(self):
        """The commonest pair of dates on a bill, and the one that would turn
        a useful register into a muted one."""
        made = self.diary.suggest_from_document("british-gas.pdf", self.BILL)
        self.assertFalse(any("Statement period" in c.what for c in made))

    def test_dates_already_gone_are_not_suggested(self):
        made = self.diary.suggest_from_document(
            "old.pdf", "Payment due by 14 August 2026.")
        self.assertEqual(made, [])

    def test_a_date_with_no_reason_beside_it_is_left_alone(self):
        made = self.diary.suggest_from_document(
            "note.txt", "We met on 14 October 2026 and talked about the roof.")
        self.assertEqual(made, [])

    def test_it_does_not_propose_a_page_full_of_dates(self):
        page = " ".join(f"Payment due by {day} December 2026."
                        for day in range(1, 12))
        self.assertLessEqual(len(self.diary.suggest_from_document("many.pdf", page)), 2)


class TestItSurvivesARestart(unittest.TestCase):
    """The commitments go in the memory database rather than a file of their
    own, because that database is the one thing copied off the box."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        import logging
        self.store = build_store({"path": os.path.join(self.tmp, "m.db")},
                                 logging.getLogger("test"))
        self.agent = FakeAgent(self.store)

    def tearDown(self):
        self.store.close()

    def diary(self):
        made = d.Diary(self.agent, {"timezone": TZ}, clock=lambda: NOW,
                       state_file=os.path.join(self.tmp, "diary.state"))
        made.load()
        return made

    def test_a_commitment_comes_back(self):
        first = self.diary()
        item = first.add("Pay the British Gas bill", "14 Oct at 9am",
                         remind_before=["3 days", 0])
        again = self.diary()
        self.assertEqual(len(again.standing()), 1)
        back = again.standing()[0]
        self.assertEqual(back.id, item.id)
        self.assertEqual(back.due_local, "2026-10-14T09:00")
        self.assertEqual(back.leads, (259200.0, 0.0))

    def test_what_was_already_said_is_not_said_again_after_a_restart(self):
        first = self.diary()
        first.add("Dentist", "14 Oct at 9am")
        first.clock = lambda: at(2026, 10, 14, 9, 0)
        first.tick(at(2026, 10, 14, 9, 0))
        self.assertEqual(len(self.agent.notifier.sent), 1)
        again = self.diary()
        again.tick(at(2026, 10, 14, 9, 30))
        self.assertEqual(len(self.agent.notifier.sent), 1)

    def test_a_dropped_one_stays_dropped(self):
        first = self.diary()
        item = first.add("Dentist", "14 Oct at 9am")
        first.drop(item.id)
        self.assertEqual(self.diary().standing(), [])

    def test_it_goes_in_where_the_backup_will_find_it(self):
        self.diary().add("Dentist", "14 Oct at 9am")
        rows = self.store.recent(10, kind="commitment")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["pinned"])
        self.assertTrue(rows[0]["meta"]["commitment"])

    def test_no_durable_store_is_said_rather_than_pretended(self):
        agent = FakeAgent()
        made = d.Diary(agent, {"timezone": TZ}, clock=lambda: NOW,
                       state_file=os.path.join(self.tmp, "d.state"))
        made.add("Dentist", "14 Oct at 9am")
        self.assertFalse(made.state()["durable"])


class TestWhatTheModelIsTold(Base):

    def test_an_empty_diary_says_nothing(self):
        self.assertIsNone(self.diary.context())

    def test_what_is_coming_is_context_not_work(self):
        self.diary.add("Dentist", "in 3 days")
        got = self.diary.context()
        self.assertTrue(got["in_the_next_fortnight"])
        self.assertIn("not your tasks", got["how_to_use_this"])
        self.assertIn("you may not confirm one", got["how_to_use_this"])

    def test_something_far_out_is_not_in_the_fortnight(self):
        self.diary.add("MOT", "14 Oct")
        self.assertIsNone(self.diary.context())

    def test_a_suggestion_waiting_on_him_is_shown_as_waiting(self):
        self.diary.suggest("Something in bill.pdf", "in 2 days", origin="bill.pdf")
        got = self.diary.context()
        self.assertTrue(got["you_suggested_these_and_he_has_not_ruled"])
        self.assertNotIn("in_the_next_fortnight", got)

    def test_the_state_says_when_it_last_looked(self):
        self.assertEqual(self.diary.state()["last_checked"], "never")
        self.diary.tick(NOW)
        self.assertNotEqual(self.diary.state()["last_checked"], "never")


class TestItIsActuallyWiredIn(unittest.TestCase):
    """A register nothing calls is a register that does not exist. These are
    about the seams: the agent building one, a document reaching it, the
    status file carrying it, and the model being told."""

    def agent(self, **cfg):
        import logging
        from jarvis.agent.core import AgentCore
        settings = {"name": "Jarvis", "profile": "cloud", "diary": {"timezone": TZ}}
        settings.update(cfg)
        return AgentCore(settings, {}, logging.getLogger("test"))

    def test_the_agent_has_one(self):
        agent = self.agent()
        self.assertTrue(hasattr(agent, "diary"))
        self.assertEqual(agent.diary.standing(), [])

    def test_the_status_carries_a_summary_not_the_register(self):
        """The status file is written every cycle. Eighty commitments in it
        is a file that stops being read."""
        agent = self.agent()
        agent.diary.add("Dentist", "14 Oct 2027 at 9am")
        summary = agent.get_status()["diary"]
        self.assertEqual(summary["standing"], 1)
        self.assertIn("Dentist", summary["next"])
        self.assertNotIn("facts", summary)

    def test_a_document_it_reads_reaches_the_diary(self):
        """The whole point of the exercise: a date it read is still there
        tomorrow, as something he can rule on."""
        import os
        import tempfile
        from jarvis.agent.planner import Task, TaskType
        agent = self.agent()
        path = os.path.join(tempfile.mkdtemp(), "bill.txt")
        with open(path, "w") as fh:
            fh.write("British Gas. Amount due 92.50 by 14 October 2027.\n")
        agent.record_upload("bill.txt", path, os.path.getsize(path))
        task = Task(description="read it", task_type=TaskType.READ_FILE, priority=5,
                    metadata={"path": path})
        agent.act(task)
        waiting = agent.diary.proposed()
        self.assertEqual(len(waiting), 1)
        self.assertEqual(waiting[0].due_local[:10], "2027-10-14")

    def test_a_document_with_no_dates_leaves_the_diary_alone(self):
        import os
        import tempfile
        from jarvis.agent.planner import Task, TaskType
        agent = self.agent()
        path = os.path.join(tempfile.mkdtemp(), "plain.txt")
        with open(path, "w") as fh:
            fh.write("just some words\n")
        agent.act(Task(description="read it", task_type=TaskType.READ_FILE, priority=5,
                       metadata={"path": path}))
        self.assertEqual(agent.diary.items, [])

    def test_a_file_he_never_handed_over_is_not_read_for_commitments(self):
        """It reads config files, logs and certificates too, and those are
        full of dates beside words like "expires". None of them is his."""
        import os
        import tempfile
        from jarvis.agent.planner import Task, TaskType
        agent = self.agent()
        path = os.path.join(tempfile.mkdtemp(), "some.conf")
        with open(path, "w") as fh:
            fh.write("certificate expires 14 October 2027\n")
        agent.act(Task(description="read it", task_type=TaskType.READ_FILE,
                       priority=5, metadata={"path": path}))
        self.assertEqual(agent.diary.items, [])

    def test_the_model_is_told_what_is_coming(self):
        agent = self.agent()
        agent.diary.add("Dentist", "in 3 days")
        self.assertTrue(agent.diary.context()["in_the_next_fortnight"])


if __name__ == "__main__":
    unittest.main()
