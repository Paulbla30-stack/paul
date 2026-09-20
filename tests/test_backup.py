"""An off-box copy of the memory, because losing it loses the person.

The ledger had an Object Lock witness, a pinned key and an off-box auditor.
The memory had one SQLite file on one volume with zero snapshots behind it --
fine while it held "root was at 24%", not fine once it held a profile of a
person, his goals and the proposals he has ruled on.

The tests that matter are the ones about the copy being *usable*: a
consistent snapshot rather than a file copy, a restore that is verified
against its manifest, and a backup that says plainly it has never run.
"""

import json
import logging
import os
import sqlite3
import tempfile
import unittest

from jarvis.agent.backup import MemoryBackup
from jarvis.agent.store import build_store

LOG = logging.getLogger("test")
NOW = 1_800_000_000.0


class FakeS3:
    """Enough S3 to be wrong in the ways S3 is wrong."""

    def __init__(self, fail_on=None):
        self.objects = {}
        self.calls = []
        self.fail_on = fail_on

    def put_object(self, Bucket, Key, Body, **kw):
        self.calls.append(("put", Key, kw))
        if self.fail_on == "put":
            raise RuntimeError("AccessDenied")
        self.objects[Key] = Body
        return {}

    def get_object(self, Bucket, Key, **kw):
        if self.fail_on == "get" or Key not in self.objects:
            raise RuntimeError("NoSuchKey")

        class Body:
            def __init__(self, data):
                self.data = data

            def read(self):
                return self.data
        return {"Body": Body(self.objects[Key])}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = build_store({"path": os.path.join(self.tmp, "m.db")}, LOG)
        for i in range(25):
            self.store.remember(f"something worth keeping number {i}", kind="note")
        self.store.remember("He has said trust is always a two way street",
                            kind="operator", source="operator", pinned=True,
                            meta={"profile": True, "source": "stated"})
        self.s3 = FakeS3()

    def tearDown(self):
        self.store.close()

    def backup(self, **cfg):
        settings = {"bucket": "jarvis-memory", "prefix": "memory",
                    "state_file": os.path.join(self.tmp, "backup.state")}
        settings.update(cfg)
        return MemoryBackup(settings, self.store, LOG, client=self.s3,
                            instance_id="i-test", clock=lambda: NOW)


class TestTheCopyIsConsistent(Base):
    """A live SQLite file has a WAL beside it. Copying the .db alone gives a
    backup that opens cleanly and has silently lost the last hour."""

    def test_the_snapshot_is_a_database_not_a_moment(self):
        out = os.path.join(self.tmp, "snap.db")
        self.assertTrue(self.store.snapshot_to(out))
        self.store.remember("written after the snapshot", kind="note")
        db = sqlite3.connect(out)
        after = db.execute("SELECT COUNT(*) FROM memories WHERE text LIKE "
                           "'%after the snapshot%'").fetchone()[0]
        db.close()
        self.assertEqual(after, 0)

    def test_the_snapshot_holds_everything_up_to_that_point(self):
        out = os.path.join(self.tmp, "snap.db")
        self.store.snapshot_to(out)
        db = sqlite3.connect(out)
        rows = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        db.close()
        self.assertEqual(rows, 26)

    def test_the_snapshot_is_not_world_readable(self):
        out = os.path.join(self.tmp, "snap.db")
        self.store.snapshot_to(out)
        self.assertEqual(os.stat(out).st_mode & 0o777, 0o600)

    def test_a_store_with_no_database_cannot_pretend_to_snapshot(self):
        from jarvis.agent.store import NullStore
        self.assertFalse(NullStore().snapshot_to(os.path.join(self.tmp, "x.db")))


