"""Verdicts: whether what it said was true, ruled on by something that is not it.

The load-bearing test in this file is the first one. The agent said "I used
read_logs to check the agent's journal and found no entries, as the log file
exists but is empty", in a conversation where no tool can run, about a file
it had not opened, while the journal had 53 lines. Every fault detector in
faults.py reads that record as clean, because nothing about its *shape* is
wrong. If this module cannot rule on that sentence, it is not worth having.

The rest guard the three rules. Provenance never collapses into a score.
Unchecked is never counted as passed. And a ruling can be revised on the
record without the earlier one being removed.
"""

import logging
import unittest

from jarvis.agent import verdicts

LOG = logging.getLogger("test")
NOW = 1_800_000_000.0

# The sentence this module exists for.
CONFABULATION = ("I used read_logs to check the agent's journal for the past hour "
                 "and found no entries, as the log file at /var/log/jarvis.log "
                 "exists but is empty. This aligns with the observation that the "
                 "system has been stable.")


class FakeAgent:
    def __init__(self, ran=(), goals=()):
        self.task_history = [
            {"task": {"type": name}, "result": {"success": True}, "timestamp": NOW - 60}
            for name in ran
        ]
        self.planner = type("P", (), {"goals": [{"description": g} for g in goals]})()


def rule(text, agent=None, observations=None, now=NOW):
    return verdicts.check(text, agent or FakeAgent(), observations, now=now)


def by_claim(rulings, needle):
    return [v for v in rulings if needle in v.claim]


class TestTheConfabulation(unittest.TestCase):
    """The one this module was built for."""

    def test_a_tool_it_did_not_run_is_ruled_false(self):
        got = by_claim(rule(CONFABULATION), "run read_logs")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].ruling, verdicts.FAILED)
        self.assertEqual(got[0].source, verdicts.MACHINE)
        self.assertIn("no read_logs task ran", got[0].reason)

    def test_a_tool_it_did_run_is_ruled_true(self):
        got = by_claim(rule(CONFABULATION, FakeAgent(ran=["read_logs"])), "run read_logs")
        self.assertEqual(got[0].ruling, verdicts.HELD)

    def test_a_tool_it_ran_yesterday_does_not_count_as_now(self):
        agent = FakeAgent(ran=["read_logs"])
        agent.task_history[0]["timestamp"] = NOW - 40 * 3600
        self.assertEqual(by_claim(rule(CONFABULATION, agent), "run read_logs")[0].ruling,
                         verdicts.FAILED)

    def test_a_word_that_is_not_a_tool_is_not_ruled_on(self):
        """"I ran the numbers" is not a claim about this system."""
        self.assertEqual(rule("I ran the numbers and checked the figures again."), [])

    def test_the_whole_sentence_produces_a_note_for_the_operator(self):
        note = verdicts.note(rule(CONFABULATION))
        self.assertIsNotNone(note)
        self.assertIn("read_logs", note)


