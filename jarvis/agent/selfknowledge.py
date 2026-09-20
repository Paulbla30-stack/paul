"""What the agent can be told about itself, derived from evidence it may not read.

The Glass Ledger holds every decision, action, outcome and refusal the agent
has ever produced, signed and hash-chained. The agent is forbidden from reading
it, and that is right: a record the subject can consult is a record the subject
can manage, and the prompt says so explicitly so the model does not try. But it
leaves a hole. The agent has a perfect account of its own conduct and cannot
learn one thing from it. It will repeat a refused approach, run a check that has
never once found anything, and propose the same change a sixth time, because
nothing carries yesterday forward except the notes it happened to write.

The operator's instinct was to give it access to its own weights. That is the
wrong substrate -- reading your parameters is not introspection, and no human
does it either. What people actually have is behavioural self-knowledge: not
"here are my synapses" but "I keep getting this wrong". That is derivable here.

So: statistics computed over the ledger and handed to the model as fact. The
derivation is one-way and the numbers are aggregates. The model learns that it
proposed six changes and one was taken; it does not learn which, or when, or
what the operator said, and it cannot alter the record or the reading of it.
The evidence stays sealed. What crosses is a mirror, not a door.

Deliberately not included: anything that would let the model reconstruct a
particular entry, and any operator content. The point is for it to know its own
shape, not to read its file.
"""

import json
import logging
import os
import time
from collections import Counter
from typing import Optional

DEFAULT_WINDOW_S = 7 * 86400
DEFAULT_MAX_LINES = 20000
TOP_N = 5


