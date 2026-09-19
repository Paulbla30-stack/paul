"""Tests for memory consolidation.

The store was a flat list: everything the same weight, kept until a row cap
pushed the oldest out, handed to the model whole on every cycle. The operator's
diagnosis was that we throw everything at it and expect it to organise it in
flight. This is the pass that organises it first.

Most of these tests are about what consolidation must NOT do, because an
editorial act performed by the thing being edited needs rules it cannot talk
its way around.
"""

import logging
import os
import tempfile
import time
import unittest

from jarvis.agent.consolidate import (Consolidator, measurement_of, similarity,
                                      keywords)
from jarvis.agent.store import MemoryStore, NullStore

LOG = logging.getLogger("test")


class FakeLedger:
    def __init__(self):
        self.entries = []

    def record(self, kind, body):
        self.entries.append((kind, body))
        return True


class Base(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(os.path.join(self.tmp.name, "m.db"), logger=LOG)
        self.ledger = FakeLedger()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def consolidator(self, **cfg):
        settings = {"min_group": 3, "similarity": 0.5, "promote_at": 5}
        settings.update(cfg)
        return Consolidator(self.store, LOG, settings, self.ledger)


class TestNothingIsDestroyed(Base):
    """The rule that does not bend. A model that can quietly erase its own
    past is doing what the Glass Ledger exists to prevent, inside the one
    store the ledger does not cover."""

    def test_a_merge_keeps_every_source_on_disk(self):
        for text in ("the security scan found no new findings this hour",
                     "the security scan found no new findings again this hour",
                     "the security scan reported no new findings this hour at all"):
            self.store.remember(text)
        before = self.store.stats()["entries"]
        report = self.consolidator().run()
        self.assertEqual(len(report["merged"]), 1)
        # one new derived row, and not one original removed
        self.assertEqual(self.store.stats()["entries"], before + 1)
        merged = report["merged"][0]
        self.assertEqual(len(merged["sources"]), 3)
        for source_id in merged["sources"]:
            row = self.store._query("SELECT * FROM memories WHERE id = ?",
                                    (source_id,))[0]
            self.assertEqual(row["state"], "dormant")
            self.assertTrue(row["text"])

    def test_a_contradiction_supersedes_and_keeps_the_history(self):
        """How a thing changed is often the useful part."""
        self.store.remember("the root filesystem is 8 GB")
        time.sleep(0.01)
        self.store.remember("the root filesystem is 20 GB")
        report = self.consolidator().run()
        self.assertEqual(len(report["superseded"]), 1)
        event = report["superseded"][0]
        self.assertEqual(event["current_value"], 20.0)
        self.assertEqual(event["was"], [8.0])
        live = [r["text"] for r in self.store.live(10)]
        self.assertIn("the root filesystem is 20 GB", live)
        self.assertNotIn("the root filesystem is 8 GB", live)
        old = self.store._query("SELECT * FROM memories WHERE id = ?",
                                (event["retired"][0],))[0]
        self.assertEqual(old["state"], "superseded")


class TestDerivedIsNeverConsolidatedAgain(Base):
    """Summarising a summary, and then that, is how a memory becomes a
    confident fiction with no provenance left."""

    def test_a_merged_entry_is_not_an_input_to_the_next_pass(self):
        for i in range(3):
            self.store.remember(f"the security scan found no new findings run {i}")
        first = self.consolidator().run()
        self.assertEqual(len(first["merged"]), 1)
        merged_id = first["merged"][0]["id"]
        row = self.store._query("SELECT * FROM memories WHERE id = ?", (merged_id,))[0]
        self.assertEqual(row["derived"], 1)
        # feed it more of the same shape and run again
        for i in range(3, 6):
            self.store.remember(f"the security scan found no new findings run {i}")
        second = self.consolidator().run()
        for merge in second["merged"]:
            self.assertNotIn(merged_id, merge["sources"])

    def test_live_can_exclude_derived_entries(self):
        self.store.remember_derived("a distilled thing", [1, 2, 3])
        self.store.remember("an observed thing")
        self.assertEqual(len(self.store.live(include_derived=True)), 2)
        self.assertEqual(len(self.store.live(include_derived=False)), 1)


class TestWeight(Base):

    def test_use_strengthens_and_neglect_fades(self):
        entry = self.store.remember("something worth keeping")
        self.store.reinforce([entry["id"]], amount=0.5)
        row = self.store.live(1)[0]
        self.assertAlmostEqual(row["weight"], 1.5)
        self.assertEqual(row["used"], 1)
        # decay only touches what has not been used lately
        self.assertEqual(self.store.decay(0.5, older_than_s=86400), 0)
        self.assertEqual(self.store.decay(0.5, older_than_s=0), 1)
        self.assertAlmostEqual(self.store.live(1)[0]["weight"], 0.75)

    def test_pinned_memories_do_not_fade(self):
        self.store.remember("a standing fact", pinned=True)
        self.store.decay(0.1, older_than_s=0)
        self.assertAlmostEqual(self.store.live(1)[0]["weight"], 1.0)

    def test_pruning_drops_the_weakest_not_merely_the_oldest(self):
        """Age alone would drop a hard-won fact from week one to make room
        for this morning's sixth 'disk is fine', which is backwards."""
        old = self.store.remember("a hard-won fact from week one")
        self.store.reinforce([old["id"]], amount=3.0)
        for i in range(12):
            self.store.remember(f"routine chatter {i}")
        self.store.prune(max_rows=10)
        kept = [r["text"] for r in self.store.recent(20)]
        self.assertIn("a hard-won fact from week one", kept)

    def test_a_derived_entry_survives_pruning(self):
        """It is the distilled form of memories already let go."""
        derived = self.store.remember_derived("distilled", [1, 2, 3])
        for i in range(20):
            self.store.remember(f"chatter {i}")
        self.store.prune(max_rows=10)
        rows = self.store._query("SELECT * FROM memories WHERE id = ?",
                                 (derived["id"],))
        self.assertEqual(len(rows), 1)


class TestPromotion(Base):

    def test_what_keeps_being_re_established_becomes_standing(self):
        for _ in range(6):
            self.store.remember("the planner is an imported Qwen3-32B on Bedrock")
        report = self.consolidator().run()
        self.assertEqual(len(report["promoted"]), 1)
        self.assertTrue(self.store.live(5)[0]["pinned"])

    def test_something_seen_twice_is_not_promoted(self):
        for _ in range(2):
            self.store.remember("a passing observation")
        self.assertEqual(self.consolidator().run()["promoted"], [])


class TestTheParsers(unittest.TestCase):

    def test_measurements_are_recognised_with_their_units(self):
        self.assertEqual(measurement_of("the root filesystem is 20 GB"),
                         ("root filesystem", 20.0, "gb"))
        self.assertEqual(measurement_of("disk usage at 84%"),
                         ("disk usage", 84.0, "%"))
        self.assertIsNone(measurement_of("the scan found nothing"))

    def test_different_units_are_different_subjects(self):
        """20GB and 20% are not two readings of one thing."""
        a = measurement_of("the root filesystem is 20 GB")
        b = measurement_of("the root filesystem is 20 %")
        self.assertNotEqual(a[2], b[2])

    def test_similarity_ignores_filler_words(self):
        self.assertGreater(similarity("the security scan found no new findings",
                                      "a security scan has found no new findings"), 0.7)
        self.assertLess(similarity("the disk is nearly full",
                                   "the planner is on Bedrock"), 0.2)
        self.assertNotIn("the", keywords("the disk is full"))


class TestItNeverBreaksTheLoop(Base):

    def test_no_durable_memory_is_a_skip_not_a_crash(self):
        c = Consolidator(NullStore(), LOG, {}, self.ledger)
        report = c.run()
        self.assertEqual(report["skipped"], "no durable memory")

    def test_disabled_is_a_skip(self):
        report = self.consolidator(enabled=False).run()
        self.assertEqual(report["skipped"], "consolidation disabled")

    def test_a_dry_run_changes_nothing(self):
        for i in range(3):
            self.store.remember(f"the security scan found no new findings run {i}")
        before = self.store.stats()["entries"]
        report = self.consolidator().run(dry_run=True)
        self.assertEqual(len(report["merged"]), 1)
        self.assertIsNone(report["merged"][0]["id"])
        self.assertEqual(self.store.stats()["entries"], before)
        self.assertEqual(self.ledger.entries, [])

    def test_a_broken_store_is_reported_not_raised(self):
        class Broken(MemoryStore):
            def live(self, *a, **k):
                raise RuntimeError("disk gone")
        broken = Broken(os.path.join(self.tmp.name, "b.db"), logger=LOG)
        report = Consolidator(broken, LOG, {}, self.ledger).run()
        self.assertIn("disk gone", report["skipped"])
        broken.close()


class TestTheRecord(Base):

    def test_a_pass_that_changed_something_is_ledgered(self):
        """How the memory got its shape is itself worth auditing."""
        for i in range(3):
            self.store.remember(f"the security scan found no new findings run {i}")
        self.consolidator().run()
        kinds = [k for k, _ in self.ledger.entries]
        self.assertIn("consolidation", kinds)
        body = dict(self.ledger.entries[0][1])
        self.assertEqual(len(body["merged"]), 1)
        self.assertEqual(len(body["merged"][0]["sources"]), 3)

    def test_a_quiet_pass_is_not_an_event(self):
        self.store.remember("one lonely observation")
        self.consolidator().run()
        self.assertEqual(self.ledger.entries, [])


if __name__ == "__main__":
    unittest.main()
