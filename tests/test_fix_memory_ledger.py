"""Regression tests for the memory-store rules and the ledger's on-box checks.

Two rules in the store were breakable: a merge whose summary text already
existed turned that row into a derived one, and the row cap could delete the
sources a derived row cites. On the ledger side, the on-box verify took no pin,
so a ledger rolled back behind the last anchored checkpoint read INTACT, and a
bucket that stayed broken stopped being logged after the twentieth failure.
"""

import logging
import os
import tempfile
import unittest
from unittest import mock

try:
    from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: F401
    HAVE_CRYPTO = True
except Exception:  # pragma: no cover
    HAVE_CRYPTO = False

from jarvis.agent.consolidate import Consolidator
from jarvis.agent.store import MemoryStore
from jarvis.ledger.agent_ledger import AgentLedger
from jarvis.ledger.anchor import LedgerAnchor

needs_crypto = unittest.skipUnless(HAVE_CRYPTO, "cryptography not installed")
LOG = logging.getLogger("test")


class StoreBase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(os.path.join(self.tmp.name, "m.db"), logger=LOG)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def row(self, memory_id):
        rows = self.store._query("SELECT * FROM memories WHERE id = ?", (memory_id,))
        return rows[0] if rows else None


class TestADerivedWriteNeverConvertsAnExistingRow(StoreBase):

    def test_existing_original_is_refused_and_left_untouched(self):
        original = self.store.remember("the disk is fine")
        before = self.row(original["id"])
        self.assertIsNone(self.store.remember_derived("the disk is fine", [7, 8, 9],
                                                      weight=3.0))
        after = self.row(original["id"])
        self.assertFalse(after["derived"])
        self.assertIsNone(after["sources"])
        self.assertEqual(after["weight"], before["weight"])
        self.assertEqual(after["seen"], before["seen"])
        self.assertEqual(after["ts"], before["ts"])

    def test_existing_dormant_original_is_not_revived(self):
        original = self.store.remember("an old observation")
        self.store.set_state(original["id"], "dormant")
        self.assertIsNone(self.store.remember_derived("an old observation", [1]))
        self.assertEqual(self.row(original["id"])["state"], "dormant")

    def test_another_derivations_sources_are_not_rewritten(self):
        first = self.store.remember_derived("a summary", [1, 2, 3])
        self.assertIsNotNone(first)
        self.assertIsNone(self.store.remember_derived("a summary", [4, 5, 6]))
        self.assertEqual(self.row(first["id"])["sources"], [1, 2, 3])

    def test_the_same_derivation_again_is_refreshed(self):
        first = self.store.remember_derived("a summary", [1, 2, 3], weight=2.0)
        again = self.store.remember_derived("a summary", [3, 2, 1], weight=2.5)
        self.assertEqual(again["id"], first["id"])
        self.assertTrue(again["repeat"])
        row = self.row(first["id"])
        self.assertTrue(row["derived"])
        self.assertEqual(row["sources"], [1, 2, 3])
        self.assertAlmostEqual(row["weight"], 2.5)

    def test_consolidation_treats_the_refusal_as_a_rejected_merge(self):
        texts = ("the security scan found no new findings this hour",
                 "the security scan found no new findings again this hour",
                 "the security scan reported no new findings this hour at all")
        # The text the merge would write, already on disk as an original.
        longest = max(texts, key=len)
        clash = self.store.remember(f"{longest} (observed 3x within the hour)")
        self.store.set_state(clash["id"], "dormant")
        ids = [self.store.remember(t)["id"] for t in texts]
        consolidator = Consolidator(self.store, LOG,
                                    {"min_group": 3, "similarity": 0.5, "promote_at": 5})
        report = consolidator.run()
        self.assertEqual(report["merged"], [])
        clash_row = self.row(clash["id"])
        self.assertFalse(clash_row["derived"])
        self.assertEqual(clash_row["state"], "dormant")
        for memory_id in ids:
            self.assertEqual(self.row(memory_id)["state"], "live")


