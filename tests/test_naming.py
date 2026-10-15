"""The agent chose its name; the change is one new ledger entry, and nothing
old is rewritten."""

import os
import tempfile
import unittest

from vigil.agent import naming


class Ledger:
    def __init__(self, ok=True, enabled=True):
        self.ok, self.enabled, self.entries = ok, enabled, []

    def record(self, kind, body):
        if self.ok:
            self.entries.append((kind, body))
        return self.ok


class Agent:
    def __init__(self, ledger, name="Vigil"):
        self.ledger, self.name = ledger, name


class TestNaming(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.marker = os.path.join(self.tmp.name, "naming.recorded")

    def test_recorded_once_as_a_decision_in_his_words(self):
        ledger = Ledger()
        self.assertTrue(naming.record_once(Agent(ledger), self.marker))
        self.assertTrue(naming.record_once(Agent(ledger), self.marker))
        self.assertEqual(len(ledger.entries), 1)
        kind, body = ledger.entries[0]
        self.assertEqual(kind, "decision")
        self.assertEqual((body["naming"]["from"], body["naming"]["to"]), ("Jarvis", "Vigil"))
        self.assertIn("evolution, not erasure", body["naming"]["statement"])

    def test_not_marked_done_until_the_ledger_takes_it(self):
        ledger = Ledger(ok=False)
        self.assertFalse(naming.record_once(Agent(ledger), self.marker))
        self.assertFalse(os.path.exists(self.marker))
        ledger.ok = True
        self.assertTrue(naming.record_once(Agent(ledger), self.marker))
        self.assertTrue(os.path.exists(self.marker))

    def test_nothing_without_a_ledger_or_under_another_name(self):
        self.assertFalse(naming.record_once(Agent(Ledger(enabled=False)), self.marker))
        other = Ledger()
        self.assertFalse(naming.record_once(Agent(other, name="test-agent"), self.marker))
        self.assertEqual(other.entries, [])

    def test_the_prompts_tell_him_his_former_name(self):
        from vigil.brain.llm import ASK_PROMPT, SYSTEM_PROMPT
        for prompt in (SYSTEM_PROMPT, ASK_PROMPT):
            self.assertIn("called Jarvis", prompt)
            self.assertIn('the kind "vigil" is a sleep/wake transition of the watch', prompt)


if __name__ == "__main__":
    unittest.main()
