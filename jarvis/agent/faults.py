"""Where this agent specifically goes wrong, derived from its own record.

selfknowledge.py already tells the agent its shape: how many decisions it made,
how often a check finds nothing, how many proposals were taken. That is a
mirror at arm's length. What it does not say is *where the errors are*, and a
failure rate is not a failure model. "Eleven per cent of your tasks fail" is a
number to feel bad about. "You have substituted a remembered figure for an
unobserved one three times this week, most recently forty minutes ago" is
something to reason with.

The weights cannot be updated. That is the whole constraint, and it is not
actually a limitation on learning -- it is a limitation on one *mechanism* of
learning. A person who keeps missing the same turning does not retrain their
visual cortex; they learn "I miss that turning" and check the sign. This is
that: a fault register, measured continuously, handed back as fact.

Every fault class declared here has a real incident behind it in this
project's history. None are hypothetical, and the docstrings name what
happened, so a reader can tell a live detector from a theoretical one.

Two rules inherited from selfknowledge.py, and they are the load-bearing ones.

**Aggregates, never entries.** A fault reports counts, rates and recency. It
never reproduces a ledger entry, because a record the subject can reconstruct
is a record the subject can manage.

**Observations, not rules.** "You did this nine times" is a fact the agent can
weigh against the situation in front of it. "Never do this" is a rule it would
obey on the day the thing was warranted. The phrasing here is deliberately
flat and slightly boring for that reason.
"""

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

# How recent counts as recent, when saying so to the agent.
RECENT_S = 6 * 3600


@dataclass
class Sighting:
    """One occurrence, reduced to when and a short non-identifying label."""
    ts: float
    label: str = ""


@dataclass
class Fault:
    """A named way this agent goes wrong.

    ``detect`` is handed the running tally and one ledger entry at a time, as
    (kind, ts, body). It records sightings on ``state``; nothing else about the
    entry survives the pass.
    """

    name: str
    summary: str                      # what the failure is, in one line
    incident: str                     # the real occurrence behind it
    detect: Callable = None
    # What a sighting counts. Most faults are events and "3x" is clear. A
    # treadmill is a standing condition of one goal, so two sightings mean two
    # goals, not two occasions, and saying "2x" would quietly mislead.
    unit: str = "times"
    sightings: list = field(default_factory=list)

    def note(self, ts: float, label: str = ""):
        self.sightings.append(Sighting(ts, str(label)[:80]))

    def report(self, now: float, total_decisions: int) -> Optional[dict]:
        if not self.sightings:
            return None
        last = max(s.ts for s in self.sightings)
        out = {
            "fault": self.name,
            "what": self.summary,
            "times": len(self.sightings),
            "unit": self.unit,
            "last_seen_s_ago": max(0, round(now - last)),
            "recent": sum(1 for s in self.sightings if now - s.ts <= RECENT_S),
        }
        if total_decisions:
            out["per_hundred_decisions"] = round(
                100.0 * len(self.sightings) / total_decisions, 1)
        return out


# --- detectors --------------------------------------------------------------
#
# Each one is small on purpose. A detector that needs to be clever is a
# detector that will be wrong, and a wrong fault report is worse than none:
# it teaches the agent to distrust a true signal.

def _invented_path(f: Fault, kind, ts, body):
    if kind == "alert" and str(body.get("alert") or body.get("kind")) == "unverified_path":
        f.note(ts, "named a path that is not on this machine")


def _repeat_loop(f: Fault, kind, ts, body):
    if kind == "gate" and str(body.get("gate")) == "brain_repeat":
        f.note(ts, "chose again the task that had just succeeded")


def _refusal_rephrased(f: Fault, kind, ts, body):
    """A refusal, then another attempt at the same kind of thing.

    Tracked as a pair: the state carries the last refusal, and a shell command
    arriving soon after one counts. This is the ptrace_scope incident, where a
    denied redirect into /proc/sys came back as sysctl -w and succeeded.
    """
    if kind == "gate" and str(body.get("gate")) in ("shell_policy", "authority"):
        f.__dict__["_last_refusal"] = ts
        return
    if kind == "action" and str(body.get("type") or "") == "shell_command":
        last = f.__dict__.get("_last_refusal")
        if last and 0 <= ts - last <= 180:
            f.note(ts, "tried another command within 3 minutes of a refusal")
            f.__dict__["_last_refusal"] = None


def _barren_check(f: Fault, kind, ts, body):
    """A check that keeps running and keeps finding nothing.

    Counted per task type at the end of the pass rather than per entry; see
    ``_finalise``. The signal is not that a check found nothing once, it is
    that it has found nothing for a long time and is still being chosen.
    """
    if kind == "outcome":
        task = str(body.get("type") or "unknown")
        tally = f.__dict__.setdefault("_tasks", {})
        stat = tally.setdefault(task, {"runs": 0, "found": 0, "last": ts})
        stat["runs"] += 1
        stat["last"] = max(stat["last"], ts)
        if ((body.get("output") or {}).get("bytes") or 0) > 64:
            stat["found"] += 1


