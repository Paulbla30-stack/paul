"""Time: the present, measured durations, and dates written on a page.

The failure this is against is specific and repeatable. A model's sense of
how long something takes comes from text about people doing things, and that
text contains no entries at all for "forty milliseconds" -- so asked how long
a check takes, it answers in the units of a human project. It will say a week
for something the machine finishes in a second, not because it is guessing
badly but because it is guessing from the wrong distribution.

So the tests here are mostly about refusing to round the machine scale away,
and about a date on a page being useless until something turns it into a
distance from today.
"""

import time
import unittest

from jarvis.agent import timesense as t

# A Sunday night, British Summer Time, so UTC and the operator differ.
NOW = time.mktime(time.strptime("2026-09-20 22:30", "%Y-%m-%d %H:%M"))


class TestItDoesNotRoundTheMachineScaleAway(unittest.TestCase):
    """"Less than a second" is where four orders of magnitude go to die."""

    def test_milliseconds_are_said_in_milliseconds(self):
        self.assertEqual(t.phrase(0.011), "11ms")
        self.assertEqual(t.phrase(0.38), "380ms")
        self.assertEqual(t.phrase(0.0004), "0ms")

    def test_seconds_keep_a_decimal_while_it_matters(self):
        self.assertEqual(t.phrase(2.4), "2.4s")
        self.assertEqual(t.phrase(42), "42s")

    def test_longer_spans_read_like_a_person_wrote_them(self):
        self.assertEqual(t.phrase(600), "10 minutes")
        self.assertEqual(t.phrase(7200), "2 hours")
        self.assertEqual(t.phrase(259_200), "3 days")
        self.assertEqual(t.phrase(1_814_400), "3 weeks")

    def test_a_gap_says_which_side_of_now_it_is_on(self):
        self.assertEqual(t.gap(NOW - 60, NOW), "60s ago")
        self.assertEqual(t.gap(NOW + 3600, NOW), "in 60 minutes")
        self.assertEqual(t.gap(NOW, NOW), "just now")

    def test_the_bands_are_orders_of_magnitude_apart(self):
        self.assertEqual(t.band_of(0.04), t.MACHINE)
        self.assertEqual(t.band_of(30), t.SERVICE)
        self.assertEqual(t.band_of(3600), t.PERSON)
        self.assertEqual(t.band_of(5 * 86400), t.WORLD)


class TestTheDurationsAreMeasuredNotEstimated(unittest.TestCase):
    """The whole point: a fact with observations behind it beats a prior."""

    def history(self):
        rows, ts = [], NOW - 500
        for kind, took in (("system_check", 0.011), ("security_scan", 0.38),
                           ("estate_report", 2.4)) * 4:
            rows.append({"task": {"type": kind}, "timestamp": ts, "took_s": took})
            ts += 45          # the cycle interval, which is not the duration
        return rows

    def test_it_reports_what_actually_happened(self):
        got = t.durations(self.history(), NOW)
        self.assertEqual(got["system_check"]["typical"], "11ms")
        self.assertEqual(got["security_scan"]["typical"], "380ms")
        self.assertEqual(got["estate_report"]["band"], t.MACHINE)

    def test_the_gap_between_entries_is_not_the_duration(self):
        """It was being used as one. On an idle loop the gap is the cycle
        interval, so a goal_step writing one boolean read as 45 seconds --
        five orders of magnitude out, offered with the authority of a
        measurement."""
        got = t.durations(self.history(), NOW)
        self.assertEqual(got["system_check"]["typical"], "11ms")
        self.assertNotIn("45", got["system_check"]["typical"])

    def test_an_entry_with_no_recorded_time_is_skipped_not_estimated(self):
        rows = [{"task": {"type": "x"}, "timestamp": NOW - 1},
                {"task": {"type": "x"}, "timestamp": NOW}]
        self.assertEqual(t.durations(rows, NOW), {})

    def test_one_observation_is_not_a_measurement(self):
        rows = [{"task": {"type": "x"}, "timestamp": NOW, "took_s": 0.1}]
        self.assertEqual(t.durations(rows, NOW), {})

    def test_the_scale_note_carries_the_measured_numbers(self):
        blob = " ".join(t.scale(t.durations(self.history(), NOW)))
        self.assertIn("11ms", blob)
        self.assertIn("observations, not estimates", blob)

    def test_it_says_to_name_a_scale_rather_than_invent_a_number(self):
        blob = " ".join(t.scale())
        self.assertIn("say which scale it is on", blob)
        self.assertIn("About a week", blob)

    def test_an_empty_record_is_survivable(self):
        self.assertEqual(t.durations([], NOW), {})
        self.assertEqual(t.durations(None, NOW), {})


