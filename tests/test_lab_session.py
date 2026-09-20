"""The lab session: the switch that cannot be thrown quietly.

One property carries this file. There is no ordering of calls that leaves
the lab open while the agent has not been told, because the announcement
runs first and the window only opens if it landed. Everything else here is
detail around that.

The second property is nearly as important and easier to lose in a later
edit: the agent is told that it is being measured, and not what is being
measured. Telling it the dials would put the answer inside the question and
the lab would stop measuring anything.
"""

import logging
import unittest

from jarvis.agent import lab

LOG = logging.getLogger("test")
NOW = 1_800_000_000.0


class FakeAgent:
    """Just enough agent for the announcement to have somewhere to land."""

    def __init__(self, deaf=False):
        self.notes = []
        self.deaf = deaf            # remember() silently does nothing
        self.ledgered = []
        self.woken = []
        self.cycle_count = 7
        self.ledger = self

    def remember(self, text, kind="note", source="brain", pinned=False):
        if self.deaf:
            return None
        self.notes.append(text)
        return {"text": text, "kind": kind}

    def record(self, kind, body):
        self.ledgered.append((kind, body))

    def note_operator(self, what):
        self.woken.append(what)


def session(agent=None, clock=None):
    ticks = iter([NOW, NOW + 600, NOW + 1200, NOW + 1800, NOW + 2400])
    return lab.LabSession(agent if agent is not None else FakeAgent(),
                          clock=clock or (lambda: next(ticks)))


class TestItCannotOpenWithoutTelling(unittest.TestCase):
    """The whole point. If this test goes, the feature has gone with it."""

    def test_a_deaf_agent_means_a_shut_lab(self):
        s = session(FakeAgent(deaf=True))
        with self.assertRaises(lab.NotAnnounced):
            s.open(purpose="tuning the voice dial")
        self.assertFalse(s.is_open())
        self.assertIsNone(s.notice())

    def test_an_announcement_that_raises_means_a_shut_lab(self):
        def boom(event, state):
            raise RuntimeError("the notice failed")
        s = lab.LabSession(announce=boom)
        with self.assertRaises(lab.NotAnnounced):
            s.open()
        self.assertFalse(s.is_open())

    def test_no_agent_and_no_hook_cannot_open_at_all(self):
        with self.assertRaises(lab.NotAnnounced):
            lab.LabSession().open()

    def test_open_means_announced(self):
        s = session()
        s.open()
        self.assertTrue(s.is_open())
        self.assertTrue(s.state()["announced"])

    def test_forcing_the_flag_without_an_announcement_does_not_open_it(self):
        """is_open() reads the announcement, not the intent to open."""
        s = session()
        s._open = True
        self.assertFalse(s.is_open())


