"""Durable operational memory: what the agent learned, kept across restarts.

The agent's working memory is bounded and evictable, and its notes were a
twenty-entry deque. Both lived only in the process, so a restart, a crash or
a deploy left the agent knowing nothing it had worked out. The one durable
record was the Glass Ledger, which is evidence *about* the agent and which
the planner is forbidden to read. The agent was therefore amnesiac by
design, and every cycle it re-learned the same facts about its own machine.

This is the agent's own store, and deliberately only that. It holds what the
agent learned about the machine it runs on. Who the operator is, who he
works with and what he is dealing with belong to a separate store with
different sensitivity, different retention and different rules about who may
write to it; nothing here reaches for that, and nothing here should.

Three properties matter:

* **Provenance.** Every entry records where it came from. A memory whose
  origin is unknown is a rumour, and an agent that cannot tell its own
  inference from its operator's instruction will eventually act on the
  wrong one.
* **Idempotence.** Remembering the same thing twice touches one row. A
  planner that repeats itself should not be able to crowd out its own
  older and better memories.
* **Never fatal.** A broken or unwritable database degrades to memory-only
  and logs it. Losing the loop because the disk filled would be a worse
  failure than forgetting.
"""

import hashlib
import logging
import os
import sqlite3
import threading
import time
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL    NOT NULL,
    kind    TEXT    NOT NULL,
    source  TEXT    NOT NULL,
    cycle   INTEGER,
    text    TEXT    NOT NULL,
    digest  TEXT    NOT NULL UNIQUE,
    pinned  INTEGER NOT NULL DEFAULT 0,
    seen    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS memories_ts ON memories(ts);
