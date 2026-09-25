"""An off-box copy of the agent's memory, because losing it loses the person.

The Glass Ledger has an Object Lock witness bucket, a pinned key, a
checkpoint and an off-box auditor. The memory had nothing at all: one SQLite
file, on one EBS volume, on one instance, with zero snapshots behind it.

That was defensible while the memory held "root was at 24%". It stopped
being defensible the moment it held an operator profile -- eighteen lines
about a person, curated by him, plus his goals, the proposals he has ruled
on and everything the agent has worked out. Losing the instance would mean
retyping all of it, and the agent starting again not knowing anyone.

So it is copied, and the shape of the copy is deliberately *unlike* the
ledger's, because the two hold different things.

    The ledger is evidence, and evidence must be impossible to alter. Object
    Lock in COMPLIANCE mode, write-only from the instance, never read back.

    The memory is a person's own data, and a person must be able to erase it.
    No Object Lock, versioned so a bad upload cannot destroy a good copy, and
    readable back by the instance -- because unlike the ledger, restoring it
    is the entire point.

Three things learned elsewhere in this codebase and applied here.

**A consistent copy, not a file copy.** A live SQLite database has a
write-ahead log beside it. Copying the .db alone gives a file that opens
cleanly and has silently lost whatever was in the WAL, which is the worst
kind of backup: one that looks healthy. It goes through SQLite's backup API.

**Configured is not proven.** The channel taught this: a backup that has
never run reports itself ready, and the first time anyone finds out is the
restore that matters. So it keeps a durable record of whether a copy has
ever actually landed, and the health check says plainly when one has not.

**What was not saved is said, not omitted.** A failed upload is reported
with its reason rather than passed over until next hour.
"""

import hashlib
import json
import os
import tempfile
import time
from typing import Optional

DEFAULT_EVERY_S = 3600.0        # memory changes slowly; the ledger is the fast one
DEFAULT_PREFIX = "memory"
STATE_FILE = "/var/lib/jarvis/backup.state"
# One stable key. The bucket is versioned, so every upload is a new version
# and the newest is always here -- which makes "restore the latest" a GET
# rather than a listing, and makes a rollback a version id.
OBJECT_NAME = "memory.db"
MANIFEST_NAME = "memory.json"