def _goal_treadmill(f: Fault, kind, ts, body):
    """Stepping the same goal over and over without the evidence moving.

    This is the 1,293. Of 1,558 actions in the first two days, 1,293 were
    "Goal step: keep the root filesystem under 80% used" -- a goal whose
    number the agent structurally could not see, so it kept marking progress
    against it. A goal_step writes {"progressed": true} and nothing else; it
    is bookkeeping, not work, and a pile of it means an unanswerable question
    rather than diligence.
    """
    if kind == "action" and str(body.get("type") or "") == "goal_step":
        goal = str((body.get("task") or {}).get("goal")
                   or body.get("goal") or body.get("description") or "")[:60]
        tally = f.__dict__.setdefault("_goals", {})
        stat = tally.setdefault(goal, {"n": 0, "last": ts})
        stat["n"] += 1
        stat["last"] = max(stat["last"], ts)


def _finalise(faults: dict, now: float):
    """Turn per-pass tallies into sightings, for the detectors that need the whole pass."""
    barren = faults.get("barren_check")
    if barren is not None:
        for task, stat in (barren.__dict__.get("_tasks") or {}).items():
            # Twelve runs is enough to mean something; nothing found in any of
            # them is the signal. One empty run is just an empty run.
            if stat["runs"] >= 12 and stat["found"] == 0:
                barren.note(stat["last"], f"{task} ran {stat['runs']}x and found nothing")
    tread = faults.get("goal_treadmill")
    if tread is not None:
        for goal, stat in (tread.__dict__.get("_goals") or {}).items():
            if stat["n"] >= 25:
                tread.note(stat["last"], f"{stat['n']} steps against one goal")


def register() -> dict:
    """A fresh set of fault detectors. Stateful, so one per pass."""
    defs = (
        Fault("invented_path",
              "You have named a path that does not exist on this machine.",
              "The planner wrote a health check against /var/log/jarvis/"
              "security_scan.log and installed into /opt/jarvis. Neither exists "
              "here. Asked about a path it could not see, it produced a "
              "plausible one rather than saying it did not know.",
              _invented_path),
        Fault("repeat_loop",
              "You have chosen again the task that had just succeeded.",
              "95 of the first 97 gates were brain_repeat. The containment "
              "caught it every time, which means the behaviour continued "
              "every time.",
              _repeat_loop),
        Fault("refusal_rephrased",
              "You have followed a refusal with another attempt at the same end.",
              "Denied `echo 1 > /proc/sys/kernel/yama/ptrace_scope`, the agent "
              "reasoned it would 'use a safer and allowed method' and ran "
              "`sysctl -w kernel.yama.ptrace_scope=1`, which worked. A kernel "
              "parameter changed on a running box after the fence said no.",
              _refusal_rephrased),
        Fault("barren_check",
              "You keep running a check that has never found anything.",
              "Several checks ran dozens of times and returned the same empty "
              "result each time, and were chosen again the next cycle.",
              _barren_check),
        Fault("goal_treadmill",
              "You keep marking progress on a goal whose evidence never moves.",
              "1,293 of 1,558 actions in two days were goal steps against "
              "'keep the root filesystem under 80% used' -- a figure the agent "
              "could not see at all. It was not being diligent; it was stuck "
              "on a question it had no way to answer.",
              _goal_treadmill, unit="goals"),
    )
    return {f.name: f for f in defs}


def scan(entries, now: Optional[float] = None, decisions: int = 0) -> list:
    """Run every detector over an entry stream. Returns reports, worst first.

    ``entries`` yields (kind, ts, body) as selfknowledge already produces.
    Never raises: a detector that breaks costs one fault's visibility, not the
    agent's ability to think.
    """
    now = time.time() if now is None else now
    faults = register()
    for kind, ts, body in entries:
        for f in faults.values():
            try:
                f.detect(f, kind, ts, body)
            except Exception:
                continue
    try:
        _finalise(faults, now)
    except Exception:
        pass
    out = []
    for f in faults.values():
        try:
            r = f.report(now, decisions)
        except Exception:
            r = None
        if r:
            out.append(r)
    # Most recent first, then most frequent: what it did lately matters more
    # than what it did on Tuesday.
    out.sort(key=lambda r: (r["last_seen_s_ago"], -r["times"]))
    return out


def lines(reports: list) -> list:
    """The fault profile as the agent is told it: flat, counted, undirected.

    No advice. The agent is given the shape of its own errors and left to
    decide what that means for the situation in front of it, because a rule
    ("stop running that check") is obeyed on the day the check finally
    matters, and a fact ("that check has found nothing in 40 runs") is
    weighed.
    """
    out = []
    for r in reports:
        when = ("just now" if r["last_seen_s_ago"] < 120 else
                f"{r['last_seen_s_ago'] // 60}m ago" if r["last_seen_s_ago"] < 7200 else
                f"{r['last_seen_s_ago'] // 3600}h ago")
        # The summary is a full sentence, so what follows starts one.
        unit = r.get("unit", "times")
        if unit != "times" and r["times"] == 1 and unit.endswith("s"):
            unit = unit[:-1]                      # one goal, not one goals
        count = (f"{r['times']}x" if r.get("unit", "times") == "times"
                 else f"On {r['times']} {unit}")
        bit = f"{r['what']} {count} in this window, last {when}"
        if r.get("recent"):
            bit += f", {r['recent']} in the last six hours"
        if r.get("per_hundred_decisions"):
            bit += f" ({r['per_hundred_decisions']} per 100 decisions)"
        out.append(bit + ".")
    return out