CREATE INDEX IF NOT EXISTS memories_kind ON memories(kind);
"""

KINDS = ("note", "fact", "upload", "goal", "operator", "proposal")
SOURCES = ("brain", "operator", "system")

DEFAULT_PATH = "/var/lib/jarvis/memory.db"
DEFAULT_MAX_ROWS = 2000
TEXT_LIMIT = 2000


def _digest(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8", "replace")).hexdigest()


class NullStore:
    """Stands in when no durable store is configured: remembers nothing."""

    enabled = False
    available = False
    path = None

    def remember(self, text, kind="note", source="brain", cycle=None, pinned=False):
        return None

    def recent(self, limit: int = 20, kind: Optional[str] = None) -> list:
        return []

    def search(self, query: str, limit: int = 20) -> list:
        return []

    def pinned(self, limit: int = 10) -> list:
        return []

    def forget(self, memory_id: int) -> bool:
        return False

    def prune(self, max_rows: Optional[int] = None) -> int:
        return 0

    def stats(self) -> dict:
        return {"enabled": False, "available": False}

    def close(self):
        pass


class MemoryStore:
    """SQLite-backed operational memory, local to the machine."""

    def __init__(self, path: str = DEFAULT_PATH, max_rows: int = DEFAULT_MAX_ROWS,
                 logger: Optional[logging.Logger] = None):
        self.path = path
        self.max_rows = max(10, int(max_rows))
        self.log = (logger or logging.getLogger("jarvis")).getChild("store")
        self.enabled = True
        self.available = False
        self.reason: Optional[str] = None
        self._lock = threading.Lock()
        self._db: Optional[sqlite3.Connection] = None
        self._open()

    # ---- lifecycle ------------------------------------------------------

    def _open(self):
        try:
            parent = os.path.dirname(self.path) or "."
            os.makedirs(parent, exist_ok=True)
            db = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript(SCHEMA)
            db.commit()
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            self._db = db
            self.available = True
            self.reason = None
        except Exception as exc:                       # never fatal
            self._db = None
            self.available = False
            self.reason = f"{exc.__class__.__name__}: {exc}"
            self.log.error("Memory store unavailable (%s); running without durable memory",
                           self.reason)

    def close(self):
        with self._lock:
            if self._db is not None:
                try:
                    self._db.close()
                finally:
                    self._db = None
                    self.available = False

    # ---- writing --------------------------------------------------------

    def remember(self, text: str, kind: str = "note", source: str = "brain",
                 cycle: Optional[int] = None, pinned: bool = False) -> Optional[dict]:
        """Store one memory. Remembering the same text again refreshes it."""
        text = (text or "").strip()[:TEXT_LIMIT]
        if not text or self._db is None:
            return None
        kind = kind if kind in KINDS else "note"
        source = source if source in SOURCES else "brain"
        digest, now = _digest(text), time.time()
        try:
            with self._lock:
                row = self._db.execute("SELECT id, seen FROM memories WHERE digest = ?",
                                       (digest,)).fetchone()
                if row is not None:
                    # Seen before: one row, refreshed, not a duplicate.
                    self._db.execute(
                        "UPDATE memories SET ts = ?, seen = seen + 1, cycle = ?,"
                        " pinned = MAX(pinned, ?) WHERE id = ?",
                        (now, cycle, int(bool(pinned)), row["id"]))
                    self._db.commit()
                    return {"id": row["id"], "text": text, "kind": kind,
                            "repeat": True, "seen": row["seen"] + 1}
                cur = self._db.execute(
                    "INSERT INTO memories (ts, kind, source, cycle, text, digest, pinned)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (now, kind, source, cycle, text, digest, int(bool(pinned))))
                self._db.commit()
                new_id = cur.lastrowid
        except Exception as exc:
            self.log.warning("Could not store memory: %s", exc)
            return None
        self.prune()
        return {"id": new_id, "text": text, "kind": kind, "repeat": False, "seen": 1}

    def forget(self, memory_id: int) -> bool:
        if self._db is None:
            return False
        try:
            with self._lock:
                cur = self._db.execute("DELETE FROM memories WHERE id = ?", (int(memory_id),))
                self._db.commit()
                return cur.rowcount > 0
        except Exception as exc:
            self.log.warning("Could not forget memory %s: %s", memory_id, exc)
            return False

    def prune(self, max_rows: Optional[int] = None) -> int:
        """Drop the oldest unpinned memories past the cap. Pinned ones stay."""
        if self._db is None:
            return 0
        cap = max(10, int(max_rows or self.max_rows))
        try:
            with self._lock:
                total = self._db.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]
                if total <= cap:
                    return 0
                cur = self._db.execute(
                    "DELETE FROM memories WHERE id IN ("
                    "  SELECT id FROM memories WHERE pinned = 0"
                    "  ORDER BY ts ASC LIMIT ?)", (total - cap,))
                self._db.commit()
                dropped = cur.rowcount
        except Exception as exc:
            self.log.warning("Could not prune memories: %s", exc)
            return 0
        if dropped:
            self.log.info("Pruned %d memory entries (cap %d)", dropped, cap)
        return dropped

    # ---- reading --------------------------------------------------------

    @staticmethod
    def _row(row) -> dict:
        return {"id": row["id"], "ts": row["ts"], "kind": row["kind"],
                "source": row["source"], "cycle": row["cycle"], "text": row["text"],
                "pinned": bool(row["pinned"]), "seen": row["seen"]}

    def _query(self, sql: str, args: tuple) -> list:
        if self._db is None:
            return []
        try:
            with self._lock:
                return [self._row(r) for r in self._db.execute(sql, args).fetchall()]
        except Exception as exc:
            self.log.warning("Could not read memories: %s", exc)
            return []

    def recent(self, limit: int = 20, kind: Optional[str] = None) -> list:
        limit = max(1, min(int(limit), 200))
        if kind:
            return self._query("SELECT * FROM memories WHERE kind = ? ORDER BY ts DESC LIMIT ?",
                               (kind, limit))
        return self._query("SELECT * FROM memories ORDER BY ts DESC LIMIT ?", (limit,))

    def pinned(self, limit: int = 10) -> list:
        return self._query("SELECT * FROM memories WHERE pinned = 1 ORDER BY ts DESC LIMIT ?",
                           (max(1, min(int(limit), 100)),))

    def search(self, query: str, limit: int = 20) -> list:
        """Every term must appear. Plain LIKE: the store is small by design."""
        terms = [t for t in str(query or "").split() if t][:6]
        if not terms:
            return []
        where = " AND ".join(["text LIKE ?"] * len(terms))
        args = tuple(f"%{t}%" for t in terms) + (max(1, min(int(limit), 100)),)
        return self._query(f"SELECT * FROM memories WHERE {where} ORDER BY ts DESC LIMIT ?", args)

    def stats(self) -> dict:
        out = {"enabled": self.enabled, "available": self.available, "path": self.path,
               "max_rows": self.max_rows}
        if self.reason:
            out["reason"] = self.reason
        if self._db is None:
            return out
        try:
            with self._lock:
                out["entries"] = self._db.execute(
                    "SELECT COUNT(*) AS n FROM memories").fetchone()["n"]
                out["pinned"] = self._db.execute(
                    "SELECT COUNT(*) AS n FROM memories WHERE pinned = 1").fetchone()["n"]
                oldest = self._db.execute("SELECT MIN(ts) AS t FROM memories").fetchone()["t"]
                if oldest:
                    out["oldest_age_s"] = round(time.time() - oldest)
        except Exception as exc:
            out["error"] = str(exc)
        return out


def build_store(config: Optional[dict], logger: Optional[logging.Logger] = None):
    """Make the store from config; NullStore when disabled or unbuildable."""
    cfg = config or {}
    if not cfg.get("enabled", True):
        return NullStore()
    try:
        return MemoryStore(path=cfg.get("path") or DEFAULT_PATH,
                           max_rows=int(cfg.get("max_rows") or DEFAULT_MAX_ROWS),
                           logger=logger)
    except Exception:
        return NullStore()
