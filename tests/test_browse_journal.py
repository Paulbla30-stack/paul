"""The browser journal and the daily debrief.

Paul: "under approval with daily debrief of what went well and what didn't.
Where I had to correct." In data, that is: every browse action with its
outcome and who did it, every decision he makes on a browse proposal, and a
summary that says so plainly -- including when the answer is "nothing".
"""

import os
import tempfile
import unittest

from jarvis.agent.browse import BrowserJournal


class _Clock:
    def __init__(self, t=1_790_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class TestRecording(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.clock = _Clock()
        self.j = BrowserJournal(os.path.join(self.dir, "j.jsonl"), clock=self.clock)

    def test_an_entry_is_written_and_read_back(self):
        self.j.record("opened https://e.test/", "https://e.test/", "ok")
        rows = self.j.recent()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["by"], "agent")
        self.assertEqual(rows[0]["outcome"], "ok")

    def test_old_entries_fall_out_of_the_window(self):
        self.j.record("opened a", "https://a.test/", "ok")
        self.clock.t += 2 * 86400
        self.j.record("opened b", "https://b.test/", "ok")
        self.assertEqual([r["did"] for r in self.j.recent(86400)], ["opened b"])

    def test_a_missing_file_is_an_empty_journal_not_an_error(self):
        j = BrowserJournal(os.path.join(self.dir, "nope.jsonl"))
        self.assertEqual(j.recent(), [])
        self.assertEqual(j.debrief()["actions"], 0)

    def test_a_corrupt_line_is_skipped_not_fatal(self):
        self.j.record("opened a", "https://a.test/", "ok")
        with open(self.j.path, "a") as fh:
            fh.write("not json\n")
        self.j.record("opened b", "https://b.test/", "ok")
        self.assertEqual(len(self.j.recent()), 2)

    def test_the_journal_is_bounded(self):
        from jarvis.agent import browse
        old = browse.MAX_JOURNAL_BYTES
        browse.MAX_JOURNAL_BYTES = 4000
        try:
            for i in range(200):
                self.j.record(f"opened page {i} " + "x" * 80, "https://e.test/", "ok")
            self.assertLessEqual(os.path.getsize(self.j.path), 4000)
            self.assertTrue(self.j.recent())               # newest kept
        finally:
            browse.MAX_JOURNAL_BYTES = old


class TestTheDebrief(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.j = BrowserJournal(os.path.join(self.dir, "j.jsonl"), clock=_Clock())

    def test_nothing_says_nothing(self):
        text = BrowserJournal.render(self.j.debrief())
        self.assertIn("Nothing", text)

    def test_it_counts_pages_actions_and_what_waited(self):
        self.j.record("opened https://a.test/", "https://a.test/", "ok")
        self.j.record("followed L2", "https://a.test/b", "ok")
        self.j.record("submit", "https://a.test/b", "needs-approval",
                      reason="submitting a form sends something", gate="publish")
        d = self.j.debrief()
        self.assertEqual(d["actions"], 3)
        self.assertEqual(d["pages_visited"], 2)
        self.assertEqual(len(d["waited_for_paul"]), 1)
        self.assertEqual(d["waited_for_paul"][0]["gate"], "publish")
        # Only the page something happened on is named; the page merely read
        # is a count. The agent's own call, when asked.
        self.assertEqual(d["top_pages"], ["https://a.test/b"])
        self.assertIn("Did something on", BrowserJournal.render(d))

    def test_a_declined_decision_is_a_correction(self):
        self.j.record("submit", "https://a.test/", "needs-approval", gate="publish")
        self.j.record("Browser: send the form", "https://a.test/", "declined",
                      by="operator", reason="not that one", kind="decision")
        self.j.record("Browser: press next", "https://a.test/", "accepted",
                      by="operator", kind="decision")
        d = self.j.debrief()
        self.assertEqual(d["corrections"], 1)
        self.assertEqual(len(d["decisions"]), 2)
        text = BrowserJournal.render(d)
        self.assertIn("corrected (declined) 1", text)
        self.assertIn("not that one", text)

    def test_pauls_own_actions_are_counted_separately(self):
        self.j.record("open news.test", "https://news.test/", "ok", by="operator")
        d = self.j.debrief()
        self.assertEqual(d["operator_actions"], 1)
        self.assertEqual(d["actions"], 0)

    def test_refusals_and_errors_are_kept_apart(self):
        self.j.record("type F1", "https://a.test/", "refused", reason="secret field")
        self.j.record("opened x", "", "error", reason="timed out")
        d = self.j.debrief()
        self.assertEqual(len(d["refused"]), 1)
        self.assertEqual(len(d["errors"]), 1)
        text = BrowserJournal.render(d)
        self.assertIn("Refused", text)
        self.assertIn("Did not work", text)


if __name__ == "__main__":
    unittest.main()