class TestWhatTheAgentIsTold(unittest.TestCase):

    def setUp(self):
        self.agent = FakeAgent()
        self.s = session(self.agent)
        self.s.open(purpose="comparing the voice dial", operator="paul")

    def test_the_memory_says_what_is_happening_and_that_some_of_it_is_not_him(self):
        told = self.agent.notes[-1]
        for phrase in ("behaviour lab is open", "varying what you", "are not yours",
                       "will not remember choosing", "Nothing run in the lab touches"):
            self.assertIn(phrase, told)
        self.assertIn("comparing the voice dial", told)

    def test_it_is_operator_sourced_memory_and_wakes_him(self):
        self.assertEqual(self.agent.woken, ["lab_opened"])
        self.assertEqual(self.agent.ledgered[-1][1]["action"], "lab_opened")
        self.assertTrue(self.agent.ledgered[-1][1]["told_the_agent"])

    def test_the_notice_rides_in_context_for_as_long_as_the_window_is_open(self):
        notice = self.s.notice()
        self.assertTrue(notice["open"])
        self.assertIn("behaviour lab right now", notice["what_this_means"])
        self.assertEqual(notice["purpose"], "comparing the voice dial")
        self.s.close()
        self.assertIsNone(self.s.notice())

    def test_he_is_told_that_he_is_measured_and_not_what_is_measured(self):
        """The dials stay out of it, or the measurement measures the telling.

        Checked against the templates rather than a live session, because the
        purpose line is the operator's own words and they may say anything
        they like in it. What must not happen is the system volunteering the
        settings. That the entries carry a fingerprint is fine and is said on
        purpose: it tells him the marks exist without telling him their value.
        """
        from jarvis.brain import dials
        written = (lab.OPEN_MEMORY + lab.CLOSE_MEMORY + lab.OPEN_NOTICE).lower()
        # Dial labels, not ids: "context" is an ordinary English word and the
        # notice needs it ("none of your context"), while "Context given" is
        # the name of a control and naming one would be telling him a setting.
        for d in dials.registry()["dials"]:
            self.assertNotIn(d["label"].lower(), written,
                             f"the {d['id']} dial leaked into the notice")
        for leak in ("level 0", "max_tokens", "system prompt is", "turned off"):
            self.assertNotIn(leak, written, f"a setting leaked into the notice: {leak}")
        # And the state a UI reads carries no settings either.
        self.assertEqual(set(self.s.state()) & {"settings", "dials", "fingerprint"}, set())

    def test_closing_is_announced_too(self):
        self.s.note_run()
        self.s.close()
        told = self.agent.notes[-1]
        self.assertIn("behaviour lab is closed", told)
        self.assertIn("1 experiment", told)
        self.assertIn("back to your own settings", told)
        self.assertEqual(self.agent.woken[-1], "lab_closed")

    def test_a_close_that_cannot_announce_still_closes(self):
        """Opening fails closed and closing fails open; both end up shut."""
        self.agent.deaf = True
        self.s.close()
        self.assertFalse(self.s.is_open())

    def test_no_purpose_is_said_to_be_unstated_rather_than_invented(self):
        agent = FakeAgent()
        s = session(agent)
        s.open()
        self.assertIn("not stated", agent.notes[-1])
        self.assertNotIn("purpose", s.notice())


class TestTheSwitchBehaves(unittest.TestCase):

    def test_opening_twice_announces_once(self):
        agent = FakeAgent()
        s = session(agent)
        s.open(purpose="p")
        s.open(purpose="p")
        self.assertEqual(agent.woken, ["lab_opened"])

    def test_closing_a_shut_lab_says_nothing(self):
        agent = FakeAgent()
        s = session(agent)
        s.close()
        self.assertEqual(agent.notes, [])

    def test_runs_are_counted_per_window_and_reset_on_the_next_one(self):
        agent = FakeAgent()
        s = lab.LabSession(agent, clock=lambda: NOW)
        s.open()
        s.note_run()
        s.note_run()
        self.assertEqual(s.notice()["runs_so_far"], 2)
        s.close()
        self.assertIn("2 experiments", agent.notes[-1])
        s.open()
        self.assertEqual(s.state()["runs"], 0)

    def test_how_long_it_was_open_reads_like_a_person_wrote_it(self):
        self.assertEqual(lab._duration(41), "41 seconds")
        self.assertEqual(lab._duration(600), "10 minutes")
        self.assertEqual(lab._duration(9000), "2.5 hours")

    def test_a_long_purpose_and_a_long_name_are_bounded(self):
        agent = FakeAgent()
        s = session(agent)
        s.open(purpose="x" * 900, operator="y" * 900)
        self.assertLessEqual(len(s.purpose), 300)
        self.assertLessEqual(len(s.opened_by), 60)


class TestTheAgentSideGate(unittest.TestCase):
    """The gate lives on experiment(), not on the HTTP door in front of it."""

    def test_an_agent_starts_with_the_lab_shut(self):
        from jarvis.agent.core import AgentCore
        agent = AgentCore({"name": "t", "profile": "cloud"},
                          {"display": None, "input": None, "memory": None, "storage": None},
                          LOG)
        self.assertFalse(agent.lab.is_open())
        self.assertIn("lab is closed", agent.experiment("hello")["error"])

    def test_the_context_carries_the_window_only_while_it_is_open(self):
        from jarvis.agent.core import AgentCore
        agent = AgentCore({"name": "t", "profile": "cloud"},
                          {"display": None, "input": None, "memory": None, "storage": None},
                          LOG)
        self.assertIsNone(agent.lab.notice())
        agent.lab.open(purpose="checking")
        self.assertTrue(agent.lab.notice()["open"])
        self.assertIn("behaviour lab is open", list(agent.notes)[-1])


if __name__ == "__main__":
    unittest.main()
