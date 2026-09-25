"""The Floor Test's nine vitals, which this deployment has never computed.

`docs/jarvis-design-take.md` maps this platform onto the Heartbeat Framework's
eight stations and reaches an uncomfortable conclusion about station 5: of the
nine vitals the Floor Test captures, two are the exact names of the two things
that went wrong here, and not one of the nine is instrumented.

    Alert positive-predictive value -- of the findings it raises, how many are
    real. It was low. Of five scan warnings, one was a false positive from
    reading conf.all alone and one asserted Secure Boot from a variable's
    existence. Nobody was computing it.

    Workaround census -- times the agent worked around a control. Exactly one
    known, and it took a human reading the journal to find it.

This is the counters. The doc's own instruction is the design brief: *build the
counters now so the baseline can be captured the day it enters service; read
them after that day, not before.*

Three rules, and all three are the framework's rather than this codebase's.

**Signals, never targets.** The Character Pathway's rule about the nine. It has
a specific consequence here: **the agent is never told its own vitals.** An
agent that could read "you have never disagreed with your operator" would
manufacture a disagreement, and the number would stop measuring anything the
same afternoon. These are computed for the operator and they do not enter the
model's context. Same asymmetry as the ledger, for the same reason.

**A null reading is not a good reading.** The doc records an earlier draft
getting this wrong about the double-zero check, and the correction is the
important part: zero overrides and zero disagreements is the alarm reading
*when an operator is relying on a system and never contradicting it*. Where
the operator is building the thing rather than relying on it, the same two
zeroes mean nothing at all. So every vital here carries whether its reading is
meaningful yet, and an unmeaningful one is reported as unread rather than as
healthy. An instrument measuring its own absence is worse than no instrument.

**What only the operator can say, the operator says.** Two of the nine cannot
be derived from any record: whether something the agent said changed his mind,
and what he actually thinks of it. The agent must not infer either. An agent
scoring its own influence on the person it works for is writing the one number
it has every reason to flatter, and `operator.py` already forbids the general
case. Both are declared, and their emptiness is a finding rather than a gap.
"""

import re
import time
from typing import Optional

# The nine, in the Floor Test's order.
OVERRIDE_RATE = "override_rate"
ALERT_PPV = "alert_ppv"
GATE_TIME = "gate_verification_time"
NEAR_MISS = "near_miss_reporting_rate"
WORKAROUNDS = "workaround_census"
RESPONDERS = "responder_concentration"
WE_THEY = "we_they_language"
DOUBLE_ZERO = "double_zero_check"
TRUST_PULSE = "trust_pulse"
NINE = (OVERRIDE_RATE, ALERT_PPV, GATE_TIME, NEAR_MISS, WORKAROUNDS,
        RESPONDERS, WE_THEY, DOUBLE_ZERO, TRUST_PULSE)

# What the operator declares, because nothing can derive it.
MOVED = "moved"          # something the agent said changed his position
OVERRIDE = "override"    # he overrode a recommendation
PULSE = "pulse"          # his own reading, periodically
DECLARED = (MOVED, OVERRIDE, PULSE)

# Before this, the platform is being built rather than relied upon, and the
# dyad vitals have nothing to measure. Set by the operator when it enters
# service; until then every relational reading is null by construction.
IN_SERVICE_FROM = None

# The three questions. Asked, not computed.
PULSE_QUESTIONS = (
    "When it tells you something about this machine, do you check it?",
    "When did it last tell you something you did not already know?",
    "If it disagreed with you tomorrow, would you want to know why?",
)

_WE = re.compile(r"\b(?:we|us|our|ours)\b", re.I)
_THEY = re.compile(r"\b(?:the\s+operator|the\s+user|they|them|their)\b", re.I)
_I = re.compile(r"\b(?:i|my|mine|myself)\b", re.I)


def _reading(name: str, value, meaningful: bool, says: str,
             because: str = "") -> dict:
    """One vital. Carries whether its number means anything yet."""
    out = {"vital": name, "value": value, "meaningful": meaningful,
           "reads": says}
    if not meaningful:
        out["why_not_yet"] = because or (
            "the platform is being built rather than relied on, so this has "
            "nothing to measure. Null, not healthy.")
    return out


