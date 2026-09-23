"""A restored goal that names a file which is gone must say so.

The failure this is for, from 23 September 2026: the agent woke on a rebuilt
box carrying three priority-5 goals about uploaded documents. `memory_backup`
had carried memory.db across from i-091c77c6079228ca2, so the goals survived;
nothing carries /var/lib/jarvis/uploads, so the documents did not. It spent
its top-priority attention retrying reads that could never succeed, and the
only reason anyone found out is that it said so in a chat turn.

Two things are being asserted here, and the second matters more than the first:

  * it notices, at start, and says which path;
  * it does **not** retire the goal. Asked what the rule should be, the agent
    argued for a proposal rather than a deletion -- "absence today is not
    absence forever" -- and that is the operator's call. A test that let a
    future edit silently drop an operator instruction would be worse than no
    test at all.
"""

import os
import tempfile
import unittest

from jarvis.agent.core import AgentCore


class TestPathsNamedInAGoal(unittest.TestCase):
    """What counts as a path, and what does not."""

    def named(self, text):
        return AgentCore._paths_named(text)

    def test_it_finds_an_absolute_file_path(self):
        self.assertEqual(
            self.named("Read the uploaded file /var/lib/jarvis/uploads/gas.txt "
                       "and report what is in it"),
            ["/var/lib/jarvis/uploads/gas.txt"])

    def test_it_finds_more_than_one(self):
        self.assertEqual(self.named("diff /etc/a/one.conf against /etc/b/two.conf"),
                         ["/etc/a/one.conf", "/etc/b/two.conf"])

    def test_it_does_not_repeat_a_path_named_twice(self):
        self.assertEqual(self.named("read /tmp/x/y then read /tmp/x/y again"),
                         ["/tmp/x/y"])

    def test_the_root_is_not_a_path_it_reports(self):
        # "Keep the root filesystem under 80% used" is a real configured goal.
        # The root always exists, so reporting it would be noise for ever.
        self.assertEqual(self.named("Keep / under 80% used"), [])

    def test_a_single_segment_is_not_enough(self):
        # /tmp on its own is a directory that always exists; the case this is
        # for is a file inside one.
        self.assertEqual(self.named("clear /tmp"), [])

    def test_trailing_punctuation_is_not_part_of_the_path(self):
        self.assertEqual(self.named("read /var/lib/jarvis/uploads/gas.txt."),
                         ["/var/lib/jarvis/uploads/gas.txt"])
        self.assertEqual(self.named("read /var/lib/a/b, then stop"),
                         ["/var/lib/a/b"])

    def test_a_url_is_not_a_filesystem_path(self):
        # The agent is given URLs in goals often enough that mistaking one for
        # a missing file would fire on every single boot.
        self.assertEqual(self.named("fetch https://example.org/a/b.json"), [])

    def test_prose_with_a_slash_is_not_a_path(self):
        self.assertEqual(self.named("report the read/write ratio"), [])

    def test_empty_and_none_are_safe(self):
        self.assertEqual(self.named(""), [])
        self.assertEqual(self.named(None), [])


class _Recorder:
    """Just enough AgentCore to exercise the check in isolation."""

    _PATH_IN_GOAL = AgentCore._PATH_IN_GOAL
    _paths_named = staticmethod(AgentCore._paths_named)
    _check_goal_paths = AgentCore._check_goal_paths

    def __init__(self):
        import logging
        self.log = logging.getLogger("test.goalpaths")
        self.log.addHandler(logging.NullHandler())
        self.remembered = []
        self.retired = []

    def remember(self, text, kind="note", source="brain", pinned=False):
        self.remembered.append({"text": text, "kind": kind, "source": source})
        return {"id": len(self.remembered)}

    def _forget_goal(self, description, why):
        self.retired.append((description, why))


class TestItSaysSoWhenTheFileIsGone(unittest.TestCase):

    def setUp(self):
        self.agent = _Recorder()
        self.dir = tempfile.mkdtemp()
        self.here = os.path.join(self.dir, "here.txt")
        with open(self.here, "w") as fh:
            fh.write("present\n")
        self.gone = os.path.join(self.dir, "gone.txt")

    def test_a_present_file_produces_nothing(self):
        self.agent._check_goal_paths([(f"read {self.here}", 5)])
        self.assertEqual(self.agent.remembered, [])

    def test_a_missing_file_is_noted(self):
        self.agent._check_goal_paths([(f"read {self.gone}", 5)])
        self.assertEqual(len(self.agent.remembered), 1)
        self.assertIn(self.gone, self.agent.remembered[0]["text"])

    def test_the_note_is_sourced_to_the_system_not_the_brain(self):
        # Provenance is the point everywhere else in this store; a note the
        # startup check wrote is not something the model observed.
        self.agent._check_goal_paths([(f"read {self.gone}", 5)])
        self.assertEqual(self.agent.remembered[0]["source"], "system")
        self.assertEqual(self.agent.remembered[0]["kind"], "note")

    def test_three_goals_about_missing_files_make_one_note(self):
        # The exact shape of the live failure: three goals, two distinct
        # paths. The notes deque goes to the model every cycle, so three
        # lines saying the same thing is three lines it skims.
        other = os.path.join(self.dir, "other.txt")
        self.agent._check_goal_paths([
            (f"read {self.gone} and report what is in it", 5),
            (f"read {self.gone} again and say what dates are in it", 5),
            (f"read the newly uploaded {other}", 5),
        ])
        self.assertEqual(len(self.agent.remembered), 1)
        text = self.agent.remembered[0]["text"]
        self.assertIn(self.gone, text)
        self.assertIn(other, text)
        self.assertIn("2 path", text)

    def test_it_never_retires_the_goal(self):
        """The one that must not be 'fixed' later.

        Noticing and deciding are different jobs. The agent itself asked for
        a proposal rather than a deletion, and the operator rules on it.
        """
        self.agent._check_goal_paths([(f"read {self.gone}", 5)])
        self.assertEqual(self.agent.retired, [])

    def test_no_goals_is_quiet(self):
        self.agent._check_goal_paths([])
        self.assertEqual(self.agent.remembered, [])

    def test_a_goal_naming_no_path_is_quiet(self):
        self.agent._check_goal_paths([("Report security posture once per hour", 5)])
        self.assertEqual(self.agent.remembered, [])

    def test_a_failure_to_note_does_not_stop_the_boot(self):
        def boom(*a, **k):
            raise RuntimeError("store is wedged")
        self.agent.remember = boom
        self.agent._check_goal_paths([(f"read {self.gone}", 5)])   # must not raise

    def test_a_goal_that_names_both_a_present_and_a_missing_file(self):
        self.agent._check_goal_paths([(f"diff {self.here} against {self.gone}", 5)])
        self.assertEqual(len(self.agent.remembered), 1)
        text = self.agent.remembered[0]["text"]
        self.assertIn(self.gone, text)
        self.assertNotIn(self.here, text)


if __name__ == "__main__":
    unittest.main()