class MemoryBackup:
    """Copies the agent's durable memory off the box on a slow clock."""

    def __init__(self, config: Optional[dict], store, logger,
                 client=None, region: Optional[str] = None,
                 instance_id: Optional[str] = None, clock=time.time):
        cfg = dict(config or {})
        self.bucket = cfg.get("bucket") or None
        self.prefix = str(cfg.get("prefix") or DEFAULT_PREFIX).strip("/")
        self.every = max(300.0, float(cfg.get("every_s") or DEFAULT_EVERY_S))
        self.state_file = cfg.get("state_file") or STATE_FILE
        self.region = region
        self.instance_id = instance_id
        self.store = store
        self.log = logger
        self.clock = clock
        self._client = client
        self.enabled = bool(self.bucket and getattr(store, "available", False))
        self.last_error: Optional[str] = None
        self.last_attempt_at: Optional[float] = None
        self.stats = {"copies": 0, "failures": 0}
        # Durable, for the same reason the channel's is: in-process counters
        # reset on every deploy, and "has this ever worked" is exactly the
        # fact a restart must not erase.
        self.copied_ever = 0
        self.last_ok_at: Optional[float] = None
        self.last_rows: Optional[int] = None
        self._load_state()

    # ---- state ---------------------------------------------------------

    @property
    def proven(self) -> bool:
        """Has a copy ever actually landed in the bucket?"""
        return self.copied_ever > 0

    def due(self, now: Optional[float] = None) -> bool:
        if not self.enabled:
            return False
        now = self.clock() if now is None else now
        if self.last_attempt_at is None:
            return True
        return (now - self.last_attempt_at) >= self.every

    def status(self) -> dict:
        now = self.clock()
        out = {"enabled": self.enabled, "bucket": self.bucket or None,
               "proven": self.proven, "copies_ever": self.copied_ever,
               "every_s": self.every, "stats": dict(self.stats),
               "last_error": self.last_error}
        if self.last_ok_at:
            out["last_copy_s_ago"] = max(0, round(now - self.last_ok_at))
            out["last_rows"] = self.last_rows
        if not self.enabled:
            out["reason"] = ("no bucket configured" if not self.bucket
                             else "no durable memory to copy")
        return out

    def _load_state(self):
        try:
            with open(self.state_file) as f:
                data = json.load(f)
            self.copied_ever = max(0, int(data.get("copies") or 0))
            self.last_ok_at = data.get("last_ok_at") or None
            self.last_rows = data.get("rows")
        except FileNotFoundError:
            pass
        except Exception as exc:
            self.log.debug("Could not read backup state: %s", exc)

    def _save_state(self):
        try:
            directory = os.path.dirname(self.state_file)
            if directory:
                os.makedirs(directory, exist_ok=True)
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"copies": self.copied_ever,
                           "last_ok_at": self.last_ok_at,
                           "rows": self.last_rows}, f)
            os.replace(tmp, self.state_file)
        except Exception as exc:
            self.log.debug("Could not write backup state: %s", exc)

    # ---- doing it ------------------------------------------------------

    def _boto(self):
        if self._client is not None:
            return self._client
        import boto3
        self._client = boto3.client("s3", region_name=self.region or None)
        return self._client

    def run(self, now: Optional[float] = None) -> dict:
        """Take a consistent copy and put it in the bucket. Never raises."""
        now = self.clock() if now is None else now
        if not self.enabled:
            return {"copied": False, "reason": self.status().get("reason")}
        self.last_attempt_at = now
        workdir = tempfile.mkdtemp(prefix="jarvis-backup-")
        local = os.path.join(workdir, OBJECT_NAME)
        try:
            if not self.store.snapshot_to(local):
                self.stats["failures"] += 1
                self.last_error = "could not take a consistent snapshot"
                return {"copied": False, "reason": self.last_error}
            size = os.path.getsize(local)
            digest = _sha256(local)
            rows = _row_count(local)
            manifest = {
                "taken_at": now,
                "taken_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                "sha256": digest, "bytes": size, "rows": rows,
                "instance": self.instance_id,
                # What is in it, so a restore can be sanity-checked without
                # opening a database full of someone's personal notes.
                "kinds": _kind_counts(local),
            }
            client = self._boto()
            key = f"{self.prefix}/{OBJECT_NAME}" if self.prefix else OBJECT_NAME
            meta = f"{self.prefix}/{MANIFEST_NAME}" if self.prefix else MANIFEST_NAME
            with open(local, "rb") as f:
                client.put_object(Bucket=self.bucket, Key=key, Body=f.read(),
                                  ServerSideEncryption="AES256",
                                  ContentType="application/x-sqlite3")
            client.put_object(Bucket=self.bucket, Key=meta,
                              Body=json.dumps(manifest, indent=1).encode(),
                              ServerSideEncryption="AES256",
                              ContentType="application/json")
        except Exception as exc:
            self.stats["failures"] += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.log.warning("Memory backup failed: %s", self.last_error)
            return {"copied": False, "reason": self.last_error}
        finally:
            _clean(workdir)

        self.stats["copies"] += 1
        self.copied_ever += 1
        self.last_ok_at = now
        self.last_rows = rows
        self.last_error = None
        self._save_state()
        self.log.info("Memory copied off-box: %d rows, %d bytes, sha256 %s",
                      rows, size, digest[:12])
        return {"copied": True, "rows": rows, "bytes": size,
                "sha256": digest, "bucket": self.bucket, "key": key}

    def fetch(self, destination: str, version_id: Optional[str] = None) -> dict:
        """Bring a copy back down, and check it is what the manifest says.

        Unlike the ledger, which the instance may never read back, restoring
        this is the whole reason it exists. Verified against the manifest
        because a backup nobody has ever restored is a hope.
        """
        if not self.bucket:
            return {"restored": False, "reason": "no bucket configured"}
        key = f"{self.prefix}/{OBJECT_NAME}" if self.prefix else OBJECT_NAME
        meta = f"{self.prefix}/{MANIFEST_NAME}" if self.prefix else MANIFEST_NAME
        try:
            client = self._boto()
            extra = {"VersionId": version_id} if version_id else {}
            body = client.get_object(Bucket=self.bucket, Key=key, **extra)["Body"].read()
            directory = os.path.dirname(destination)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(destination, "wb") as f:
                f.write(body)
            os.chmod(destination, 0o600)
            manifest = {}
            try:
                manifest = json.loads(
                    client.get_object(Bucket=self.bucket, Key=meta)["Body"].read())
            except Exception:
                manifest = {}
        except Exception as exc:
            return {"restored": False, "reason": f"{type(exc).__name__}: {exc}"}

        got = _sha256(destination)
        rows = _row_count(destination)
        out = {"restored": True, "path": destination, "sha256": got,
               "rows": rows, "taken_at_utc": manifest.get("taken_at_utc")}
        if manifest.get("sha256") and manifest["sha256"] != got:
            # Said rather than swallowed. A copy that does not match its
            # manifest is still handed over -- it may be all there is -- but
            # nobody should restore it believing it is intact.
            out["intact"] = False
            out["warning"] = ("this copy does not match its manifest: expected "
                              f"{manifest['sha256'][:12]}, got {got[:12]}. It "
                              "may be truncated or from a different upload.")
        else:
            out["intact"] = bool(manifest.get("sha256"))
        return out


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _row_count(path: str) -> int:
    try:
        import sqlite3
        db = sqlite3.connect(path)
        try:
            return int(db.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
        finally:
            db.close()
    except Exception:
        return 0


def _kind_counts(path: str) -> dict:
    try:
        import sqlite3
        db = sqlite3.connect(path)
        try:
            return {str(k): int(n) for k, n in db.execute(
                "SELECT kind, COUNT(*) FROM memories WHERE state = 'live' "
                "GROUP BY kind")}
        finally:
            db.close()
    except Exception:
        return {}


def _clean(directory: str):
    try:
        for name in os.listdir(directory):
            os.unlink(os.path.join(directory, name))
        os.rmdir(directory)
    except OSError:
        pass


def build_backup(config: Optional[dict], store, logger, client=None,
                 region: Optional[str] = None, instance_id: Optional[str] = None):
    """From the cloud.memory_backup block. No bucket means switched off."""
    cloud = (config or {}).get("cloud") or {}
    cfg = dict(cloud.get("memory_backup") or {})
    if not cfg.get("region"):
        cfg["region"] = region or ((cloud.get("instance") or {}).get("region"))
    return MemoryBackup(cfg, store, logger, client=client,
                        region=cfg.get("region"),
                        instance_id=instance_id
                        or (cloud.get("instance") or {}).get("instance_id"))