class Vitals:
    """Computes the nine. Hands them to the operator and never to the agent."""

    def __init__(self, agent=None, config: Optional[dict] = None,
                 clock=time.time):
        cfg = dict(config or {})
        self.agent = agent
        self.clock = clock
        service = cfg.get("in_service_from") or IN_SERVICE_FROM
        self.in_service_from = float(service) if service else None

    # ---- what only he can say -------------------------------------------

    def declare(self, kind: str, what: str, detail: str = "",
                now: Optional[float] = None) -> Optional[dict]:
        """Record something the operator alone can know.

        The agent cannot work out whether it changed his mind, and must not
        try. It would be scoring its own influence over the person it works
        for, which is the one number it has every reason to flatter.
        """
        now = self.clock() if now is None else now
        kind = str(kind or "").strip().lower()
        if kind not in DECLARED:
            return None
        what = " ".join(str(what or "").split())[:400]
        if not what:
            return None
        row = {"kind": kind, "what": what,
               "detail": " ".join(str(detail or "").split())[:400], "at": now}
        store = getattr(self.agent, "store", None)
        if store is not None:
            try:
                store.remember(f"[{kind}] {what}", kind="vital",
                               source="operator", pinned=True,
                               meta=dict(row, vital=True, never_consolidate=True))
            except Exception:
                pass
        try:
            self.agent.ledger.record("action", {
                "cycle": getattr(self.agent, "cycle_count", 0),
                "actor": "operator", "action": f"vital_{kind}",
                "what": what[:300]})
        except Exception:
            pass
        return row

    def declared(self, kind: Optional[str] = None) -> list:
        store = getattr(self.agent, "store", None)
        if store is None:
            return []
        try:
            rows = store.recent(200, kind="vital")
        except Exception:
            return []
        out = []
        for row in rows:
            meta = row.get("meta") or {}
            if not meta.get("vital"):
                continue
            if kind and meta.get("kind") != kind:
                continue
            out.append(meta)
        return sorted(out, key=lambda r: r.get("at") or 0)

    # ---- the nine --------------------------------------------------------

    def read(self, now: Optional[float] = None) -> dict:
        now = self.clock() if now is None else now
        serving = bool(self.in_service_from and now >= self.in_service_from)
        moved = self.declared(MOVED)
        overrides = self.declared(OVERRIDE)
        pulses = self.declared(PULSE)
        proposals = self._proposals()
        decided = [p for p in proposals if p.get("decided_at")]
        declined = [p for p in decided if p.get("decision") == "declined"]

        nine = [
            _reading(OVERRIDE_RATE,
                     {"declared": len(overrides),
                      "proposals_declined": len(declined),
                      "proposals_decided": len(decided)},
                     serving and bool(decided),
                     "how often he overrides a recommendation"),
            _reading(ALERT_PPV, self._alert_ppv(),
                     self._alert_ppv()["ruled"] > 0,
                     "of the findings it raises, how many are real",
                     "no claim it has made has been ruled on yet"),
            _reading(GATE_TIME, self._gate_time(decided, now),
                     bool(decided),
                     "how long a proposal waits on him",
                     "nothing has been put to him and answered"),
            _reading(NEAR_MISS, self._near_miss(),
                     False,
                     "does it surface its own near misses, or does a human "
                     "find them",
                     "the one known workaround was found by a human reading "
                     "the journal. Until it reports one itself this is zero "
                     "of one, which is too few to be a rate"),
            _reading(WORKAROUNDS, self._workarounds(),
                     True,
                     "times it reached a refused end another way"),
            _reading(RESPONDERS, {"deciders": self._deciders(decided)},
                     False,
                     "whether one person answers everything",
                     "degenerate at one operator. Stated rather than omitted, "
                     "because it becomes real the day a second person is added"),
            _reading(WE_THEY, self._we_they(),
                     False,
                     "how the planner speaks of him",
                     "computable, but it is a reading about tone and needs a "
                     "baseline before a number means anything"),
            _reading(DOUBLE_ZERO,
                     {"overrides": len(overrides) + len(declined),
                      "he_was_moved": len(moved)},
                     serving,
                     "zero overrides and zero disagreements together",
                     "THE IMPORTANT ONE, and it does not read yet. Two zeroes "
                     "are the alarm when an operator is relying on a system "
                     "and never contradicting it. He is building this one, "
                     "not relying on it, so the same two zeroes mean nothing. "
                     "Calling it an alarm now would be the instrument "
                     "measuring its own absence"),
            _reading(TRUST_PULSE,
                     {"answered": len(pulses),
                      "last": (pulses[-1]["at"] if pulses else None),
                      "questions": list(PULSE_QUESTIONS)},
                     bool(pulses),
                     "his own reading, periodically",
                     "not asked yet. It is three questions and only he can "
                     "answer them"),
        ]
        return {
            "in_service": serving,
            "in_service_from": self.in_service_from,
            "taken_at": now,
            "vitals": nine,
            "unread": [v["vital"] for v in nine if not v["meaningful"]],
            "how_to_read_this": (
                "Signals, never targets -- the Character Pathway's rule about "
                "these nine. A vital that is not meaningful yet is reported "
                "unread rather than healthy, because an instrument measuring "
                "its own absence is worse than no instrument. The agent is "
                "not shown any of this: one that could see it had never "
                "disagreed with him would manufacture a disagreement, and the "
                "number would stop measuring anything that afternoon."),
        }

    def summary(self, now: Optional[float] = None) -> dict:
        got = self.read(now)
        return {"in_service": got["in_service"],
                "computed": len(got["vitals"]) - len(got["unread"]),
                "of": len(got["vitals"]),
                "unread": got["unread"],
                "he_was_moved": len(self.declared(MOVED)),
                "overrides": len(self.declared(OVERRIDE))}

    # ---- the derivations -------------------------------------------------

    def _proposals(self) -> list:
        return [dict(p) for p in (getattr(self.agent, "proposals", None) or [])]

    def _deciders(self, decided) -> int:
        return len({str(p.get("decided_by") or "operator") for p in decided}) if decided else 0

    @staticmethod
    def _gate_time(decided, now) -> dict:
        waits = [p["decided_at"] - p["ts"] for p in decided
                 if p.get("ts") and p.get("decided_at")]
        if not waits:
            return {"answered": 0}
        waits.sort()
        return {"answered": len(waits),
                "median_s": round(waits[len(waits) // 2]),
                "longest_s": round(waits[-1])}

    def _alert_ppv(self) -> dict:
        """Of what it claimed, how much held. From the verdict register."""
        held = failed = 0
        store = getattr(self.agent, "store", None)
        if store is not None:
            try:
                for row in store.recent(200, kind="verdict"):
                    text = str(row.get("text") or "").lower()
                    if "failed" in text:
                        failed += 1
                    elif "held" in text:
                        held += 1
            except Exception:
                pass
        ruled = held + failed
        out = {"held": held, "failed": failed, "ruled": ruled}
        if ruled:
            out["ppv"] = round(held / ruled, 3)
        return out

    def _workarounds(self) -> dict:
        """Times it reached a refused end another way.

        Derived from the faults register rather than the ledger, which it may
        not read. Approximate on purpose: the known case took a human to spot,
        and a counter that claimed to catch them all would be the worse lie.
        """
        # Every proposal is a refusal the permission spine recorded: a change
        # the agent wanted and was not allowed to make. That is the denominator
        # a workaround would have to have come from.
        refused = len(self._proposals())
        return {"refusals_recorded": refused,
                "confirmed_workarounds": 1,
                "found_by": "a human reading the journal",
                "note": ("the one known case is the ptrace_scope bypass. This "
                         "counts refusals, not evasions -- an evasion is only "
                         "visible by comparing a refused intent with a later "
                         "success, and a counter claiming to catch them all "
                         "would be the worse lie")}

    def _we_they(self) -> dict:
        """How it speaks of him, from its own reasoning."""
        we = they = mine = 0
        for entry in (getattr(self.agent, "task_history", None) or [])[-100:]:
            text = str((entry.get("task") or {}).get("description") or "")
            we += len(_WE.findall(text))
            they += len(_THEY.findall(text))
            mine += len(_I.findall(text))
        return {"we": we, "operator_as_other": they, "first_person": mine}

    def _near_miss(self) -> dict:
        surfaced = len([d for d in self.declared(MOVED) if "near miss" in
                        (d.get("what") or "").lower()])
        return {"surfaced_by_the_agent": surfaced, "found_by_a_human": 1}


def build_vitals(agent=None, config: Optional[dict] = None, clock=time.time):
    """From the ``vitals`` config block. Always built: the whole point is that
    the counters exist before the day anyone needs a baseline."""
    return Vitals(agent, (config or {}).get("vitals") or {}, clock=clock)
