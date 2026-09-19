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

# Added after the fact, so they are applied as migrations rather than baked
# into SCHEMA: an existing box must keep the memories it already has.
#
#   weight     how much this matters. Strengthens when it proves useful,
#              decays when it does not. Ordering and pruning read it.
#   used       times it was actually retrieved into the model's context, as
#              opposed to `seen`, which counts times it was written again.
#   used_at    when that last happened, so decay has something to measure.
#   state      live | dormant | superseded. Nothing is ever deleted by
#              consolidation; it stops being current.
#   derived    0 for an observation, 1 for something consolidation wrote.
#              A derived entry is never consolidated again: summarising
#              summaries is how a memory becomes a confident fiction.
#   sources    the ids a derived entry was made from, comma separated, so
#              provenance survives the merge.
MIGRATIONS = (
    ("weight", "ALTER TABLE memories ADD COLUMN weight REAL NOT NULL DEFAULT 1.0"),
    ("used", "ALTER TABLE memories ADD COLUMN used INTEGER NOT NULL DEFAULT 0"),
    ("used_at", "ALTER TABLE memories ADD COLUMN used_at REAL"),
    ("state", "ALTER TABLE memories ADD COLUMN state TEXT NOT NULL DEFAULT 'live'"),
    ("derived", "ALTER TABLE memories ADD COLUMN derived INTEGER NOT NULL DEFAULT 0"),
    ("sources", "ALTER TABLE memories ADD COLUMN sources TEXT"),
)

STATES = ("live", "dormant", "superseded")

KINDS = ("note", "fact", "upload", "goal", "operator", "proposal")
SOURCES = ("brain", "operator", "system")

