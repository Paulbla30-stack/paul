"""Off-box witness copy of the ledger: checkpoint pins and a file copy in S3.

The paper's trust boundary is that the witness never lives with the
writer. On AWS the nearest thing the writer can reach but not revoke is
an S3 bucket with Object Lock: the instance role may only PutObject, and
every version it writes is retained for the bucket's retention period,
so a compromised or confused agent cannot unsay what it already said.
The audit itself (``aws/scripts/ledger_audit.py``) runs elsewhere,
against that copy, with a public key and a pin the writer never sees.

Anchoring never blocks the agent: a failed upload is logged, counted and
retried on the next tick, and shows in the ledger status.
"""

import hashlib
import base64
import json
import os
import time
from typing import Optional

from openclaw.ledger.chain import FORMAT, utc_now


class LedgerAnchor:
    def __init__(self, config: dict, logger, ledger_path: str, pubkey: str, writer: str,
                 client=None, region: Optional[str] = None, instance_id: Optional[str] = None):
        cfg = dict(config or {})
        self.bucket = cfg.get("bucket") or None
        self.prefix = str(cfg.get("prefix") or "ledger").strip("/")
        self.every = max(15.0, float(cfg.get("every_s") or 300))
        self.copy = bool(cfg.get("copy", True))
        self.region = region
        self.log = logger
        self.path = ledger_path
        self.pubkey = pubkey
        self.writer = writer
        self.instance_id = instance_id
        self.client = client
        self.enabled = bool(self.bucket)
        self._dirty_head: Optional[dict] = None
        self._last_flush = 0.0
        self.last_error: Optional[str] = None
        self.stats = {"checkpoints": 0, "copies": 0, "failures": 0}
        self.last_anchored: Optional[dict] = None
        self.failure_streak = 0

    def _client(self):
        if self.client is None:
            import boto3
            self.client = boto3.client("s3", region_name=self.region) if self.region else boto3.client("s3")
        return self.client

    def key(self, name: str) -> str:
        return f"{self.prefix}/{self.writer}/{name}"

    def notify(self, head: dict):
        self._dirty_head = dict(head)

    def due(self) -> bool:
        return self.enabled and self._dirty_head is not None and \
            time.time() - self._last_flush >= self.every

    def flush(self, force: bool = False) -> bool:
        """Upload the pending checkpoint (and copy). Returns True on success."""
        if not self.enabled or self._dirty_head is None:
            return False
        if not force and time.time() - self._last_flush < self.every:
            return False
        head = self._dirty_head
        checkpoint = {"format": FORMAT, "writer": self.writer, "instance_id": self.instance_id,
                      "pubkey": self.pubkey, "seq": head["seq"], "entry_hash": head["entry_hash"],
                      "anchored_at": utc_now()}
        try:
            s3 = self._client()
            self._put(s3, self.key(f"checkpoints/{head['seq']:012d}.json"),
                      json.dumps(checkpoint, indent=1).encode(), "application/json")
            self._put(s3, self.key("checkpoint.json"),
                      json.dumps(checkpoint, indent=1).encode(), "application/json")
            self.stats["checkpoints"] += 1
            if self.copy:
                with open(self.path, "rb") as fh:
                    data = fh.read()
                self._put(s3, self.key("ledger.jsonl"), data, "application/x-ndjson")
                self.stats["copies"] += 1
        except Exception as e:
            self.stats["failures"] += 1
            self.failure_streak += 1
            self.last_error = f"{type(e).__name__}: {e}"
            if self.failure_streak in (1, 5, 20):
                self.log.warning("Ledger anchor upload to s3://%s failed (%s)", self.bucket, self.last_error)
            self._last_flush = time.time()  # do not hammer a broken bucket
            return False
        self.failure_streak = 0
        self.last_error = None
        self.last_anchored = checkpoint
        self._last_flush = time.time()
        if self._dirty_head == head:
            self._dirty_head = None
        return True

    def _put(self, s3, key: str, data: bytes, content_type: str):
        md5 = base64.b64encode(hashlib.md5(data).digest()).decode()
        s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type,
                      ContentMD5=md5)

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "bucket": self.bucket,
            "prefix": self.key("") if self.enabled else None,
            "every_s": self.every,
            "copy": self.copy,
            "pending": self._dirty_head,
            "last_anchored": self.last_anchored,
            "last_error": self.last_error,
            "stats": dict(self.stats),
        }