class TestItActuallyLandsSomewhere(Base):

    def test_a_copy_goes_up_encrypted(self):
        got = self.backup().run(NOW)
        self.assertTrue(got["copied"])
        self.assertEqual(got["rows"], 26)
        self.assertIn("memory/memory.db", self.s3.objects)
        put = next(c for c in self.s3.calls if c[1].endswith("memory.db"))
        self.assertEqual(put[2]["ServerSideEncryption"], "AES256")

    def test_a_manifest_goes_with_it(self):
        self.backup().run(NOW)
        manifest = json.loads(self.s3.objects["memory/memory.json"])
        self.assertEqual(manifest["rows"], 26)
        self.assertEqual(manifest["instance"], "i-test")
        self.assertIn("note", manifest["kinds"])
        self.assertEqual(len(manifest["sha256"]), 64)

    def test_one_stable_key_so_the_latest_is_a_get_not_a_listing(self):
        b = self.backup()
        b.run(NOW)
        b.run(NOW + 10_000)
        self.assertEqual(sorted(self.s3.objects), ["memory/memory.db",
                                                   "memory/memory.json"])

    def test_a_refused_upload_is_reported_rather_than_raised(self):
        self.s3.fail_on = "put"
        got = self.backup().run(NOW)
        self.assertFalse(got["copied"])
        self.assertIn("AccessDenied", got["reason"])

    def test_nothing_is_left_behind_on_disk(self):
        before = set(os.listdir(tempfile.gettempdir()))
        self.backup().run(NOW)
        leftovers = [d for d in set(os.listdir(tempfile.gettempdir())) - before
                     if d.startswith("jarvis-backup-")]
        self.assertEqual(leftovers, [])


class TestConfiguredIsNotProven(Base):
    """The channel taught this: the first time anyone finds out a backup
    never ran is the restore that matters."""

    def test_a_fresh_backup_is_unproven(self):
        b = self.backup()
        self.assertTrue(b.enabled)
        self.assertFalse(b.proven)

    def test_a_landed_copy_proves_it(self):
        b = self.backup()
        b.run(NOW)
        self.assertTrue(b.proven)
        self.assertEqual(b.status()["copies_ever"], 1)

    def test_a_failed_copy_proves_nothing(self):
        self.s3.fail_on = "put"
        b = self.backup()
        b.run(NOW)
        self.assertFalse(b.proven)
        self.assertIn("AccessDenied", b.status()["last_error"])

    def test_proof_survives_a_restart(self):
        self.backup().run(NOW)
        again = self.backup()
        self.assertTrue(again.proven)
        self.assertEqual(again.stats["copies"], 0)     # the counter reset
        self.assertEqual(again.copied_ever, 1)         # the fact did not

    def test_no_bucket_means_off_and_says_why(self):
        b = self.backup(bucket=None)
        self.assertFalse(b.enabled)
        self.assertIn("no bucket", b.status()["reason"])

    def test_it_runs_on_a_slow_clock(self):
        b = self.backup(every_s=3600)
        self.assertTrue(b.due(NOW))
        b.run(NOW)
        self.assertFalse(b.due(NOW + 60))
        self.assertTrue(b.due(NOW + 3601))

    def test_the_clock_has_a_floor(self):
        """Copying a database every ten seconds is not a backup strategy."""
        self.assertGreaterEqual(self.backup(every_s=1).every, 300)


class TestBringingItBack(Base):
    """Unlike the ledger, which is never read back, restoring is the point."""

    def test_a_restored_copy_is_the_same_database(self):
        b = self.backup()
        b.run(NOW)
        out = os.path.join(self.tmp, "restored.db")
        got = b.fetch(out)
        self.assertTrue(got["restored"])
        self.assertEqual(got["rows"], 26)
        db = sqlite3.connect(out)
        found = db.execute("SELECT COUNT(*) FROM memories WHERE text LIKE "
                           "'%two way street%'").fetchone()[0]
        db.close()
        self.assertEqual(found, 1)

    def test_it_is_checked_against_its_manifest(self):
        b = self.backup()
        b.run(NOW)
        got = b.fetch(os.path.join(self.tmp, "restored.db"))
        self.assertTrue(got["intact"])
        self.assertIn("taken_at_utc", got)

    def test_a_copy_that_does_not_match_is_handed_over_with_a_warning(self):
        """It may be all there is. Nobody should restore it believing it is
        intact."""
        b = self.backup()
        b.run(NOW)
        self.s3.objects["memory/memory.db"] = self.s3.objects["memory/memory.db"][:-200]
        got = b.fetch(os.path.join(self.tmp, "restored.db"))
        self.assertTrue(got["restored"])
        self.assertFalse(got["intact"])
        self.assertIn("does not match its manifest", got["warning"])

    def test_a_missing_copy_is_an_answer_not_a_crash(self):
        got = self.backup().fetch(os.path.join(self.tmp, "restored.db"))
        self.assertFalse(got["restored"])
        self.assertIn("NoSuchKey", got["reason"])

    def test_the_restored_file_is_not_world_readable(self):
        b = self.backup()
        b.run(NOW)
        out = os.path.join(self.tmp, "restored.db")
        b.fetch(out)
        self.assertEqual(os.stat(out).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
