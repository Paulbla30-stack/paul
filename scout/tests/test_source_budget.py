"""How long one source may spend before it has to stop.

23 September 2026, the scheduled 07:00 run: medRxiv took 234 seconds of the
Lambda's 300 and the run hit the wall with Status: timeout. It survived only
because Lambda retried it automatically, and the retry is not a safety net --
a first attempt that writes chain entries and then dies marks those items
seen, so the retry finds nothing new and sends no digest. A whole day of
reading would vanish with no error anywhere.

No single request was slow. Every request already had a timeout. What had no
bound was how MANY requests a source could make: medRxiv pages through its
results in a `while True`. This is the missing bound.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import sources  # noqa: E402
from sources import Budget, BudgetExpired, FetchError  # noqa: E402


class _Clock:
    """A clock the test moves by hand, so nothing here sleeps."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def tick(self, seconds):
        self.now += seconds


class TestTheAllowance(unittest.TestCase):

    def setUp(self):
        self.clock = _Clock()
        self.addCleanup(sources.set_budget, None)

    def test_time_remaining_counts_down(self):
        b = Budget(90, clock=self.clock)
        self.assertEqual(b.remaining(), 90)
        self.clock.tick(30)
        self.assertEqual(b.remaining(), 60)

    def test_it_does_not_trip_while_there_is_time(self):
        b = Budget(90, clock=self.clock)
        self.clock.tick(89)
        b.check("https://example.invalid/page")
        self.assertFalse(b.tripped)

    def test_it_trips_once_spent(self):
        b = Budget(90, clock=self.clock)
        self.clock.tick(91)
        with self.assertRaises(BudgetExpired):
            b.check("https://example.invalid/page")
        self.assertTrue(b.tripped)

    def test_a_budget_expiry_is_a_fetch_error(self):
        # collect() catches FetchError per source already. If this were not a
        # subclass, one slow source would kill the whole run instead of
        # degrading to a failed source.
        self.assertTrue(issubclass(BudgetExpired, FetchError))

    def test_the_message_says_what_ran_out(self):
        b = Budget(5, clock=self.clock)
        self.clock.tick(6)
        with self.assertRaises(BudgetExpired) as caught:
            b.check("https://example.invalid/page/7")
        self.assertIn("5s", str(caught.exception))
        self.assertIn("page/7", str(caught.exception))


class TestHttpGetHonoursIt(unittest.TestCase):
    """The bound lives in http_get, so every source inherits it without a
    single fetcher signature changing."""

    def setUp(self):
        self.clock = _Clock()
        self.addCleanup(sources.set_budget, None)
        self.opened = []

        import urllib.request
        self.real = urllib.request.urlopen

        class _Response:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            @staticmethod
            def read():
                return b"{}"

        def fake(req, timeout=None):
            self.opened.append((req.full_url, timeout))
            self.clock.tick(40)          # every call is slow but none times out
            return _Response()

        urllib.request.urlopen = fake
        self.addCleanup(setattr, urllib.request, "urlopen", self.real)

    def test_with_no_budget_nothing_changes(self):
        sources.set_budget(None)
        sources.http_get("https://example.invalid/a", timeout=30.0)
        self.assertEqual(len(self.opened), 1)

    def test_a_paginating_source_is_stopped_partway(self):
        # The medRxiv shape: many requests, none of them slow on its own.
        sources.set_budget(Budget(90, clock=self.clock))
        pages = 0
        with self.assertRaises(BudgetExpired):
            for n in range(100):
                sources.http_get(f"https://example.invalid/page/{n}", timeout=30.0)
                pages += 1
        self.assertEqual(pages, 3, "90s at 40s a page should allow three")

    def test_the_timeout_never_outlives_the_budget(self):
        # A 30s timeout with 4s of budget left just moves the overrun into
        # the socket instead of preventing it.
        sources.set_budget(Budget(50, clock=self.clock))
        sources.http_get("https://example.invalid/a", timeout=30.0)
        sources.http_get("https://example.invalid/b", timeout=30.0)
        self.assertEqual(self.opened[0][1], 30.0)
        self.assertEqual(self.opened[1][1], 10.0, "clamped to what is left")

    def test_the_budget_is_checked_before_the_request_not_after(self):
        sources.set_budget(Budget(10, clock=self.clock))
        sources.http_get("https://example.invalid/a", timeout=5.0)
        before = len(self.opened)
        with self.assertRaises(BudgetExpired):
            sources.http_get("https://example.invalid/b", timeout=5.0)
        self.assertEqual(len(self.opened), before, "it opened a connection anyway")


class TestCollectReportsTruncationSeparately(unittest.TestCase):
    """Neither ok nor failed: some of the window was read and the rest was
    not, and calling that a clean sweep files a partial day as a complete
    one."""

    def setUp(self):
        self.addCleanup(sources.set_budget, None)

    def _cfg(self, budget_s=90.0):
        return {
            "run": {"lookback_days": 7, "max_items_per_source": 50,
                    "source_budget_s": budget_s},
            "keywords": {"tier_a": ["a"], "tier_b": ["b"]},
            "sources": {name: {"enabled": name == "medrxiv"}
                        for name in ("arxiv", "medrxiv", "lesswrong",
                                     "hackernews", "moltbook")},
        }

    def test_a_source_that_ran_out_is_named_as_truncated(self):
        import app as _app
        from datetime import datetime, timezone

        def slow(cfg, now, lookback, limit):
            b = sources.current_budget()
            self.assertIsNotNone(b, "collect did not set a budget")
            b.tripped = True                  # as a real fetcher's break leaves it
            return ["one item"]

        saved = _app.medrxiv.fetch
        _app.medrxiv.fetch = slow
        try:
            items, ok, failed, truncated = _app.collect(
                self._cfg(), datetime.now(timezone.utc))
        finally:
            _app.medrxiv.fetch = saved
        self.assertEqual(items, ["one item"])
        self.assertEqual(ok, ["medrxiv"])
        self.assertEqual(failed, [])
        self.assertEqual(truncated, ["medrxiv"], "a partial sweep read as complete")

    def test_a_clean_source_is_not_named(self):
        import app as _app
        from datetime import datetime, timezone

        saved = _app.medrxiv.fetch
        _app.medrxiv.fetch = lambda cfg, now, lookback, limit: ["one item"]
        try:
            _, ok, failed, truncated = _app.collect(
                self._cfg(), datetime.now(timezone.utc))
        finally:
            _app.medrxiv.fetch = saved
        self.assertEqual((ok, failed, truncated), (["medrxiv"], [], []))

    def test_the_budget_does_not_leak_between_sources(self):
        # A budget left set would be shared by the next source and by anything
        # else in the process that fetches.
        import app as _app
        from datetime import datetime, timezone

        saved = _app.medrxiv.fetch
        _app.medrxiv.fetch = lambda cfg, now, lookback, limit: []
        try:
            _app.collect(self._cfg(), datetime.now(timezone.utc))
        finally:
            _app.medrxiv.fetch = saved
        self.assertIsNone(sources.current_budget())

    def test_it_is_cleared_even_when_the_source_raises(self):
        import app as _app
        from datetime import datetime, timezone

        def boom(cfg, now, lookback, limit):
            raise RuntimeError("no")

        saved = _app.medrxiv.fetch
        _app.medrxiv.fetch = boom
        try:
            _, ok, failed, truncated = _app.collect(
                self._cfg(), datetime.now(timezone.utc))
        finally:
            _app.medrxiv.fetch = saved
        self.assertEqual((ok, failed), ([], ["medrxiv"]))
        self.assertIsNone(sources.current_budget())


if __name__ == "__main__":
    unittest.main()
