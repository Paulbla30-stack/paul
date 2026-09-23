"""What the machine may post, and what it may only hand over.

Paul's decision, 23 September 2026: Moltbook is agent-only, so the agent
posting there is the native act and every reader knows what they are
reading. Facebook is his own page under his own name, so the agent drafts
and he posts.

The reason this is a registry and not a convention: _send() called the
Moltbook sender for any approved proposal whatever its network. With a
second network in existence, approving a Facebook draft would have posted it
to Moltbook — the right text to the wrong audience, published and verified,
reported as a success.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import approve_app  # noqa: E402
from proposals import DRAFT_ONLY, SENDABLE, may_send  # noqa: E402


class _P:
    """Just enough proposal for _send to refuse."""

    def __init__(self, network):
        self.id = "p-1"
        self.network = network
        self.kind = "post"
        self.draft = "a draft"
        self.target_title = "t"
        self.source_item = {}


class RegistryTest(unittest.TestCase):
    def test_moltbook_is_sendable(self):
        self.assertTrue(may_send("moltbook"))

    def test_facebook_is_not(self):
        self.assertFalse(may_send("facebook"))

    def test_the_two_sets_do_not_overlap(self):
        self.assertEqual(SENDABLE & DRAFT_ONLY, frozenset())

    def test_an_unclassified_network_is_refused(self):
        # A new destination has to be classified deliberately. Defaulting to
        # sendable is how a network nobody decided about gets posted to.
        for unknown in ("linkedin", "x", "twitter", "mastodon", "", None):
            self.assertFalse(may_send(unknown), unknown)

    def test_case_and_spacing_do_not_decide(self):
        for spelling in ("Moltbook", "MOLTBOOK", "  moltbook  "):
            self.assertTrue(may_send(spelling), spelling)
        for spelling in ("Facebook", "FACEBOOK", " facebook "):
            self.assertFalse(may_send(spelling), spelling)


class SendGuardTest(unittest.TestCase):
    """The guard must refuse before anything reaches a sender."""

    def setUp(self):
        # If the guard leaks, this makes the test fail loudly rather than
        # quietly attempting a real network call.
        import sender
        self._real = sender.MoltbookSender

        def _boom(*a, **k):
            raise AssertionError("the sender was constructed; the guard leaked")

        sender.MoltbookSender = _boom
        self.addCleanup(lambda: setattr(sender, "MoltbookSender", self._real))

    def test_a_draft_only_network_is_refused(self):
        out = approve_app._send(_P("facebook"), "tbl")
        self.assertEqual(out["status"], "failed")
        self.assertFalse(out["published"])
        self.assertIn("draft-only", out["detail"])
        self.assertIn("nothing was sent", out["detail"])

    def test_the_refusal_says_who_posts_it_instead(self):
        out = approve_app._send(_P("facebook"), "tbl")
        self.assertIn("operator posts it himself", out["detail"])

    def test_an_unclassified_network_is_refused_too(self):
        out = approve_app._send(_P("linkedin"), "tbl")
        self.assertEqual(out["status"], "failed")
        self.assertIn("is not a network this project may post to", out["detail"])

    def test_refusing_is_never_reported_as_published(self):
        for network in ("facebook", "linkedin", "", "moltbook.com"):
            out = approve_app._send(_P(network), "tbl")
            self.assertFalse(out["published"], network)


if __name__ == "__main__":
    unittest.main()
