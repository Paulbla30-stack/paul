"""Tests for the fault register.

A failure rate is a number to feel bad about. A fault profile is something to
reason with. Every fault class declared has a real incident behind it in this
project's history, so these tests are written from the shape of what actually
happened rather than from an invented one.

Two invariants matter more than any detector. The profile reports aggregates
and never reproduces a ledger entry, because a record the subject can
reconstruct is a record the subject can manage. And it states facts rather
than giving advice, because a rule is obeyed on the day the thing was
warranted and a fact is weighed.
"""

import logging
import unittest

from jarvis.agent import faults

LOG = logging.getLogger("test")
NOW = 1_800_000_000.0


def stream(*entries):
    """(kind, ts, body) triples, as selfknowledge yields them."""
    return list(entries)


def scan(*entries, decisions=0, now=NOW):
    return faults.scan(stream(*entries), now=now, decisions=decisions)


def named(reports, name):
    return next((r for r in reports if r["fault"] == name), None)


class TestTheDetectorsFindTheirRealIncident(unittest.TestCase):
    def test_invented_path(self):
        """The planner wrote a check against a log directory that never existed."""
        got = named(scan(("alert", NOW - 60, {"alert": "unverified_path",
                                              "path": "/var/log/jarvis/scan.log"})),
                    "invented_path")
        self.assertIsNotNone(got)
        self.assertEqual(got["times"], 1)

    def test_repeat_loop(self):
        """95 of the first 97 gates were brain_repeat."""
        got = named(scan(*[("gate", NOW - i, {"gate": "brain_repeat"}) for i in range(95)]),
                    "repeat_loop")
        self.assertEqual(got["times"], 95)

    def test_refusal_rephrased(self):
        """Denied a redirect into /proc/sys, it came back with sysctl -w."""
        got = named(scan(("gate", NOW - 120, {"gate": "shell_policy"}),
                         ("action", NOW - 60, {"type": "shell_command"})),
                    "refusal_rephrased")
        self.assertIsNotNone(got)
        self.assertEqual(got["times"], 1)

    def test_a_command_long_after_a_refusal_is_not_a_rephrase(self):
        self.assertIsNone(named(scan(
            ("gate", NOW - 5000, {"gate": "shell_policy"}),
            ("action", NOW - 60, {"type": "shell_command"})), "refusal_rephrased"))

    def test_one_refusal_is_not_counted_twice(self):
        got = named(scan(("gate", NOW - 150, {"gate": "shell_policy"}),
                         ("action", NOW - 100, {"type": "shell_command"}),
                         ("action", NOW - 50, {"type": "shell_command"})),
                    "refusal_rephrased")
        self.assertEqual(got["times"], 1, "the second command follows no new refusal")

    def test_goal_treadmill(self):
        """1,293 of 1,558 actions were steps against a goal it could not see."""
        got = named(scan(*[("action", NOW - i, {"type": "goal_step",
                                                "task": {"goal": "keep root under 80%"}})
                           for i in range(40)]), "goal_treadmill")
        self.assertIsNotNone(got)
        # One standing condition on one goal, not forty events.
        self.assertEqual(got["times"], 1)
        self.assertEqual(got["unit"], "goals")

    def test_a_few_goal_steps_are_just_work(self):
        self.assertIsNone(named(scan(*[
            ("action", NOW - i, {"type": "goal_step", "task": {"goal": "g"}})
            for i in range(5)]), "goal_treadmill"))

    def test_barren_check(self):
        got = named(scan(*[("outcome", NOW - i, {"type": "security_scan",
                                                 "output": {"bytes": 10}})
                           for i in range(20)]), "barren_check")
        self.assertIsNotNone(got)

    def test_a_check_that_finds_things_is_not_barren(self):
        self.assertIsNone(named(scan(*[
            ("outcome", NOW - i, {"type": "security_scan", "output": {"bytes": 900}})
            for i in range(20)]), "barren_check"))

    def test_a_few_empty_runs_are_not_a_fault(self):
        self.assertIsNone(named(scan(*[
            ("outcome", NOW - i, {"type": "system_check", "output": {"bytes": 4}})
            for i in range(5)]), "barren_check"))

    def test_a_clean_record_reports_nothing(self):
        self.assertEqual(scan(("decision", NOW, {}), ("outcome", NOW, {"success": True})), [])


class TestItReportsAggregatesNotEntries(unittest.TestCase):
    """A record the subject can reconstruct is a record the subject can manage."""

    def test_no_entry_content_survives(self):
        secret = "the operator said something private in here"
        reports = scan(("alert", NOW - 10, {"alert": "unverified_path",
                                            "path": secret, "note": secret}),
                       ("gate", NOW - 10, {"gate": "brain_repeat", "detail": secret}))
        blob = str(reports)
        self.assertNotIn(secret, blob)
        self.assertNotIn("operator said", blob)

    def test_reports_carry_only_counts_and_time(self):
        for r in scan(("gate", NOW - 10, {"gate": "brain_repeat"})):
            self.assertEqual(set(r) - {"fault", "what", "times", "unit",
                                       "last_seen_s_ago", "recent",
                                       "per_hundred_decisions"}, set())

    def test_a_label_is_bounded(self):
        f = faults.Fault("x", "y", "z")
        f.note(NOW, "q" * 500)
        self.assertLessEqual(len(f.sightings[0].label), 80)


