"""How long a proposal stays open, and why it is not one number.

Paul's point, 23 September 2026: urgency is not one size. A reply to a live
thread, a response to something just announced, a release of his own work and
an evergreen explainer have genuinely different lifetimes, and giving them
all seven days tells the truth about none of them.

The window is the system's claim about how long the opportunity lasted. That
makes it a statement, not a scheduling detail.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from proposals import (DEFAULT_PURPOSE, PURPOSE_TTL_DAYS,  # noqa: E402
                       Proposal, ttl_for)

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def make(**kw):
    base = dict(kind="post", network="moltbook", target_url="https://x/1",
                target_title="t", draft="d", rationale="r", discloses=[], now=NOW)
    base.update(kw)
    return Proposal.new(**base)


def window(p):
    return datetime.fromisoformat(p.expires_at) - datetime.fromisoformat(p.created_at)


class WindowTest(unittest.TestCase):
    def test_each_purpose_gets_its_own_window(self):
        for purpose, days in PURPOSE_TTL_DAYS.items():
            self.assertEqual(window(make(purpose=purpose)), timedelta(days=days), purpose)

    def test_a_reply_closes_before_an_evergreen_post(self):
        # The ordering is the point. If these ever collapse to one number the
        # design has quietly gone back to one size fits all.
        self.assertLess(ttl_for("reply"), ttl_for("news"))
        self.assertLess(ttl_for("news"), ttl_for("release"))
        self.assertLess(ttl_for("release"), ttl_for("evergreen"))

    def test_the_purpose_is_on_the_record(self):
        # So a lapse reads back as "the moment passed" rather than
        # "nobody looked".
        self.assertEqual(make(purpose="release").purpose, "release")

    def test_case_and_spacing_do_not_change_the_window(self):
        for spelling in ("Reply", "REPLY", "  reply  "):
            self.assertEqual(window(make(purpose=spelling)), timedelta(days=2), spelling)


class UnknownPurposeTest(unittest.TestCase):
    """An unclassified proposal must not drift to a comfortable middle."""

    def test_an_unknown_purpose_gets_the_shortest_window(self):
        shortest = min(PURPOSE_TTL_DAYS.values())
        for junk in ("urgent", "misc", "whatever", "PROMO"):
            self.assertEqual(ttl_for(junk), shortest, junk)

    def test_the_default_is_not_the_longest(self):
        # Of the two ways to be wrong: lapsing a good proposal is recoverable,
        # posting a reply to a three-week-old thread is not.
        self.assertLess(PURPOSE_TTL_DAYS[DEFAULT_PURPOSE],
                        max(PURPOSE_TTL_DAYS.values()))

    def test_an_empty_purpose_falls_to_the_default(self):
        for missing in (None, "", "   "):
            self.assertEqual(make(purpose=missing).purpose, DEFAULT_PURPOSE)

    def test_adding_a_long_purpose_cannot_lengthen_the_unknown_fallback(self):
        # ttl_for computes the fallback from the table rather than naming a
        # constant, so a new long-lived purpose cannot silently become the
        # window for everything nobody classified.
        import proposals
        original = dict(proposals.PURPOSE_TTL_DAYS)
        try:
            proposals.PURPOSE_TTL_DAYS["campaign"] = 90
            self.assertEqual(proposals.ttl_for("unclassified"), min(original.values()))
        finally:
            proposals.PURPOSE_TTL_DAYS.clear()
            proposals.PURPOSE_TTL_DAYS.update(original)


class OverrideTest(unittest.TestCase):
    def test_an_explicit_ttl_still_wins(self):
        self.assertEqual(window(make(purpose="evergreen", ttl_days=1)), timedelta(days=1))

    def test_omitting_the_ttl_uses_the_purpose(self):
        self.assertEqual(window(make(purpose="release")), timedelta(days=14))


if __name__ == "__main__":
    unittest.main()
