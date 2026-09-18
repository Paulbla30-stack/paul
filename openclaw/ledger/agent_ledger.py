"""The agent's side of the Glass Ledger: open, record, fail closed.

``AgentLedger`` owns the signing key and the writer, and gives the agent
one call: ``record(kind, body) -> bool``. When it returns False the entry
was not written, and with ``fail_closed`` (the default) the agent must
not act: no record, no action, no answer. The reason is exposed on
``status()`` so the operator can see what to fix (a torn tail after a
power loss, a locked file, a missing dependency). The agent never reads
its own ledger back for planning: the ledger is evidence about the
agent, not context for it.
"""

import hashlib
import os
import threading
import time
from typing import Optional

from openclaw.ledger.chain import (LedgerError, LedgerLocked, TornTail, LedgerWriter,
                                   generate_key, load_private_key, public_key_hex, parse_line)
from openclaw.ledger.verify import verify_file
from openclaw.ledger.anchor import LedgerAnchor

DEFAULT_PATH = "/var/lib/openclaw/ledger.jsonl"
DEFAULT_KEY = "/etc/openclaw/ledger/ed25519.key"
DEFAULT_PUB = "/etc/openclaw/ledger/ed25519.pub"


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()


def sha256_file(path: str, limit: int = 256 << 20) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            read = 0
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
                read += len(chunk)
                if read > limit:
                    return None
        return h.hexdigest()
    except OSError:
        return None


class AgentLedger:
    def __init__(self, config: Optional[dict], logger, writer: str = "openclaw",
                 region: Optional[str] = None, instance_id: Optional[str] = None,
                 anchor_client=None):
        cfg = dict(config or {})
        self.enabled = bool(cfg.get("enabled", False))
        self.path = cfg.get("path") or DEFAULT_PATH
        self.key_file = cfg.get("key_file") or DEFAULT_KEY
        self.pubkey_file = cfg.get("pubkey_file") or DEFAULT_PUB
        self.fail_closed = bool(cfg.get("fail_closed", True))
        self.writer_name = writer
        self.log = logger.getChild("ledger")
        self.writer: Optional[LedgerWriter] = None
        self.pubkey: Optional[str] = None
        self.available = False
        self.reason: Optional[str] = None
        self.entries_this_session = 0
        self._lock = threading.RLock()
        self._warned = False
        self.anchor = LedgerAnchor(cfg.get("anchor") or {}, self.log, self.path, "", writer,
                                   client=anchor_client, region=region, instance_id=instance_id)
        if self.enabled:
            self.open()

    # ---- lifecycle ----------------------------------------------------------------

    def open(self):
        with self._lock:
            try:
                key = self._load_or_create_key()
                self.pubkey = public_key_hex(key)
                self.anchor.pubkey = self.pubkey
                self.writer = LedgerWriter(self.path, key, writer=self.writer_name)
                self.available = True
                self.reason = None
                self.log.info("Glass Ledger open at %s (head seq %d, pubkey %s)",
                              self.path, self.writer.seq, self.pubkey)
            except TornTail as e:
                self._fail(str(e))
            except LedgerLocked as e:
                self._fail(str(e))
            except (LedgerError, OSError, ValueError) as e:
                self._fail(f"cannot open ledger: {e}")

    def _fail(self, reason: str):
        self.available = False
        self.reason = reason
        self.writer = None
        self.log.error("Glass Ledger unavailable: %s%s", reason,
                       " (fail-closed: the agent will not act)" if self.fail_closed else "")

    def _load_or_create_key(self):
        if os.path.exists(self.key_file):
            with open(self.key_file, "r", encoding="utf-8") as fh:
                return load_private_key(fh.read())
        directory = os.path.dirname(self.key_file)
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        seed, pub = generate_key()
        fd = os.open(self.key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(seed + "\n")
        try:
            pub_dir = os.path.dirname(self.pubkey_file)
            if pub_dir:
                os.makedirs(pub_dir, exist_ok=True)
            with open(self.pubkey_file, "w", encoding="utf-8") as fh:
                fh.write(pub + "\n")
        except OSError as e:
            self.log.warning("could not write public key file %s: %s", self.pubkey_file, e)
        self.log.info("Generated Glass Ledger signing key %s; public key %s", self.key_file, pub)
        return load_private_key(seed)

    def close(self):
        with self._lock:
            if self.anchor.enabled:
                self.anchor.flush(force=True)
            if self.writer is not None:
                self.writer.close()
                self.writer = None
            self.available = False

    # ---- recording ----------------------------------------------------------------

    def record(self, kind: str, body: dict) -> bool:
        """Append one entry. False (and fail-closed) when it could not be written."""
        if not self.enabled:
            return True  # nothing to record into; the agent is not gated
        with self._lock:
            if self.writer is None:
                return False
            try:
                entry = self.writer.append(kind, body)
            except (LedgerError, OSError, ValueError, TypeError) as e:
                self._fail(f"append failed: {e}")
                return False
            self.entries_this_session += 1
            self.anchor.notify(self.writer.head)
            return True

    def gate(self) -> Optional[str]:
        """Reason the agent must not act right now, or None."""
        if not self.enabled or not self.fail_closed or self.available:
            return None
        return f"Glass Ledger unavailable (fail-closed): {self.reason}"

    def tick(self, force: bool = False):
        """Give the anchor a chance to upload; called from the run loop."""
        if self.anchor.enabled and (force or self.anchor.due()):
            self.anchor.flush(force=force)

    # ---- operator views ----------------------------------------------------------

    @property
    def head(self) -> Optional[dict]:
        return self.writer.head if self.writer is not None else None

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "available": self.available,
            "fail_closed": self.fail_closed,
            "reason": self.reason,
            "path": self.path,
            "pubkey": self.pubkey,
            "head": self.head,
            "entries_this_session": self.entries_this_session,
            "anchor": self.anchor.status(),
        }

    def tail(self, limit: int = 20) -> list:
        if not os.path.exists(self.path):
            return []
        with open(self.path, "rb") as fh:
            lines = [ln for ln in fh.read().split(b"\n") if ln][-max(1, limit):]
        out = []
        for raw in lines:
            try:
                out.append(parse_line(raw))
            except ValueError as e:
                out.append({"error": f"unparseable line ({e})"})
        return out

    def verify(self) -> dict:
        """On-box convenience check. The audit that counts runs off-box."""
        return verify_file(self.path, pubkey=self.pubkey).to_dict()
