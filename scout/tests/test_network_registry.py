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
from proposals import (PERMITTED, SENDERS, has_sender, may_send,  # noqa: E402
                       permitted, refusal_reason)


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

    def test_facebook_is_permitted_but_has_no_write_path_yet(self):
        # Paul's decision is that the machine may post there. The sender does
        # not exist, so it is still refused — and refused for the honest
        # reason, not silently treated as forbidden.
        self.assertTrue(permitted("facebook"))
        self.assertFalse(has_sender("facebook"))
        self.assertFalse(may_send("facebook"))
        self.assertIn("no write path", refusal_reason("facebook"))

    def test_every_sender_is_for_a_permitted_network(self):
        # A write path to somewhere nobody decided about is the dangerous
        # direction: the code could post before the decision was made.
        self.assertTrue(set(SENDERS) <= set(PERMITTED))

    def test_an_unclassified_network_is_refused(self):
        # A new destination has to be classified deliberately. Defaulting to
        # sendable is how a network nobody decided about gets posted to.
        for unknown in ("linkedin", "x", "twitter", "mastodon", "", None):
            self.assertFalse(may_send(unknown), unknown)
            self.assertFalse(permitted(unknown), unknown)

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

    def test_a_permitted_network_with_no_sender_is_refused(self):
        # This is the case that would otherwise post a Facebook draft to
        # Moltbook, because _send used to call the Moltbook sender for
        # anything approved.
        out = approve_app._send(_P("facebook"), "tbl")
        self.assertEqual(out["status"], "failed")
        self.assertFalse(out["published"])
        self.assertIn("no write path", out["detail"])
        self.assertIn("nothing was sent", out["detail"])

    def test_the_refusal_distinguishes_not_yet_from_never(self):
        not_yet = approve_app._send(_P("facebook"), "tbl")["detail"]
        never = approve_app._send(_P("linkedin"), "tbl")["detail"]
        self.assertNotEqual(not_yet, never)
        self.assertIn("permitted", not_yet)

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