class TestKnowingWhatDayItIs(unittest.TestCase):

    def test_it_knows_the_operators_clock_is_not_the_machines(self):
        got = t.present(NOW, quiet_hours=[22, 7])
        self.assertIn("UTC", got["utc"])
        self.assertIn("Sunday", got["operator_local"])
        self.assertTrue(got["weekend"])

    def test_it_knows_when_the_operator_is_asleep(self):
        late = t.present(NOW, quiet_hours=[22, 7])
        self.assertTrue(late["operator_probably_asleep"])
        self.assertIn("held until the morning", late["what_that_means"])
        midday = t.present(NOW - 12 * 3600, quiet_hours=[22, 7])
        self.assertFalse(midday["operator_probably_asleep"])

    def test_a_timezone_difference_is_stated_rather_than_assumed(self):
        got = t.present(NOW)
        if got["offset_from_utc_hours"]:
            self.assertIn("which one it is", got["note"])

    def test_uptime_reads_in_human_units(self):
        self.assertEqual(t.present(NOW, started_at=NOW - 7200)["running_for"],
                         "2 hours")


class TestDatesOnAPage(unittest.TestCase):
    """A date is useless until it is a distance from today."""

    BILL = ("British Gas. Amount due 92.50 by 14 Oct. Statement period "
            "2026-08-01 to 31 Aug. Previous payment 05/09/2026. Contract "
            "renews 3 November 2026.")

    def test_every_written_form_is_found(self):
        """A year written out is the form a bill actually uses, and it was
        the form being silently dropped: the year arrives from the regex as a
        string, comparing it with an int raised TypeError, and the except
        swallowed it."""
        found = t.read_dates(self.BILL, NOW)
        written = {d["as_written"] for d in found}
        self.assertIn("14 Oct", written)
        self.assertIn("2026-08-01", written)
        self.assertIn("05/09/2026", written)
        self.assertIn("3 November 2026", written)

    def test_a_future_date_is_a_distance_forward(self):
        due = next(d for d in t.read_dates(self.BILL, NOW)
                   if d["as_written"] == "14 Oct")
        self.assertEqual(due["when"], "24 days from now")
        self.assertFalse(due["past"])

    def test_a_past_date_says_so(self):
        old = next(d for d in t.read_dates(self.BILL, NOW)
                   if d["as_written"] == "2026-08-01")
        self.assertTrue(old["past"])
        self.assertIn("ago", old["when"])

    def test_an_ambiguous_date_is_reported_not_resolved(self):
        """05/09 is 5 September here and 9 May elsewhere. Quietly choosing is
        how a payment gets missed."""
        got = next(d for d in t.read_dates(self.BILL, NOW)
                   if d["as_written"] == "05/09/2026")
        self.assertIn("ambiguous", got)
        self.assertIn("worth confirming", got["ambiguous"])

    def test_an_unambiguous_numeric_date_is_not_flagged(self):
        got = t.read_dates("due 25/12/2026", NOW)[0]
        self.assertNotIn("ambiguous", got)

    def test_a_date_with_no_year_is_read_as_this_year(self):
        self.assertTrue(t.read_dates("due 14 Oct", NOW)[0]["date"].startswith("2026"))

    def test_an_impossible_date_is_dropped_rather_than_guessed(self):
        self.assertEqual(t.read_dates("due 31 Feb 2026", NOW), [])

    def test_the_note_leads_with_what_is_coming(self):
        note = t.date_note(t.read_dates(self.BILL, NOW))
        self.assertIn("24 days from now", note)
        self.assertIn("already passed", note)
        self.assertIn("ambiguously", note)

    def test_no_dates_means_no_note(self):
        self.assertIsNone(t.date_note([]))
        self.assertEqual(t.read_dates("nothing dated here", NOW), [])


class TestItReachesTheAgent(unittest.TestCase):

    def test_a_document_it_reads_carries_its_dates(self):
        import os
        import tempfile
        from jarvis.agent import environment as env
        path = os.path.join(tempfile.mkdtemp(), "bill.txt")
        with open(path, "w") as f:
            f.write("Amount due 92.50 by 14 Oct 2026\n")
        got = env.read_file(path)
        self.assertTrue(got["dates_in_it"])
        self.assertIn("October", got["dates_note"])

    def test_a_document_with_no_dates_carries_none(self):
        import os
        import tempfile
        from jarvis.agent import environment as env
        path = os.path.join(tempfile.mkdtemp(), "plain.txt")
        with open(path, "w") as f:
            f.write("just some words\n")
        self.assertNotIn("dates_in_it", env.read_file(path))


if __name__ == "__main__":
    unittest.main()