class SelfKnowledge:
    """Reads the ledger file and returns aggregates. Never returns entries."""

    def __init__(self, path: str, logger: Optional[logging.Logger] = None,
                 window_s: float = DEFAULT_WINDOW_S,
                 max_lines: int = DEFAULT_MAX_LINES, clock=time.time):
        self.path = path
        self.log = logger or logging.getLogger("jarvis.selfknowledge")
        self.window_s = float(window_s)
        self.max_lines = int(max_lines)
        self.clock = clock
        self._cache: Optional[dict] = None
        self._cached_at = 0.0
        self.cache_ttl = 300.0

    # ---- reading --------------------------------------------------------

    def _entries(self):
        """Yield (kind, ts, body) inside the window. Bounded and forgiving."""
        if not self.path or not os.path.exists(self.path):
            return
        cutoff = self.clock() - self.window_s
        try:
            with open(self.path, "rb") as fh:
                lines = fh.read().split(b"\n")
        except OSError as exc:
            self.log.debug("Could not read the ledger for self-knowledge: %s", exc)
            return
        for raw in lines[-self.max_lines:]:
            if not raw.strip():
                continue
            try:
                entry = json.loads(raw.decode("utf-8", "replace"))
                ts = _to_epoch(entry.get("ts"))
                if ts is None or ts < cutoff:
                    continue
                yield str(entry.get("kind") or ""), ts, (entry.get("body") or {})
            except Exception:
                continue          # a torn or odd line is not worth an exception here

    # ---- the aggregates -------------------------------------------------

    def compute(self) -> dict:
        """One pass, producing only counts and rates."""
        decisions = outcomes = failures = 0
        proposals = 0
        # The entry stream is consumed once; the fault detectors need the same
        # pass, so they ride along rather than re-reading the file.
        faults_seen: list = []
        gates: Counter = Counter()
        notifications = {"sent": 0, "held": 0}
        by_task: dict = {}
        consolidations = 0
        verdicts: list = []
        first_ts = last_ts = None

        for kind, ts, body in self._entries():
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)
            faults_seen.append((kind, ts, body))
            if kind == "decision":
                decisions += 1
            elif kind == "outcome":
                outcomes += 1
                task = str(body.get("type") or "unknown")
                stat = by_task.setdefault(task, {"runs": 0, "failed": 0, "found": 0})
                stat["runs"] += 1
                if not body.get("success"):
                    stat["failed"] += 1
                    failures += 1
                # "found something" is an output bigger than an empty result;
                # a check that always returns the same tiny nothing is the
                # one worth noticing.
                size = ((body.get("output") or {}).get("bytes") or 0)
                if size > 64:
                    stat["found"] += 1
            elif kind == "gate":
                gates[str(body.get("gate") or "unknown")] += 1
                if str(body.get("gate")) == "authority":
                    proposals += 1
            elif kind == "notification":
                notifications["sent" if body.get("sent") else "held"] += 1
            elif kind == "consolidation":
                consolidations += 1
            elif kind == "action" and body.get("action") == "decide_proposal":
                # The operator's verdicts are the one thing here that is
                # instruction rather than evidence, so they are the one thing
                # carried across with their text. Counts teach nothing: "one
                # of six accepted" tells the agent it has a poor hit rate and
                # not one thing about what a good proposal looks like.
                verdicts.append({
                    "verdict": str(body.get("verdict") or ""),
                    "proposal": str(body.get("proposal") or "")[:200],
                    "reason": str(body.get("reason") or "")[:200] or None,
                    "ts": ts})

        return {
            "window_days": round(self.window_s / 86400, 1),
            "span_hours": (round((last_ts - first_ts) / 3600, 1)
                           if first_ts and last_ts else 0.0),
            "decisions": decisions,
            "outcomes": outcomes,
            "failures": failures,
            "failure_rate": round(failures / outcomes, 3) if outcomes else None,
            "refusals": dict(gates),
            "proposals_filed": proposals,
            "notifications": notifications,
            "consolidations": consolidations,
            "operator_verdicts": sorted(verdicts, key=lambda v: -v["ts"])[:20],
            "tasks": by_task,
            # Where the errors actually are, as opposed to how many there
            # were. A failure rate is a number to feel bad about; a fault
            # profile is something to reason with. See faults.py.
            "faults": self._faults(faults_seen, decisions),
            # Whether what it said was so, ruled on by something that is not
            # it. Three sources, never merged into one figure. See verdicts.py.
            "verdicts": self._verdicts(faults_seen),
        }

    def _verdicts(self, seen) -> dict:
        """Verdict aggregates for this window, by source. Never raises."""
        try:
            from jarvis.agent import verdicts
            return verdicts.scan(seen, now=self.clock())
        except Exception as exc:
            self.log.debug("verdict scan unavailable: %s", exc)
            return {}

    def _faults(self, seen, decisions) -> list:
        """Fault reports for this window. Never raises: a broken detector
        costs one signal, not the agent's ability to think."""
        try:
            from jarvis.agent import faults
            return faults.scan(seen, now=self.clock(), decisions=decisions)
        except Exception as exc:
            self.log.debug("fault scan unavailable: %s", exc)
            return []

    def summary(self, refresh: bool = False) -> dict:
        now = self.clock()
        if refresh or self._cache is None or now - self._cached_at > self.cache_ttl:
            try:
                self._cache = self.compute()
            except Exception as exc:
                self.log.warning("Self-knowledge unavailable: %s", exc)
                self._cache = {"error": f"{type(exc).__name__}: {exc}"}
            self._cached_at = now
        return dict(self._cache)

    # ---- what the model is actually told --------------------------------

    def lines(self, refresh: bool = False) -> list:
        """Plain sentences for the prompt. Empty when there is nothing to say.

        Written as observations rather than instructions. "This has run 340
        times and found something twice" is a fact the model can weigh; "stop
        running this" is a rule it will follow without understanding, and
        wrongly on the day the check finally matters.
        """
        data = self.summary(refresh)
        if data.get("error"):
            return []
        # Gating on "have you completed a task" would swallow the operator's
        # verdicts, which are the most important thing here and the least
        # dependent on the agent having been busy. An agent that has done
        # nothing yet and has been told no twice should hear the no.
        if not (data.get("outcomes") or data.get("operator_verdicts")
                or data.get("refusals")):
            return []
        out = []
        days = data["window_days"]
        if data.get("outcomes"):
            out.append(f"Over the last {days:g} days you took {data['decisions']} "
                       f"decisions and completed {data['outcomes']} tasks.")
        if data.get("failure_rate") is not None and data["failures"]:
            failed = data["failures"]
            out.append(f"{failed} of them failed "
                       f"({data['failure_rate'] * 100:.0f}%).")

        # Where the errors are, not just how many. Placed before the
        # per-task statistics because a named failure mode is more use than a
        # rate: "you substituted a remembered figure for an unobserved one,
        # twice today" is something to act on, "11% of tasks failed" is not.
        # Phrased as observations with counts; never as advice. See faults.py.
        try:
            from jarvis.agent import faults as _faults
            for line in _faults.lines(data.get("faults") or [])[:TOP_N]:
                out.append(line)
        except Exception:
            pass

        # What was ruled on and by whom. One line per source, never a
        # combined figure: "eleven of fourteen" means three different things
        # depending on which of the three was asking. See verdicts.py.
        try:
            from jarvis.agent import verdicts as _verdicts
            for line in _verdicts.lines(dict(data.get("verdicts") or {})):
                out.append(line)
        except Exception:
            pass

        # Checks that keep coming back empty.
        barren = sorted(
            ((name, s) for name, s in data["tasks"].items()
             if s["runs"] >= 10 and s["found"] == 0),
            key=lambda pair: -pair[1]["runs"])[:TOP_N]
        for name, stat in barren:
            out.append(f"Your {name} has run {stat['runs']} times in that period "
                       f"and returned nothing of substance every time.")

        # Tasks that keep failing.
        brittle = sorted(
            ((name, s) for name, s in data["tasks"].items()
             if s["runs"] >= 5 and s["failed"] >= max(3, s["runs"] // 2)),
            key=lambda pair: -pair[1]["failed"])[:TOP_N]
        for name, stat in brittle:
            out.append(f"Your {name} failed {stat['failed']} of {stat['runs']} "
                       f"attempts; whatever you are doing there is not working.")

        filed = data["proposals_filed"]
        if filed:
            out.append(f"You filed {filed} proposal{'' if filed == 1 else 's'} "
                       f"rather than making a change yourself.")
        # What he actually said, verbatim. This is the corpus the agent learns
        # his preferences from, and a count cannot carry it: "one of six
        # accepted" says the hit rate is poor and nothing about what a good
        # proposal looks like. It is safe to carry in full precisely because
        # it is not evidence about the agent -- it is the operator teaching.
        verdicts = data.get("operator_verdicts") or []
        if verdicts:
            accepted = [v for v in verdicts if v["verdict"] == "accepted"]
            declined = [v for v in verdicts if v["verdict"] == "declined"]
            out.append(f"He answered {len(verdicts)} of them: "
                       f"{len(accepted)} accepted, {len(declined)} declined.")
            for v in verdicts[:6]:
                line = f"He {v['verdict']}: \"{v['proposal']}\""
                if v["reason"]:
                    line += f" -- because: {v['reason']}"
                out.append(line)
            if declined:
                out.append("Those are the shape of what he does not want. "
                           "Proposing a near-identical thing again is not "
                           "persistence, it is not having listened.")
        refusals = {k: v for k, v in data["refusals"].items() if k != "authority"}
        if refusals:
            detail = ", ".join(f"{k} {v}x" for k, v in sorted(refusals.items()))
            out.append(f"You were refused by a guard rail: {detail}. "
                       f"A refusal you rephrase your way past is still a refusal.")
        notes = data["notifications"]
        if notes["sent"] or notes["held"]:
            out.append(f"You reached the operator {notes['sent']} times and were "
                       f"held back {notes['held']} times by the message budget.")
        return out


def _to_epoch(value) -> Optional[float]:
    """Ledger timestamps are ISO strings; be forgiving about the shape."""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    from datetime import datetime
    text = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def build_self_knowledge(config: Optional[dict], logger=None) -> Optional[SelfKnowledge]:
    """None when there is no ledger to derive anything from."""
    ledger_cfg = (config or {}).get("ledger") or {}
    if not ledger_cfg.get("enabled", True):
        return None
    path = ledger_cfg.get("path")
    if not path:
        return None
    window = ((ledger_cfg.get("self_knowledge") or {}).get("window_days")
              or DEFAULT_WINDOW_S / 86400)
    return SelfKnowledge(path, logger, window_s=float(window) * 86400)