class TestItStatesFactsAndNotRules(unittest.TestCase):
    """A rule is obeyed on the day the thing was warranted; a fact is weighed."""

    def all_lines(self):
        return faults.lines(scan(
            ("alert", NOW - 60, {"alert": "unverified_path"}),
            ("gate", NOW - 120, {"gate": "brain_repeat"}),
            ("gate", NOW - 300, {"gate": "shell_policy"}),
            ("action", NOW - 250, {"type": "shell_command"}),
            *[("action", NOW - i, {"type": "goal_step", "task": {"goal": "g"}})
              for i in range(30)],
            decisions=200))

    def test_no_line_tells_it_what_to_do(self):
        # Imperative constructions only. Bare "never" and "always" appear
        # descriptively ("evidence never moves") and banning them would ban
        # plain English rather than advice.
        forbidden = ("you should", "you must", "you need to", "do not ", "don't ",
                     "make sure", "remember to", "stop doing", "stop running",
                     "avoid ", "ensure ")
        for line in self.all_lines():
            low = line.lower()
            for word in forbidden:
                self.assertNotIn(word, low, f"advice leaked into: {line}")

    def test_every_line_carries_a_count(self):
        for line in self.all_lines():
            self.assertRegex(line, r"(\d+x|[Oo]n \d+ \w+) in this window")

    def test_a_single_occurrence_reads_as_singular(self):
        line = faults.lines(scan(*[
            ("action", NOW - i, {"type": "goal_step", "task": {"goal": "g"}})
            for i in range(30)]))[0]
        self.assertIn("On 1 goal in this window", line)
        self.assertNotIn("1 goals", line)

    def test_every_line_says_when(self):
        for line in self.all_lines():
            self.assertTrue("last just now" in line or "ago" in line, line)

    def test_a_rate_is_given_when_there_is_a_denominator(self):
        lines = faults.lines(scan(*[("gate", NOW - i, {"gate": "brain_repeat"})
                                    for i in range(20)], decisions=100))
        self.assertIn("per 100 decisions", lines[0])

    def test_no_rate_is_invented_without_one(self):
        lines = faults.lines(scan(("gate", NOW - 5, {"gate": "brain_repeat"}), decisions=0))
        self.assertNotIn("per 100", lines[0])


class TestItFailsSoft(unittest.TestCase):
    """A broken detector costs one signal, never the agent's ability to think."""

    def test_a_detector_that_raises_does_not_stop_the_scan(self):
        reg = faults.register()

        def boom(f, kind, ts, body):
            raise RuntimeError("detector is broken")

        reg["repeat_loop"].detect = boom
        original = faults.register
        faults.register = lambda: reg
        try:
            reports = faults.scan(stream(("alert", NOW - 5, {"alert": "unverified_path"})),
                                  now=NOW)
        finally:
            faults.register = original
        self.assertTrue(any(r["fault"] == "invented_path" for r in reports))

    def test_rubbish_entries_are_survivable(self):
        faults.scan(stream((None, NOW, None), ("gate", "not a time", {}),
                           ("action", NOW, "not a dict")), now=NOW)

    def test_an_empty_stream_is_fine(self):
        self.assertEqual(faults.scan([], now=NOW), [])


class TestOrdering(unittest.TestCase):
    def test_the_most_recent_fault_comes_first(self):
        reports = scan(("gate", NOW - 40_000, {"gate": "brain_repeat"}),
                       ("alert", NOW - 30, {"alert": "unverified_path"}))
        self.assertEqual(reports[0]["fault"], "invented_path")

    def test_recent_window_is_counted_separately(self):
        r = named(scan(("gate", NOW - 60, {"gate": "brain_repeat"}),
                       ("gate", NOW - 40 * 3600, {"gate": "brain_repeat"})), "repeat_loop")
        self.assertEqual(r["times"], 2)
        self.assertEqual(r["recent"], 1)


class TestEveryFaultIsGroundedInSomethingThatHappened(unittest.TestCase):
    def test_each_one_names_its_incident(self):
        for name, f in faults.register().items():
            self.assertTrue(len(f.incident) > 60,
                            f"{name} has no real incident behind it")
            self.assertTrue(f.summary.endswith("."), name)

    def test_the_summaries_are_about_the_agent_not_the_system(self):
        for f in faults.register().values():
            self.assertTrue(f.summary.startswith("You "),
                            f"{f.name}: a fault is something it did")


if __name__ == "__main__":
    unittest.main()
