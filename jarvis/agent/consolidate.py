"""Consolidation: the agent deciding what its own memory should look like.

The store began as a flat list. Everything arrived with the same weight, was
kept until a row cap pushed the oldest out, and the model was handed the lot
and expected to sort it out on every cycle. The operator's diagnosis was
exact: we throw everything at it and expect it to organise it in flight.

The metaphor he reached for was defragmenting a disk. The nearer one is what
sleep does, and the difference matters. Defragmenting rearranges blocks and
changes nothing about what they mean. Consolidation decides what survives, at
what strength, merged with what -- it is an editorial act, and an editorial act
performed by the thing being edited needs rules it cannot talk its way around.

So there are two, and they do not bend:

**Nothing is destroyed.** A merge writes a NEW entry citing the ids it came
from; the sources go dormant, still on disk, still readable, still carrying
their provenance. A contradiction supersedes rather than overwrites. If the
model could quietly erase its own past it would be doing exactly what the Glass
Ledger exists to prevent, inside the one store the ledger does not cover.

**A derived memory is never consolidated again.** Summarising a summary, and
then summarising that, is how a memory becomes a confident fiction with no
provenance left -- the model's own drift, compounding, with nothing to check it
against. Depth is capped at one. Consolidation only ever reads originals.

What a pass does, in order:

  decay      weaken what has not been used; pinned entries do not fade
  supersede  where two live memories make the same measurement with different
             values, the newer is current and the older becomes history
  merge      where several observations say the same thing, write one that
             says it better and cites them
  promote    what has been confirmed often enough becomes standing fact

The model proposes; this module decides. It is handed candidate groupings and
returns what it actually did, so a planner that hallucinates a merge of two
unrelated memories produces a rejected proposal rather than a corrupted store.
"""

import logging
import re
import time
from typing import Optional

# A measurement the agent makes repeatedly: "<subject> is <value><unit>".
# Two live memories matching the same subject with different values are a
# contradiction in time, not a disagreement, and the newer one wins.
# The word boundary has to sit inside the alternation rather than after it:
# "%" is not a word character, so "%\b" only matches when a letter follows,
# and "disk usage at 84%" parsed as unitless. That made a percentage and a
# bare count the same measurement, which is how "84%" and "84 GB" would have
# ended up superseding each other.
MEASUREMENT = re.compile(
    r"^(?P<subject>.{3,80}?)\s+(?:is|was|at)\s+(?P<value>-?\d+(?:\.\d+)?)\s*"
    r"(?P<unit>%|(?:percent|gb|mb|kb|gib|mib|kib|°c|c|ms|s)\b)?",
    re.IGNORECASE)

DEFAULT_MIN_GROUP = 3        # observations before a merge is worth writing
DEFAULT_PROMOTE_AT = 5       # confirmations before something becomes standing
# ...and they have to be spread out. Run against the live agent's real memory,
# promotion by count alone wanted to make standing facts of "no new security
# findings to act on; idling until the next cycle" -- a status line the planner
# had written six times in one session. A count says a thing was repeated. A
# span says it kept being true, which is the claim "standing fact" makes.
DEFAULT_PROMOTE_SPAN_S = 6 * 3600
DEFAULT_DECAY = 0.9
DEFAULT_DECAY_AFTER_S = 86400
DEFAULT_MAX_MERGES = 8       # per pass; consolidation is not the agent's job
STOPWORDS = frozenset("""
a an and are as at be been but by for from has have in is it its of on or that
the this to was were will with not no yes see saw its it's there their
""".split())


def normalise(text: str) -> str:
    return " ".join((text or "").lower().split())


def keywords(text: str) -> frozenset:
    words = re.findall(r"[a-z0-9/._-]{3,}", normalise(text))
    return frozenset(w for w in words if w not in STOPWORDS)


def similarity(a: str, b: str) -> float:
    """Jaccard over content words. Crude on purpose.

    Something cleverer would be a model call per pair, which is a cost per
    memory per pass, and a place for the model's judgement to enter where its
    judgement is exactly what we are trying to constrain.
    """
    ka, kb = keywords(a), keywords(b)
    if not ka or not kb:
        return 0.0
    return len(ka & kb) / len(ka | kb)