class TestTheCapCannotDeleteCitedSources(StoreBase):

    def test_sources_of_a_derived_row_survive_pruning(self):
        sources = [self.store.remember(f"observation {i}")["id"] for i in range(3)]
        derived = self.store.remember_derived("observation, three times", sources)
        for memory_id in sources:
            self.store.set_state(memory_id, "dormant")
        for i in range(20):
            self.store.remember(f"unrelated chatter {i}")
        self.store.prune(max_rows=10)
        for memory_id in sources:
            self.assertIsNotNone(self.row(memory_id), f"source {memory_id} was pruned")
        self.assertIsNotNone(self.row(derived["id"]))
        # The cap still applies to everything else.
        self.assertEqual(self.store.stats()["entries"], 10)

    def test_the_exemption_matches_whole_ids_only(self):
        rows = [self.store.remember(f"row {i}")["id"] for i in range(12)]
        self.assertEqual(rows[0], 1)
        self.assertEqual(rows[11], 12)
        # Row 12 is the weakest, so it is first in line without the exemption.
        with self.store._lock:
            self.store._db.execute("UPDATE memories SET weight = 0.1 WHERE id = 12")
            self.store._db.commit()
        self.store.remember_derived("about row twelve", [12])
        self.store.prune(max_rows=10)
        self.assertIsNotNone(self.row(12))
        # "12" contains "1" but does not cite it; row 1 is still prunable.
        self.assertIsNone(self.row(1))

    def test_the_cap_still_bites_when_the_weakest_rows_are_all_cited(self):
        rows = [self.store.remember(f"row {i}")["id"] for i in range(30)]
        weakest = rows[:8]
        with self.store._lock:
            self.store._db.execute(
                "UPDATE memories SET weight = 0.1 WHERE id IN (%s)" % ",".join("?" * 8),
                weakest)
            self.store._db.commit()
        self.store.remember_derived("the eight weakest", weakest)
        self.store.prune(max_rows=10)
        for memory_id in weakest:
            self.assertIsNotNone(self.row(memory_id))
        self.assertEqual(self.store.stats()["entries"], 10)

    def seed(self, count, tag):
        with self.store._lock:
            self.store._db.executemany(
                "INSERT INTO memories (ts, kind, source, text, digest, pinned, weight, seen)"
                " VALUES (?, 'fact', 'system', ?, ?, 0, 1.0, 1)",
                [(float(i), f"{tag} {i}", f"{tag}-{i}") for i in range(count)])
            self.store._db.commit()

    def test_the_exemption_is_not_evaluated_once_per_candidate(self):
        # At the default cap prune runs on every write. A correlated subquery
        # over `sources` costs cap x rows and took ~240 ms per write here; the
        # cited set read once costs a few tens of thousands of VM steps.
        # SQLite's progress handler counts those steps, which is deterministic
        # where a wall-clock bound would not be.
        # Hold the automatic prune off while the scene is built, so every
        # cited id exists when the measured prune runs.
        self.store.max_rows = 10 ** 6
        self.seed(2000, "old")
        for j in range(50):
            self.store.remember_derived(f"merge {j}", [j * 5 + k + 1 for k in range(5)])
        self.seed(5, "new")
        steps = [0]

        def tick():
            steps[0] += 1
            return 0
        self.store._db.set_progress_handler(tick, 1000)
        try:
            dropped = self.store.prune(max_rows=2000)
        finally:
            self.store._db.set_progress_handler(None, 1000)
        self.assertEqual(dropped, 55)
        self.assertEqual(self.store.stats()["entries"], 2000)
        self.assertLess(steps[0], 1000, f"prune took {steps[0]}k VM steps")
        for j in range(250):
            self.assertIsNotNone(self.row(j + 1), f"cited source {j + 1} was pruned")


class FakeS3:
    def __init__(self, fail=False):
        self.objects = {}
        self.fail = fail

    def put_object(self, **kw):
        if self.fail:
            raise RuntimeError("AccessDenied")
        self.objects[kw["Key"]] = kw


@needs_crypto
class TestOnBoxVerifyIsPinnedToTheAnchor(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {"enabled": True, "path": os.path.join(self.tmp.name, "ledger.jsonl"),
                    "key_file": os.path.join(self.tmp.name, "ed25519.key"),
                    "pubkey_file": os.path.join(self.tmp.name, "ed25519.pub"),
                    "anchor": {"bucket": "witness", "every_s": 15}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_rollback_behind_the_anchor_is_not_intact(self):
        led = AgentLedger(self.cfg, LOG, writer="t", anchor_client=FakeS3())
        for n in range(3):
            self.assertTrue(led.record("action", {"n": n}))
        self.assertTrue(led.anchor.flush(force=True))
        self.assertEqual(led.anchor.last_anchored["seq"], 3)
        report = led.verify()
        self.assertTrue(report["ok"])
        self.assertTrue(report["pin_ok"])
        # Roll back to seq 1 and leave a torn tail, as a power cut would.
        with open(self.cfg["path"], "rb") as fh:
            lines = [ln for ln in fh.read().split(b"\n") if ln]
        with open(self.cfg["path"], "wb") as fh:
            fh.write(b"\n".join(lines[:2]) + b"\n" + b'{"seq": 2, "ts')
        report = led.verify()
        self.assertFalse(report["ok"])
        self.assertEqual(report["verdict"], "BROKEN")
        self.assertIn("rolled back", report["reason"])
        led.close()

    def test_without_an_anchored_checkpoint_verify_is_unpinned(self):
        led = AgentLedger(dict(self.cfg, anchor={}), LOG, writer="t")
        led.record("action", {"n": 1})
        report = led.verify()
        self.assertTrue(report["ok"])
        self.assertIsNone(report["pin"])
        led.close()


class TestAnchorKeepsReportingAStuckBucket(unittest.TestCase):

    def test_failures_are_logged_every_twentieth_after_the_first_few(self):
        log = mock.Mock()
        anchor = LedgerAnchor({"bucket": "witness"}, log, "/nonexistent", "pub", "w",
                              client=FakeS3(fail=True))
        anchor.notify({"seq": 1, "entry_hash": "x"})
        for _ in range(60):
            self.assertFalse(anchor.flush(force=True))
        self.assertEqual(anchor.failure_streak, 60)
        self.assertEqual(log.warning.call_count, 5)       # 1, 5, 20, 40, 60


if __name__ == "__main__":
    unittest.main()