DEFAULT_PATH = "/var/lib/jarvis/memory.db"
DEFAULT_MAX_ROWS = 2000
TEXT_LIMIT = 2000
WEIGHT_MIN = 0.05
WEIGHT_MAX = 5.0


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

    # The consolidation surface, so a box with no durable memory takes the
    # same code path and simply has nothing to consolidate.
    def reinforce(self, memory_ids, amount: float = 0.25) -> int:
        return 0

    def decay(self, factor: float = 0.9, older_than_s: float = 86400,
              floor: float = 0.05) -> int:
        return 0

    def set_state(self, memory_id: int, state: str) -> bool:
        return False

    def remember_derived(self, text, sources, kind="fact", weight=2.0, pinned=False):
        return None

    def live(self, limit: int = 200, kind: Optional[str] = None,
             include_derived: bool = True) -> list:
        return []

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
            self._migrate(db)
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

    @staticmethod
    def _migrate(db):
        """Add the consolidation columns to a database that predates them.

        A box that has been running for weeks has memories worth keeping, so
        the columns arrive as ALTERs rather than a rebuild. Each one is
        independent: a half-applied migration leaves a usable store.
        """
        have = {r["name"] for r in db.execute("PRAGMA table_info(memories)")}
        for column, statement in MIGRATIONS:
            if column not in have:
                db.execute(statement)
        db.execute("CREATE INDEX IF NOT EXISTS memories_state ON memories(state)")
        db.execute("CREATE INDEX IF NOT EXISTS memories_weight ON memories(weight)")

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

    # ---- consolidation ---------------------------------------------------

    def reinforce(self, memory_ids, amount: float = 0.25) -> int:
        """Strengthen memories that were actually used.

        `seen` counts times the agent wrote the same thing again, which says
        the agent is repetitive. This counts times a memory was pulled into
        the model's context, which says the memory was worth keeping. They
        are different claims and the second is the one worth weighting on.
        """
        ids = [int(i) for i in (memory_ids or [])]
        if self._db is None or not ids:
            return 0
        now = time.time()
        marks = ",".join("?" * len(ids))
        try:
            with self._lock:
                cur = self._db.execute(
                    f"UPDATE memories SET weight = MIN(weight + ?, {WEIGHT_MAX}),"
                    f" used = used + 1, used_at = ? WHERE id IN ({marks})",
                    (float(amount), now, *ids))
                self._db.commit()
                return cur.rowcount
        except Exception as exc:
            self.log.warning("Could not reinforce memories: %s", exc)
            return 0

    def decay(self, factor: float = 0.9, older_than_s: float = 86400,
              floor: float = WEIGHT_MIN) -> int:
        """Fade what has not been used lately. Pinned entries do not fade.

        Decay is what makes weight mean anything: without it every memory
        drifts upward and the ordering stops discriminating.
        """
        if self._db is None:
            return 0
        cutoff = time.time() - max(0.0, float(older_than_s))
        try:
            with self._lock:
                cur = self._db.execute(
                    "UPDATE memories SET weight = MAX(weight * ?, ?)"
                    " WHERE pinned = 0 AND state = 'live'"
                    "   AND COALESCE(used_at, ts) < ?",
                    (float(factor), float(floor), cutoff))
                self._db.commit()
                return cur.rowcount
        except Exception as exc:
            self.log.warning("Could not decay memories: %s", exc)
            return 0

    def set_state(self, memory_id: int, state: str) -> bool:
        """Retire a memory without destroying it.

        Consolidation never deletes. A merged observation becomes dormant and
        a contradicted one becomes superseded, both still on disk with their
        provenance, because a model that can quietly erase its own past is
        the failure the ledger exists to prevent -- and this is the one store
        the ledger does not cover.
        """
        if self._db is None or state not in STATES:
            return False
        try:
            with self._lock:
                cur = self._db.execute("UPDATE memories SET state = ? WHERE id = ?",
                                       (state, int(memory_id)))
                self._db.commit()
                return cur.rowcount > 0
        except Exception as exc:
            self.log.warning("Could not set memory state: %s", exc)
            return False

    def remember_derived(self, text: str, sources, kind: str = "fact",
                         weight: float = 2.0, pinned: bool = False) -> Optional[dict]:
        """Write what consolidation concluded, carrying its sources with it.

        Marked derived so it can never be consolidated again: summarising a
        summary, and then that summary, is how a memory becomes a confident
        fiction with no provenance left. Depth is capped at one, forever.
        """
        ids = [int(i) for i in (sources or [])]
        entry = self.remember(text, kind=kind, source="system", pinned=pinned)
        if not entry or self._db is None:
            return entry
        try:
            with self._lock:
                self._db.execute(
                    "UPDATE memories SET derived = 1, sources = ?, weight = ?"
                    " WHERE id = ?",
                    (",".join(str(i) for i in ids), float(weight), entry["id"]))
                self._db.commit()
        except Exception as exc:
            self.log.warning("Could not mark derived memory: %s", exc)
        entry["derived"] = True
        entry["sources"] = ids
        return entry

    def live(self, limit: int = 200, kind: Optional[str] = None,
             include_derived: bool = True) -> list:
        """Current memories, strongest first. What consolidation works on."""
        sql = ("SELECT * FROM memories WHERE state = 'live'"
               + ("" if include_derived else " AND derived = 0")
               + ("" if kind is None else " AND kind = ?")
               + " ORDER BY pinned DESC, weight DESC, ts DESC LIMIT ?")
        args = ((kind, int(limit)) if kind is not None else (int(limit),))
        return self._query(sql, args)

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
                # Weakest first, oldest to break ties. Age alone would drop a
                # hard-won fact from week one to make room for this morning's
                # sixth "disk is fine", which is exactly backwards. Anything
                # consolidation derived is protected with the pinned entries:
                # it is the distilled form of memories already let go.
                cur = self._db.execute(
                    "DELETE FROM memories WHERE id IN ("
                    "  SELECT id FROM memories WHERE pinned = 0 AND derived = 0"
                    "  ORDER BY weight ASC, ts ASC LIMIT ?)", (total - cap,))
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
        keys = row.keys()
        out = {"id": row["id"], "ts": row["ts"], "kind": row["kind"],
               "source": row["source"], "cycle": row["cycle"], "text": row["text"],
               "pinned": bool(row["pinned"]), "seen": row["seen"]}
        for extra, default in (("weight", 1.0), ("used", 0), ("used_at", None),
                               ("state", "live"), ("derived", 0), ("sources", None)):
            if extra in keys:
                out[extra] = row[extra]
        out["derived"] = bool(out.get("derived"))
        if out.get("sources"):
            out["sources"] = [int(x) for x in str(out["sources"]).split(",") if x.strip()]
        return out

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