def measurement_of(text: str):
    """(subject, value, unit) when the text states a measurement."""
    m = MEASUREMENT.match((text or "").strip())
    if not m:
        return None
    subject = normalise(m.group("subject"))
    subject = " ".join(w for w in subject.split() if w not in STOPWORDS)
    if not subject:
        return None
    return subject, float(m.group("value")), (m.group("unit") or "").lower()


class Consolidator:
    """One pass over the agent's memory. Reports what it did; never raises."""

    def __init__(self, store, logger: Optional[logging.Logger] = None,
                 config: Optional[dict] = None, ledger=None, clock=time.time):
        cfg = dict(config or {})
        self.store = store
        self.log = logger or logging.getLogger("jarvis.consolidate")
        self.ledger = ledger
        self.clock = clock
        self.enabled = bool(cfg.get("enabled", True))
        self.min_group = max(2, int(cfg.get("min_group", DEFAULT_MIN_GROUP)))
        self.promote_at = max(2, int(cfg.get("promote_at", DEFAULT_PROMOTE_AT)))
        self.promote_span_s = float(cfg.get("promote_span_s", DEFAULT_PROMOTE_SPAN_S))
        self.similarity_threshold = float(cfg.get("similarity", 0.6))
        self.decay_factor = float(cfg.get("decay", DEFAULT_DECAY))
        self.decay_after_s = float(cfg.get("decay_after_s", DEFAULT_DECAY_AFTER_S))
        self.max_merges = max(1, int(cfg.get("max_merges", DEFAULT_MAX_MERGES)))
        self.scan_limit = max(20, int(cfg.get("scan_limit", 400)))

    # ---- the pass -------------------------------------------------------

    def run(self, dry_run: bool = False) -> dict:
        """Decay, supersede, merge, promote. Returns what changed."""
        report = {"ran_at": self.clock(), "dry_run": bool(dry_run),
                  "decayed": 0, "superseded": [], "merged": [], "promoted": [],
                  "scanned": 0, "skipped": None}
        if not self.enabled:
            report["skipped"] = "consolidation disabled"
            return report
        if not getattr(self.store, "available", False):
            report["skipped"] = "no durable memory"
            return report
        try:
            # Originals only: a derived entry is never an input.
            rows = self.store.live(limit=self.scan_limit, include_derived=False)
            report["scanned"] = len(rows)
            if not dry_run:
                report["decayed"] = self.store.decay(self.decay_factor,
                                                     self.decay_after_s)
            report["superseded"] = self._supersede(rows, dry_run)
            done = {i for s in report["superseded"] for i in s["retired"]}
            report["merged"] = self._merge([r for r in rows if r["id"] not in done],
                                           dry_run)
            report["promoted"] = self._promote(rows, dry_run)
        except Exception as exc:                       # never fatal
            self.log.warning("Consolidation pass failed: %s", exc)
            report["skipped"] = f"{type(exc).__name__}: {exc}"
            return report
        self._record(report)
        return report

    # ---- contradictions -------------------------------------------------

    def _supersede(self, rows, dry_run: bool) -> list:
        """Two measurements of the same thing cannot both be current.

        "the root filesystem is 8GB" and "the root filesystem is 20GB" are not
        a disagreement to resolve by weight; they are the same fact at two
        times. The newer is true now. The older is history, and history is
        kept, because how a thing changed is often the useful part.
        """
        by_subject: dict = {}
        for row in rows:
            if row.get("pinned"):
                continue
            parsed = measurement_of(row["text"])
            if parsed:
                subject, value, unit = parsed
                by_subject.setdefault((subject, unit), []).append((row, value))
        out = []
        for (subject, unit), entries in by_subject.items():
            if len(entries) < 2:
                continue
            values = {v for _, v in entries}
            if len(values) < 2:
                continue                              # agreeing repeats: a merge, not this
            entries.sort(key=lambda pair: pair[0]["ts"])
            *older, (newest, newest_value) = entries
            retired = [row["id"] for row, _ in older]
            if not dry_run:
                for memory_id in retired:
                    self.store.set_state(memory_id, "superseded")
                self.store.reinforce([newest["id"]], amount=0.5)
            out.append({"subject": subject, "unit": unit or None,
                        "current": newest["id"], "current_value": newest_value,
                        "retired": retired,
                        "was": sorted({v for _, v in older})})
        return out

    # ---- merging --------------------------------------------------------

    def _merge(self, rows, dry_run: bool) -> list:
        """Several observations of one thing become one that says it better.

        The merged entry cites its sources and the sources go dormant. The
        agent keeps what it learned and stops being handed the same sentence
        five times in its own context window.
        """
        groups, used = [], set()
        candidates = [r for r in rows if not r.get("pinned")]
        for i, row in enumerate(candidates):
            if row["id"] in used:
                continue
            group = [row]
            for other in candidates[i + 1:]:
                if other["id"] in used:
                    continue
                if similarity(row["text"], other["text"]) >= self.similarity_threshold:
                    group.append(other)
            if len(group) >= self.min_group:
                groups.append(group)
                used.update(g["id"] for g in group)
            if len(groups) >= self.max_merges:
                break

        out = []
        for group in groups:
            group.sort(key=lambda r: r["ts"])
            text = self.summarise(group)
            sources = [r["id"] for r in group]
            weight = min(5.0, 1.0 + 0.4 * len(group))
            if not dry_run:
                entry = self.store.remember_derived(text, sources, kind="fact",
                                                    weight=weight)
                if entry is None:
                    continue
                for memory_id in sources:
                    self.store.set_state(memory_id, "dormant")
                merged_id = entry["id"]
            else:
                merged_id = None
            out.append({"id": merged_id, "text": text, "sources": sources,
                        "weight": round(weight, 2)})
        return out

    @staticmethod
    def summarise(group) -> str:
        """One sentence for a group, built from the group, not invented.

        Deliberately mechanical. Asking the model to phrase it would be a
        nicer sentence and an opening for drift: the merged entry would stop
        being a claim the sources support and start being a claim the model
        made while holding them.
        """
        longest = max(group, key=lambda r: len(r["text"]))["text"].strip()
        times = len(group)
        first = min(r["ts"] for r in group)
        last = max(r["ts"] for r in group)
        span_h = max(0.0, (last - first) / 3600.0)
        when = (f"over {span_h:.0f}h" if span_h >= 1 else "within the hour")
        return f"{longest} (observed {times}x {when})"

    # ---- promotion ------------------------------------------------------

    def _promote(self, rows, dry_run: bool) -> list:
        """What has been confirmed enough becomes standing fact.

        Pinning was already the operator doing this by hand. This is the
        agent noticing that it keeps re-establishing the same thing and
        deciding to stop paying for it.
        """
        out = []
        for row in rows:
            if row.get("pinned") or row.get("derived"):
                continue
            if int(row.get("seen") or 0) + int(row.get("used") or 0) < self.promote_at:
                continue
            # Repeated is not the same as durable. Something first seen and
            # last seen within the same session is a status line, however
            # many times it was written.
            first = row.get("first_ts") or row.get("ts")
            if first and (row["ts"] - float(first)) < self.promote_span_s:
                continue
            if not dry_run:
                self.store.reinforce([row["id"]], amount=1.0)
                try:
                    with self.store._lock:                  # noqa: SLF001
                        self.store._db.execute(             # noqa: SLF001
                            "UPDATE memories SET pinned = 1 WHERE id = ?", (row["id"],))
                        self.store._db.commit()             # noqa: SLF001
                except Exception as exc:
                    self.log.warning("Could not promote memory %s: %s", row["id"], exc)
                    continue
            out.append({"id": row["id"], "text": row["text"][:120],
                        "seen": row.get("seen"), "used": row.get("used")})
        return out

    # ---- the record -----------------------------------------------------

    def _record(self, report: dict):
        """How the memory got its shape is itself worth being able to audit."""
        if self.ledger is None or report.get("dry_run"):
            return
        if not (report["merged"] or report["superseded"] or report["promoted"]):
            return                                   # a quiet pass is not an event
        try:
            self.ledger.record("consolidation", {
                "scanned": report["scanned"], "decayed": report["decayed"],
                "merged": [{"id": m["id"], "sources": m["sources"]}
                           for m in report["merged"]],
                "superseded": [{"subject": s["subject"], "current": s["current"],
                                "retired": s["retired"]} for s in report["superseded"]],
                "promoted": [p["id"] for p in report["promoted"]],
            })
        except Exception as exc:
            self.log.warning("Could not record consolidation: %s", exc)


def build_consolidator(store, config: Optional[dict] = None,
                       logger: Optional[logging.Logger] = None, ledger=None):
    memory_cfg = (config or {}).get("memory") or {}
    return Consolidator(store, logger, memory_cfg.get("consolidate"), ledger)