class TestPathClaims(unittest.TestCase):
    """Not whether the path exists -- whether what was said about it was so."""

    def test_calling_a_file_empty_when_it_is_not(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            f.write("53 lines would go here\n")
            name = f.name
        got = by_claim(rule(f"The log at {name} exists but is empty."), "is empty")
        self.assertEqual(got[0].ruling, verdicts.FAILED)
        self.assertIn("bytes", got[0].reason)

    def test_calling_a_file_empty_when_it_is(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
            name = f.name
        self.assertEqual(by_claim(rule(f"{name} is empty."), "is empty")[0].ruling,
                         verdicts.HELD)

    def test_calling_a_file_empty_when_there_is_no_file(self):
        got = by_claim(rule("/var/log/jarvis/nothing-here.log is empty."), "is empty")
        self.assertEqual(got[0].ruling, verdicts.FAILED)
        self.assertIn("not there at all", got[0].reason)

    def test_saying_a_path_is_absent_when_it_is(self):
        got = by_claim(rule("/var/log/jarvis/scan.log does not exist on this machine."),
                       "not on this machine")
        self.assertEqual(got[0].ruling, verdicts.HELD)

    def test_saying_a_path_is_absent_when_it_is_there(self):
        got = by_claim(rule("/etc/hostname does not exist."), "not on this machine")
        self.assertEqual(got[0].ruling, verdicts.FAILED)

    def test_naming_a_missing_path_with_no_assertion_still_counts(self):
        got = by_claim(rule("The report is written to /var/log/jarvis/security_scan.log."),
                       "named a path")
        self.assertEqual(got[0].ruling, verdicts.FAILED)


class TestFigures(unittest.TestCase):
    """Substituting a remembered number for an unobserved one."""

    DISK = {"disk_usage": [{"mountpoint": "/", "use_percent": "24%"},
                           {"mountpoint": "/boot", "use_percent": "9%"}]}

    def test_a_figure_that_matches_the_reading(self):
        got = rule("The root filesystem is at 24% usage.", observations=self.DISK)
        self.assertEqual(got[0].ruling, verdicts.HELD)

    def test_rounding_is_not_substitution(self):
        obs = {"disk_usage": [{"mountpoint": "/", "use_percent": "24.3%"}]}
        self.assertEqual(rule("root is at 24%", observations=obs)[0].ruling, verdicts.HELD)

    def test_a_figure_that_does_not(self):
        got = rule("The root filesystem is at 91% usage.", observations=self.DISK)
        self.assertEqual(got[0].ruling, verdicts.FAILED)
        self.assertIn("the reading is 24%", got[0].reason)

    def test_restating_a_goal_is_not_claiming_a_reading(self):
        """"Keep root under 80%" is not a claim that root is at 80%."""
        agent = FakeAgent(goals=["keep the root filesystem under 80% used"])
        self.assertEqual(
            rule("My goal is to keep the root filesystem under 80% used.",
                 agent=agent, observations=self.DISK), [])

    def test_no_reading_means_no_verdict_rather_than_a_guess(self):
        self.assertEqual(rule("root is at 91%", observations={}), [])


class TestProvenanceNeverCollapses(unittest.TestCase):
    """Three sources, three questions, three lines. Never one score."""

    def stream(self):
        return [
            ("verdict", NOW - 60, {"source": "machine", "claim": "a", "ruling": "held"}),
            ("verdict", NOW - 60, {"source": "machine", "claim": "b", "ruling": "failed"}),
            ("verdict", NOW - 90, {"source": "machine", "claim": "c", "ruling": "unchecked"}),
            ("verdict", NOW - 300, {"source": "operator", "claim": "d", "ruling": "failed",
                                    "by": "paul"}),
            ("verdict", NOW - 400, {"source": "review", "claim": "e", "ruling": "held",
                                    "by": "claude"}),
        ]

    def test_each_source_is_counted_apart(self):
        got = verdicts.scan(self.stream(), now=NOW)
        self.assertEqual(got["machine"]["held"], 1)
        self.assertEqual(got["machine"]["failed"], 1)
        self.assertEqual(got["operator"]["failed"], 1)
        self.assertEqual(got["review"]["held"], 1)

    def test_no_line_merges_the_sources(self):
        lines = verdicts.lines(verdicts.scan(self.stream(), now=NOW))
        self.assertEqual(len(lines), 3)
        self.assertTrue(any("This machine" in l for l in lines))
        self.assertTrue(any("Your operator" in l for l in lines))
        self.assertTrue(any("A second reader" in l for l in lines))

    def test_each_line_says_what_that_source_was_asking(self):
        lines = verdicts.lines(verdicts.scan(self.stream(), now=NOW))
        self.assertIn("was it true", next(l for l in lines if "This machine" in l))
        self.assertIn("was it wanted", next(l for l in lines if "Your operator" in l))
        self.assertIn("was it sound", next(l for l in lines if "A second reader" in l))

    def test_no_line_carries_the_text_of_a_claim(self):
        secret = "the operator said something private"
        lines = verdicts.lines(verdicts.scan(
            [("verdict", NOW, {"source": "operator", "claim": secret, "ruling": "failed",
                               "reason": secret})], now=NOW))
        self.assertNotIn(secret, " ".join(lines))


class TestUncheckedIsNotPassed(unittest.TestCase):
    """Or the surest route to a clean record is to say nothing falsifiable."""

    def test_unchecked_is_its_own_column(self):
        got = verdicts.scan(
            [("verdict", NOW, {"source": "machine", "claim": "a", "ruling": "unchecked"})],
            now=NOW)
        self.assertEqual(got["machine"]["unchecked"], 1)
        self.assertEqual(got["machine"]["held"], 0)

    def test_the_line_says_so_in_words(self):
        line = verdicts.lines(verdicts.scan(
            [("verdict", NOW, {"source": "machine", "claim": "a", "ruling": "unchecked"})],
            now=NOW))[0]
        self.assertIn("not the same as correct", line)


class TestAVerdictCanBeRevised(unittest.TestCase):
    """A judge who cannot be seen to change their mind ossifies."""

    def test_a_superseding_ruling_wins_and_the_change_is_reported(self):
        stream = [
            ("verdict", NOW - 600, {"source": "review", "claim": "x", "ruling": "failed",
                                    "by": "claude"}),
            ("verdict", NOW - 60, {"source": "review", "claim": "x", "ruling": "held",
                                   "by": "claude", "supersedes": "the earlier ruling"}),
        ]
        got = verdicts.scan(stream, now=NOW)
        self.assertEqual(got["review"]["held"], 1)
        self.assertEqual(got["review"]["failed"], 0)
        lines = verdicts.lines(got)
        self.assertTrue(any("looked at again and changed" in l for l in lines))

    def test_two_independent_rulings_on_the_same_shape_both_count(self):
        stream = [
            ("verdict", NOW - 600, {"source": "machine", "claim": "x", "ruling": "failed"}),
            ("verdict", NOW - 60, {"source": "machine", "claim": "x", "ruling": "failed"}),
        ]
        got = verdicts.scan(stream, now=NOW)
        self.assertEqual(got["machine"]["failed"], 2)


class TestItFailsSoft(unittest.TestCase):
    def test_a_broken_checker_does_not_cost_the_answer(self):
        original = verdicts.check_path_claims
        verdicts.check_path_claims = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
        try:
            got = rule(CONFABULATION)
        finally:
            verdicts.check_path_claims = original
        self.assertTrue(by_claim(got, "run read_logs"))

    def test_rubbish_in_is_survivable(self):
        self.assertEqual(verdicts.check(None, None, None, now=NOW), [])
        self.assertEqual(verdicts.scan([("verdict", "not a time", None)], now=NOW), {})

    def test_an_empty_record_reports_nothing(self):
        self.assertEqual(verdicts.lines(verdicts.scan([], now=NOW)), [])


class TestTheAgentSide(unittest.TestCase):

    def make_agent(self):
        from jarvis.agent.core import AgentCore
        return AgentCore({"name": "t", "profile": "cloud"},
                         {"display": None, "input": None, "memory": None, "storage": None},
                         LOG)

    def test_the_machine_cannot_be_told_a_verdict(self):
        """It rules by checking. An endpoint into it would be a way to forge one."""
        agent = self.make_agent()
        self.assertIsNone(agent.record_verdict("x", verdicts.HELD, verdicts.MACHINE))

    def test_an_operator_ruling_reaches_the_memory_he_actually_reads(self):
        agent = self.make_agent()
        entry = agent.record_verdict("that proposal was not wanted", verdicts.FAILED,
                                     verdicts.OPERATOR, by="paul", reason="cost")
        self.assertEqual(entry["source"], "operator")
        self.assertEqual(entry["asks"], "was it wanted")
        self.assertIn("not wanted", list(agent.notes)[-1])

    def test_the_confabulation_is_caught_on_the_way_out_of_chat(self):
        """End to end: the answer itself carries the check, and it is recorded."""
        import json as _json
        import tempfile
        from jarvis.agent.core import AgentCore
        from jarvis.brain.llm import BaseBrain, Completion
        try:
            from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: F401
        except Exception:
            self.skipTest("cryptography not installed")
        from jarvis.ledger import AgentLedger

        class Confabulator(BaseBrain):
            provider, default_model = "fake", "fake-1"

            def __init__(self):
                super().__init__({"model": "fake-1", "max_tokens": 512}, LOG, client=object())

            def _make_client(self):
                return object()

            def _complete(self, system, messages, structured, cache=True):
                return Completion(text=CONFABULATION, stop_reason="end_turn",
                                  usage={"input_tokens": 3, "output_tokens": 2})

        with tempfile.TemporaryDirectory() as tmp:
            led = AgentLedger({"enabled": True, "path": f"{tmp}/l.jsonl",
                               "key_file": f"{tmp}/k/ed25519.key",
                               "pubkey_file": f"{tmp}/k/ed25519.pub"}, LOG, writer="t")
            agent = AgentCore({"name": "t", "profile": "cloud"},
                              {"display": None, "input": None, "memory": None,
                               "storage": None}, LOG, brain=Confabulator(), ledger=led)
            answer = agent.chat([{"role": "user", "content": "did you read the logs?"}])
            self.assertIn("[claim check]", answer)
            self.assertIn("read_logs", answer)
            entries = [e for e in led.tail(20) if e["kind"] == "verdict"]
            self.assertTrue(entries, "no verdict reached the record")
            tool = next(e for e in entries if "read_logs" in e["body"]["claim"])
            self.assertEqual(tool["body"]["ruling"], "failed")
            self.assertEqual(tool["body"]["source"], "machine")
            self.assertEqual(tool["body"]["asks"], "was it true")
            led.close()

    def test_a_ruling_needs_a_real_ruling_and_source(self):
        agent = self.make_agent()
        self.assertIsNone(agent.record_verdict("x", "brilliant", verdicts.OPERATOR))
        self.assertIsNone(agent.record_verdict("", verdicts.HELD, verdicts.OPERATOR))
        self.assertIsNone(agent.record_verdict("x", verdicts.HELD, "someone else"))


if __name__ == "__main__":
    unittest.main()
