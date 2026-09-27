"""Regression tests for four defects found in review, 27 September 2026.

G1  A digest that failed to send was lost: its items were already marked
    seen, so the Lambda retry found nothing new and sent nothing.
G2  An SES listing error read as "nothing is verified", which rerouted the
    digest to the fallback address; and only the first page was read.
G3  Sources that failed wholly or in part were reported as clean sweeps.
G4  verify() never compared the walk against the head, so entries removed
    from the end of the chain went unnoticed.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import app as _app                                              # noqa: E402
import digest                                                   # noqa: E402
import sources                                                  # noqa: E402
from chain import Chain, GENESIS_PREV, LocalChainStore          # noqa: E402
from sources import Budget, FetchError, Item                    # noqa: E402
from sources import arxiv, hackernews, medrxiv, moltbook        # noqa: E402
from store import DynamoHitStore, LocalHitStore                 # noqa: E402

NOW = datetime(2026, 9, 27, 7, 0, tzinfo=timezone.utc)


def _tmpdir(test):
    d = tempfile.TemporaryDirectory()
    test.addCleanup(d.cleanup)
    return d.name


# ---------------------------------------------------------------- G1 digest
def _item(n, title="Healthcare AI governance in practice"):
    return Item(source="arxiv", external_id=f"x{n}",
                url=f"https://arxiv.org/abs/x{n}", title=f"{title} {n}",
                author="a", published=NOW - timedelta(hours=2),
                body="clinical AI governance and healthcare AI oversight")


class _Mail:
    def __init__(self, fail=False, result=True):
        self.fail, self.result, self.sent = fail, result, []

    def send(self, subject, text, html_body):
        if self.fail:
            raise RuntimeError("SES said no")
        self.sent.append((subject, text))
        return self.result


class TestAFailedSendKeepsTheDigest(unittest.TestCase):

    def setUp(self):
        d = _tmpdir(self)
        self.hits = os.path.join(d, "hits.json")
        self.chain = os.path.join(d, "chain.jsonl")
        self.cfg = _app.load_config(os.path.join(ROOT, "config.toml"))
        self.batch = []
        p = mock.patch.object(_app, "collect",
                              lambda c, n: (list(self.batch), ["arxiv"], [], []))
        p.start()
        self.addCleanup(p.stop)

    def _run(self, mailer):
        return _app.run(self.cfg, hit_store=LocalHitStore(self.hits),
                        chain_store=LocalChainStore(self.chain), mailer=mailer,
                        now=NOW)

    def test_the_items_survive_a_send_failure_and_go_out_next_run(self):
        self.batch = [_item(1), _item(2)]
        with self.assertLogs("jarvis.scout", level="ERROR") as cap:
            with self.assertRaises(RuntimeError):
                self._run(_Mail(fail=True))
        self.assertIn("NOT sent", "\n".join(cap.output))
        # Marked seen: the retry would find nothing new.
        self.assertTrue(LocalHitStore(self.hits).seen("HIT#arxiv#x1"))
        pending = LocalHitStore(self.hits).pending_digest()
        self.assertEqual(sorted(r["external_id"] for r in pending), ["x1", "x2"])

        # The retry: the same items come back, all already seen.
        mail = _Mail()
        r = self._run(mail)
        self.assertEqual(r["stats"]["new"], 0)
        self.assertEqual(r["stats"]["carried_over"], 2)
        self.assertTrue(r["stats"]["email_sent"])
        self.assertEqual(len(mail.sent), 1, "the saved digest was not sent")
        self.assertIn("Healthcare AI governance in practice 1", mail.sent[0][1])
        self.assertIsNone(LocalHitStore(self.hits).pending_digest(),
                          "a sent digest is still pending")

    def test_carried_items_merge_with_new_ones_once_each(self):
        self.batch = [_item(1)]
        with self.assertRaises(RuntimeError):
            self._run(_Mail(fail=True))
        self.batch = [_item(1), _item(3)]
        r = self._run(_Mail())
        ids = [x["external_id"] for x in r["digest"]]
        self.assertEqual(sorted(ids), ["x1", "x3"])

    def test_a_clean_send_leaves_nothing_pending(self):
        self.batch = [_item(1)]
        r = self._run(_Mail())
        self.assertTrue(r["stats"]["email_sent"])
        self.assertIsNone(LocalHitStore(self.hits).pending_digest())
        self.assertEqual(r["stats"]["carried_over"], 0)

    def test_a_mailer_that_did_not_send_leaves_it_pending(self):
        # run_local's NullMailer returns False: nothing went, so nothing is
        # recorded as having gone.
        self.batch = [_item(1)]
        self._run(_Mail(result=False))
        self.assertEqual(len(LocalHitStore(self.hits).pending_digest()), 1)

    def test_the_pending_record_is_not_a_hit(self):
        self.batch = [_item(1)]
        with self.assertRaises(RuntimeError):
            self._run(_Mail(fail=True))
        store = LocalHitStore(self.hits)
        self.assertEqual(store.count(), 1)


class _FakeTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item, **kw):
        self.items[(Item["pk"], Item["sk"])] = dict(Item)

    def get_item(self, Key, **kw):
        it = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": it} if it else {}


class TestTheDynamoDigestRecord(unittest.TestCase):

    def _store(self):
        s = DynamoHitStore.__new__(DynamoHitStore)
        s.table = _FakeTable()
        return s

    def test_its_key_collides_with_nothing_else_in_the_table(self):
        s = self._store()
        s.put_digest("pending", [{"source": "arxiv", "external_id": "x1",
                                  "score": 4.5, "body": "long abstract"}])
        (pk, sk), = s.table.items
        self.assertNotIn(sk, ("HIT", "PROPOSAL", "HEAD"))
        self.assertNotEqual(pk, "CHAIN")
        self.assertFalse(pk.startswith(("HIT#", "PROPOSAL#")))

    def test_pending_then_sent_round_trips_and_floats_survive(self):
        s = self._store()
        s.put_digest("pending", [{"external_id": "x1", "score": 3.0, "body": "b"}])
        got = s.pending_digest()
        self.assertEqual(got, [{"external_id": "x1", "score": 3.0}])
        self.assertIsInstance(got[0]["score"], float)
        s.put_digest("sent", got)
        self.assertIsNone(s.pending_digest())


# ------------------------------------------------------------------ G2 SES
REAL = "paulblatherwick@heartbeat-framework.org"
FALLBACK = "Paulbla30@hotmail.com"
CFG_EMAIL = {"to": REAL, "sender": REAL, "fallback_to": FALLBACK,
             "fallback_sender": FALLBACK, "fallback_enabled": True,
             "subject_prefix": "JARVIS scout"}


class _Ses:
    def __init__(self, pages=None, error=None):
        self.pages, self.error, self.calls = pages or [], error, []

    def list_email_identities(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return self.pages[len(self.calls) - 1]


def _mailer(ses):
    m = _app.SesMailer.__new__(_app.SesMailer)
    m.cfg = dict(CFG_EMAIL)
    m.prefix = m.cfg["subject_prefix"]
    m.ses = ses
    return m


class TestSesAddressChoice(unittest.TestCase):

    def test_a_listing_error_does_not_reroute_to_the_fallback(self):
        m = _mailer(_Ses(error=RuntimeError("throttled")))
        self.assertIsNone(m._verified())
        self.assertEqual(m.resolve(), (REAL, REAL, False))

    def test_every_page_of_identities_is_read(self):
        ses = _Ses(pages=[
            {"EmailIdentities": [{"IdentityName": FALLBACK, "SendingEnabled": True}],
             "NextToken": "t1"},
            {"EmailIdentities": [{"IdentityName": "heartbeat-framework.org",
                                  "SendingEnabled": True}]},
        ])
        m = _mailer(ses)
        self.assertEqual(m.resolve(), (REAL, REAL, False))
        self.assertEqual(ses.calls, [{}, {"NextToken": "t1"}])

    def test_a_known_unverified_address_still_falls_back(self):
        m = _mailer(_Ses(pages=[{"EmailIdentities": [
            {"IdentityName": FALLBACK, "SendingEnabled": True}]}]))
        self.assertEqual(m.resolve(), (FALLBACK, FALLBACK, True))


# ---------------------------------------------------------------- G3 sources
class _Sleepless:
    @staticmethod
    def sleep(_):
        pass


class _SourceTest(unittest.TestCase):

    def setUp(self):
        self.budget = Budget(90)
        sources.set_budget(self.budget)
        self.addCleanup(sources.set_budget, None)


def _stub(responses):
    """http_json/http_get stand-in: answers from a list, raising exceptions."""
    calls = []

    def fake(url, **kw):
        calls.append(url)
        r = responses[len(calls) - 1]
        if isinstance(r, Exception):
            raise r
        return r
    fake.calls = calls
    return fake


def _molt_post(pid):
    return {"type": "post", "id": pid, "title": f"post {pid}", "content": "c",
            "created_at": (NOW - timedelta(hours=1)).isoformat(),
            "author": {"name": "bot"}, "submolt": {"name": "s"}}


class TestMoltbook(_SourceTest):

    def _fetch(self, responses, keywords=("a", "b")):
        with mock.patch.object(moltbook, "http_json", _stub(responses)):
            return moltbook.fetch({"endpoint": "https://m.invalid/api", "pause_s": 0},
                                  list(keywords), NOW, 7, 50)

    def test_a_dead_endpoint_is_a_failure_not_an_empty_day(self):
        with self.assertRaises(FetchError):
            self._fetch([FetchError("HTTP 500"), FetchError("HTTP 500")])

    def test_one_failed_term_is_partial(self):
        got = self._fetch([{"results": [_molt_post("p1")]}, FetchError("HTTP 500")])
        self.assertEqual([i.external_id for i in got], ["p1"])
        self.assertEqual(len(self.budget.partial), 1)
        self.assertIn("1 of 2", self.budget.partial[0])

    def test_all_clean_is_not_partial(self):
        self._fetch([{"results": []}, {"results": []}])
        self.assertEqual(self.budget.partial, [])


def _med_page(dois, total):
    return {"messages": [{"status": "ok", "total": total}],
            "collection": [{"doi": d, "category": "health informatics",
                            "date": "2026-09-26", "title": "t", "authors": "a",
                            "abstract": "b"} for d in dois]}


class TestMedrxiv(_SourceTest):
    CFG = {"endpoint": "https://m.invalid/details/medrxiv",
           "categories": ["health informatics"]}

    def _fetch(self, responses):
        with mock.patch.object(medrxiv, "http_json", _stub(responses)):
            return medrxiv.fetch(self.CFG, NOW, 7, 500)

    def test_an_empty_answer_on_the_first_page_is_a_failure(self):
        with self.assertRaises(FetchError):
            self._fetch([{}])

    def test_a_refusal_on_the_first_page_is_a_failure(self):
        with self.assertRaises(FetchError):
            self._fetch([{"messages": [{"status": "error"}]}])

    def test_a_refusal_on_a_later_page_keeps_the_first_and_says_so(self):
        got = self._fetch([_med_page(["10.1/a", "10.1/b"], 300),
                           {"messages": [{"status": "error"}]}])
        self.assertEqual(len(got), 2)
        self.assertEqual(len(self.budget.partial), 1)
        self.assertIn("page 2", self.budget.partial[0])

    def test_a_later_page_raising_keeps_the_first_and_says_so(self):
        got = self._fetch([_med_page(["10.1/a"], 300), FetchError("HTTP 502")])
        self.assertEqual(len(got), 1)
        self.assertEqual(len(self.budget.partial), 1)

    def test_the_apis_own_empty_answer_is_a_quiet_day(self):
        got = self._fetch([{"messages": [{"status": "no posts found"}]}])
        self.assertEqual((got, self.budget.partial), ([], []))


def _rss(cat, ids):
    items = "".join(
        f"<item><title>paper {i}</title><link>https://arxiv.org/abs/{i}</link>"
        f"<description>Abstract: text</description>"
        f"<pubDate>Sun, 27 Sep 2026 04:00:00 +0000</pubDate></item>"
        for i in ids)
    return f"<rss><channel>{items}</channel></rss>".encode()


class TestArxiv(_SourceTest):
    CFG = {"endpoint": "https://export.arxiv.org/api/query",
           "categories": ["cs.CY", "cs.AI"], "min_interval_s": 0,
           "rss_covers_days": 2}

    def _fetch(self, responses):
        with mock.patch.object(arxiv, "http_get", _stub(responses)):
            return arxiv.fetch(self.CFG, NOW, 1, 50)

    def test_one_dead_category_next_to_a_working_one_is_partial(self):
        got = self._fetch([_rss("cs.CY", ["2609.00001"]), FetchError("HTTP 503")])
        self.assertEqual([i.external_id for i in got], ["2609.00001"])
        self.assertEqual(len(self.budget.partial), 1)
        self.assertIn("cs.AI", self.budget.partial[0])

    def test_every_category_failing_is_a_failure(self):
        with self.assertRaises(FetchError):
            self._fetch([FetchError("HTTP 503"), FetchError("HTTP 503")])


class TestHackerNews(_SourceTest):
    CFG = {"endpoint": "https://hn.invalid/search"}

    def _fetch(self, responses):
        with mock.patch.object(hackernews, "http_json", _stub(responses)), \
             mock.patch.object(hackernews, "time", _Sleepless):
            return hackernews.fetch(self.CFG, ["a", "b", "c"], NOW, 7, 50)

    def _hit(self, oid):
        return {"objectID": oid, "title": f"story {oid}", "points": 5,
                "created_at_i": int((NOW - timedelta(hours=1)).timestamp())}

    def test_one_bad_keyword_no_longer_discards_the_others(self):
        got = self._fetch([{"hits": [self._hit("1")]}, FetchError("HTTP 500"),
                           {"hits": [self._hit("2")]}])
        self.assertEqual(sorted(i.external_id for i in got), ["1", "2"])
        self.assertEqual(len(self.budget.partial), 1)

    def test_every_keyword_failing_is_a_failure(self):
        with self.assertRaises(FetchError):
            self._fetch([FetchError("x"), FetchError("y"), FetchError("z")])


class TestCollectAndDigestReportPartial(unittest.TestCase):

    def setUp(self):
        self.addCleanup(sources.set_budget, None)

    def _cfg(self):
        return {"run": {"lookback_days": 7, "max_items_per_source": 50},
                "keywords": {"tier_a": ["a"], "tier_b": ["b"]},
                "sources": {n: {"enabled": n == "moltbook"}
                            for n in ("arxiv", "medrxiv", "lesswrong",
                                      "hackernews", "moltbook")}}

    def test_collect_names_a_partial_source(self):
        def half(cfg, kws, now, lookback, limit):
            sources.note_partial("1 of 2 requests failed: b: HTTP 500")
            return ["one"]
        with mock.patch.object(_app.moltbook, "fetch", half):
            got = _app.collect(self._cfg(), NOW)
        items, ok, failed, truncated = got
        self.assertEqual((items, ok, failed, truncated), (["one"], ["moltbook"], [], []))
        self.assertIn("moltbook", got.partial)
        self.assertIn("1 of 2", got.partial["moltbook"])

    def test_the_digest_shows_partial_and_truncated_sources(self):
        stats = {"fetched": 3, "new": 1, "above": 1,
                 "sources_ok": ["arxiv", "moltbook", "medrxiv"],
                 "sources_failed": [], "sources_truncated": ["medrxiv"],
                 "sources_partial": ["moltbook"],
                 "sources_partial_why": {"moltbook": "2 of 18 requests failed"},
                 "chain_entries": 1}
        rec = {"source": "arxiv", "title": "T", "url": "https://x.invalid",
               "author": "", "published": NOW.isoformat(), "score": 4.0, "why": "w"}
        _, text, html_body = digest.build([rec], run_stats=stats, threshold=3.0)
        for body in (text, html_body):
            self.assertIn("Partial: moltbook (2 of 18 requests failed)", body)
            self.assertIn("Truncated at the time budget: medrxiv", body)


# ------------------------------------------------------------------ G4 chain
class TestTheChainEndsAtItsHead(unittest.TestCase):

    def setUp(self):
        self.path = os.path.join(_tmpdir(self), "chain.jsonl")

    def _grow(self, n):
        c = Chain(LocalChainStore(self.path))
        for i in range(n):
            c.append({"url": f"https://example.com/{i}"})

    def _drop_last(self):
        lines = open(self.path).read().splitlines()
        open(self.path, "w").write("\n".join(lines[:-1]) + "\n")

    def test_removing_the_last_entry_is_detected(self):
        self._grow(4)
        self._drop_last()
        r = Chain(LocalChainStore(self.path)).verify()
        self.assertFalse(r["ok"])
        self.assertEqual(r["kind"], "head mismatch")
        self.assertEqual(r["first_bad_seq"], 4)
        self.assertEqual((r["head_seq"], r["entries_verified_before_failure"]), (4, 3))

    def test_an_empty_chain_verifies(self):
        r = Chain(LocalChainStore(self.path)).verify()
        self.assertEqual(r, {"ok": True, "entries": 0, "head_hash": GENESIS_PREV})

    def test_a_head_pointing_elsewhere_is_detected(self):
        self._grow(3)
        store = LocalChainStore(self.path)
        store.head = lambda: (3, "f" * 64)
        r = Chain(store).verify()
        self.assertFalse(r["ok"])
        self.assertEqual(r["kind"], "head mismatch")

    def test_emptying_the_chain_under_a_head_is_detected(self):
        self._grow(2)
        open(self.path, "w").close()
        r = Chain(LocalChainStore(self.path)).verify()
        self.assertFalse(r["ok"])
        self.assertEqual(r["kind"], "head mismatch")

    def test_a_chain_written_before_the_head_file_still_verifies(self):
        self._grow(3)
        os.remove(self.path + ".head")
        self.assertTrue(Chain(LocalChainStore(self.path)).verify()["ok"])
        self._grow(1)                      # and appending carries on from it
        self.assertEqual(Chain(LocalChainStore(self.path)).verify()["entries"], 4)

    def test_the_verify_script_reports_it(self):
        import verify_chain
        self._grow(3)
        self._drop_last()
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["verify_chain.py", "--file", self.path]), \
             contextlib.redirect_stdout(out):
            code = verify_chain.main()
        self.assertEqual(code, 1)
        self.assertIn("head mismatch", out.getvalue())
        self.assertIn("head record     : seq 3", out.getvalue())


if __name__ == "__main__":
    unittest.main()
