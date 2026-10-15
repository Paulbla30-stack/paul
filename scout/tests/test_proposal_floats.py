"""14 October 2026: the first draft good enough to file crashed the run.

The proposal carried its item's score as a float, DynamoDB refused it, and
the exception came out of propose() before the digest was built. The hits
were already stored as seen, so the retry found nothing new and the day's
email was lost. The fake store in test_stage2_wiring accepted floats; this
one serialises the way boto3 does, so it refuses them as the table did.
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from boto3.dynamodb.types import TypeSerializer                  # noqa: E402

import app as _app                                              # noqa: E402
from chain import LocalChainStore                               # noqa: E402
from proposals import Proposal, ProposalStore                   # noqa: E402
from sources import Item                                        # noqa: E402
from store import LocalHitStore                                 # noqa: E402

NOW = datetime(2026, 10, 14, 6, 0, tzinfo=timezone.utc)


class _StrictTable:
    """Serialises like boto3, so a float fails here exactly as it did live."""

    def __init__(self):
        self.items = {}
        self._ser = TypeSerializer()

    def put_item(self, Item, **kw):
        for k, v in Item.items():
            self._ser.serialize(v)          # TypeError on a float
        self.items[(Item["pk"], Item["sk"])] = dict(Item)

    def get_item(self, Key, **kw):
        it = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": dict(it)} if it else {}


def _proposal():
    return Proposal.new(kind="post", network="moltbook", target_url="https://e.invalid/1",
                        target_title="A paper", draft="A considered reply.",
                        rationale="on topic", discloses=[],
                        source_item={"source": "arxiv", "external_id": "x1",
                                     "url": "https://e.invalid/1", "score": 8.5, "chain_seq": 71},
                        now=NOW, purpose="news")


class TestTheStoreTakesAScore(unittest.TestCase):

    def test_the_strict_table_refuses_a_raw_float(self):
        with self.assertRaises(TypeError):
            _StrictTable().put_item(Item={"pk": "x", "sk": "y", "score": 8.5})

    def test_a_proposal_with_a_float_score_is_stored_and_read_back(self):
        store = ProposalStore.__new__(ProposalStore)
        store.table = _StrictTable()
        p = _proposal()
        store.put(p)
        got = store.get(p.id)
        self.assertEqual(got.source_item["score"], 8.5)
        self.assertIsInstance(got.source_item["score"], float)
        self.assertEqual(got.source_item["chain_seq"], 71)
        self.assertIsInstance(got.source_item["chain_seq"], int)


class _Drafter:
    def draft(self, item):
        return {"worth_posting": True, "draft": "A considered reply.",
                "rationale": "on topic", "disclosures": [], "voice_ok": True}


class _BrokenStore:
    def pending(self, limit=50, tenant=None):
        return []

    def put(self, proposal):
        raise TypeError("Float types are not supported. Use Decimal types instead.")


class TestAFailedProposalIsNotFatal(unittest.TestCase):

    def test_propose_records_it_and_carries_on(self):
        cfg = {"drafting": {"enabled": True, "post_threshold": 6.0, "max_drafts_per_run": 3,
                            "network": "moltbook", "purpose": "news"}}
        recs = [{"source": "arxiv", "external_id": f"x{n}", "url": f"https://e.invalid/{n}",
                 "title": f"Paper {n}", "score": 9.0 - n, "chain_seq": n, "body": "b"} for n in range(2)]
        stats = _app.propose(cfg, recs, drafter=_Drafter(), store=_BrokenStore(), now=NOW)
        self.assertEqual(stats["proposed"], 0)
        self.assertEqual(len(stats["declined"]), 2)
        self.assertTrue(all("not stored" in d["reason"] for d in stats["declined"]))

    def test_the_digest_still_goes_when_proposing_blows_up(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        cfg = _app.load_config(os.path.join(ROOT, "config.toml"))
        batch = [Item(source="arxiv", external_id=f"x{n}", url=f"https://arxiv.org/abs/x{n}",
                      title=f"Healthcare AI governance in practice {n}", author="a",
                      published=NOW - timedelta(hours=2),
                      body="clinical AI governance and healthcare AI oversight") for n in range(3)]

        class _Mail:
            sent = []

            def send(self, subject, text, html_body):
                self.sent.append(subject)
                return True

        def boom(*a, **k):
            raise RuntimeError("anything at all")

        mail = _Mail()
        with mock.patch.object(_app, "collect", lambda c, n: (list(batch), ["arxiv"], [], [])), \
             mock.patch.object(_app, "propose", boom), \
             self.assertLogs("vigil.scout", level="ERROR") as cap:
            stats = _app.run(cfg, hit_store=LocalHitStore(os.path.join(d.name, "hits.json")),
                             chain_store=LocalChainStore(os.path.join(d.name, "chain.jsonl")),
                             mailer=mail, now=NOW)
        self.assertTrue(stats["stats"]["above"] >= 1, "the fixture must clear the digest threshold")
        self.assertEqual(len(mail.sent), 1, "the digest went out despite the failure")
        self.assertIn("proposing failed; the digest still goes", "\n".join(cap.output))


if __name__ == "__main__":
    unittest.main()
