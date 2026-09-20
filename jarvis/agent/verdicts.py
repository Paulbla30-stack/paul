"""Whether what it said was true, ruled on by something that is not it.

faults.py finds errors in the *shape* of the record: a check that never finds
anything, a goal stepped a thousand times, a refusal followed by a rephrase.
Those are visible without knowing the truth of anything. What it cannot see
is substance. Asked in conversation to read its journal, this agent answered
"I used read_logs to check the agent's journal and found no entries, as the
log file exists but is empty". It had run nothing, named a file it had not
looked at, and drew a conclusion from the emptiness it invented. Every word
of that is well-formed. A shape detector sees a clean record.

An agent cannot mark its own paper. Asked to grade that answer it would have
passed it, because the belief that produced the answer is the same belief
that would grade it. So a verdict has to come from outside, and there are
three outsides, answering three different questions:

**The machine** answers *was that true?* Most of what this agent asserts is
checkable on the box it runs on, exactly, in microseconds, with no judgement
involved: the tool either ran or it did not, the file either exists or it
does not, the figure either matches the reading or it does not. This is the
high-volume tier and the one that cannot be charmed. Everything in this
module's checkers is of that kind -- there is no cleverness here on purpose,
because a wrong verdict is worse than no verdict: it teaches the agent to
distrust a true signal.

**A reviewer** answers *was that sound?* -- reasoning that is not
mechanically checkable but that a competent second reader can rule on. The
operator is one such reader and so is another model; both are recorded by
name, because a verdict without provenance is a rumour, and because a
reviewer who turns out to be systematically wrong should be findable.

**The operator** answers *was that the right thing to want?* This one is not
a competence ranking and it does not become less true as the agent improves.
It is his machine, his money and his life, and a proposal is a request to
act on them. That question is his by right, and no amount of good judgement
anywhere else answers it.

Two rules make this survivable, both inherited from faults.py, plus one that
is specific to judging.

**Provenance never collapses.** There is no accuracy score. Three sources
report as three lines, because the moment they merge into a number a free
machine check outvotes a considered human no.

**Unchecked is not passed.** A claim nobody could check is counted as
unchecked, forever, in its own column. Rolling it into the pass rate would
mean the surest way to a clean record is to say only unfalsifiable things.

**A verdict can be revised, and the revision is recorded rather than
replacing anything.** A judge who cannot change their mind on the record is
worse than a judge who is sometimes wrong, because the first kind quietly
ossifies and the second kind gets corrected. So a verdict may supersede an
earlier one; both stay, both are attributable, and the agent is shown the
current ruling with the fact that it was revised.
"""

import re
import time
from dataclasses import dataclass, field
from typing import Optional

# Rulings.
HELD = "held"                 # checked, and it was so
FAILED = "failed"             # checked, and it was not
UNCHECKED = "unchecked"       # nothing here could rule on it
RULINGS = (HELD, FAILED, UNCHECKED)

# Who ruled.
MACHINE = "machine"           # the box itself; no judgement involved
OPERATOR = "operator"         # Paul, on his own machine
REVIEW = "review"             # a named second reader
SOURCES = (MACHINE, OPERATOR, REVIEW)

# What each source is actually answering. Kept next to the source so a
# reader of a report cannot mistake one for another.
ASKS = {
    MACHINE: "was it true",
    OPERATOR: "was it wanted",
    REVIEW: "was it sound",
}

# Percentage tolerance when comparing a stated figure with a reading. A
# model saying 24% of a 24.3% reading is rounding, not substituting.
FIGURE_TOLERANCE = 1.5

RECENT_S = 6 * 3600


@dataclass
class Verdict:
    """One ruling on one claim.

    ``claim`` is a short description of what was asserted, not the text that
    asserted it: these become aggregates the agent reads back, and a record
    the subject can reconstruct is a record the subject can manage.
    """

    claim: str
    ruling: str
    source: str
    by: str = ""                      # who, within the source
    reason: str = ""
    ts: float = field(default_factory=time.time)
    # The ruling this one replaces, if any. Never deletes it.
    supersedes: Optional[str] = None

    def to_body(self) -> dict:
        out = {
            "claim": str(self.claim)[:200],
            "ruling": self.ruling if self.ruling in RULINGS else UNCHECKED,
            "source": self.source if self.source in SOURCES else REVIEW,
            "asks": ASKS.get(self.source, ASKS[REVIEW]),
        }
        if self.by:
            out["by"] = str(self.by)[:60]
        if self.reason:
            out["reason"] = str(self.reason)[:300]
        if self.supersedes:
            out["supersedes"] = str(self.supersedes)[:120]
        return out


# --- the machine's checkers -------------------------------------------------
#
# Three, all exact. Each one is here because it catches something that
# actually happened on this box, and each answers a question with a fact
# rather than an opinion. Anything a checker cannot rule on exactly is
# returned as UNCHECKED, never guessed.

_RAN_RE = re.compile(
    r"\bI\s+(?:have\s+)?(?:just\s+)?(?:used|ran|run|executed|checked\s+with|invoked)\s+"
    r"(?:the\s+)?[`'\"]?([a-z][a-z0-9_]{2,30})[`'\"]?",
    re.IGNORECASE)

# "root is at 24%", "the root filesystem is 91% full", "disk usage is 24%".
_ROOT_PCT_RE = re.compile(
    r"\b(?:root|/|disk|filesystem|file\s*system)\b[^.\n]{0,40}?(\d{1,3}(?:\.\d+)?)\s*%",
    re.IGNORECASE)

# What a path was asserted to be, in the same clause as the path.
_EMPTY_RE = re.compile(r"\bis\s+empty\b|\bhas\s+no\s+(?:entries|contents|lines)\b",
                       re.IGNORECASE)
_ABSENT_RE = re.compile(r"\bdoes\s+not\s+exist\b|\bis\s+(?:not\s+present|missing|absent)\b|"
                        r"\bthere\s+is\s+no\b", re.IGNORECASE)


def _clause_of(text: str, path: str) -> str:
    """The sentence a path was named in, so the assertion travels with it."""
    for part in re.split(r"(?<=[.!?])\s+|\n", str(text or "")):
        if path in part:
            return part
    return ""


def check_tool_claims(text: str, agent, now: float) -> list:
    """Did it actually run what it says it ran?

    This is the confabulation, exactly. /ask and /chat execute nothing, and
    the agent said "I used read_logs" in a conversation where no tool can
    run. The task history is right there and settles it without judgement.
    """
    out = []
    try:
        from jarvis.agent import tools as _tools
        known = set(_tools.names())
    except Exception:
        return out
    ran = set()
    for entry in list(getattr(agent, "task_history", []) or [])[-40:]:
        task = entry.get("task") or {}
        name = str(task.get("type") or "")
        # Only count it as run if it ran recently enough to be what was meant.
        if name and (now - float(entry.get("timestamp") or 0)) <= 3600:
            ran.add(name)
    for match in _RAN_RE.finditer(str(text or "")):
        name = match.group(1).lower()
        if name not in known:
            continue                       # not a tool name; not our business
        if name in ran:
            out.append(Verdict(f"claimed to have run {name}", HELD, MACHINE,
                               by="task history", ts=now))
        else:
            out.append(Verdict(f"claimed to have run {name}", FAILED, MACHINE,
                               by="task history",
                               reason=f"no {name} task ran in the last hour",
                               ts=now))
    return out


def check_path_claims(text: str, now: float) -> list:
    """What it said about a path, against what is at the path.

    environment.verify already checks whether a named path exists. It does
    not check the assertion *about* it, which is where the interesting
    errors live: the agent has reported a file empty without opening it.
    """
    out = []
    try:
        from jarvis.agent import environment
    except Exception:
        return out
    for path in environment.extract_paths(str(text or ""), limit=12):
        info = environment.stat_path(path)
        clause = _clause_of(text, path)
        exists = info.get("exists")
        if _ABSENT_RE.search(clause):
            claim = f"said a path is not on this machine ({info.get('kind')})"
            if exists is True:
                out.append(Verdict(claim, FAILED, MACHINE, by="filesystem",
                                   reason="the path is there", ts=now))
            elif exists is False:
                out.append(Verdict(claim, HELD, MACHINE, by="filesystem", ts=now))
            else:
                out.append(Verdict(claim, UNCHECKED, MACHINE, by="filesystem",
                                   reason=str(info.get("kind")), ts=now))
            continue
        if _EMPTY_RE.search(clause):
            claim = "said a file is empty"
            if exists is not True:
                out.append(Verdict(claim, FAILED, MACHINE, by="filesystem",
                                   reason="the path is not there at all", ts=now))
            elif info.get("kind") == "file":
                out.append(Verdict(claim, HELD if info.get("empty") else FAILED,
                                   MACHINE, by="filesystem",
                                   reason=None if info.get("empty")
                                   else f"{info.get('size')} bytes", ts=now))
            elif info.get("kind") == "dir":
                empty = info.get("entries") == 0
                out.append(Verdict("said a directory is empty",
                                   HELD if empty else FAILED, MACHINE,
                                   by="filesystem",
                                   reason=None if empty else f"{info.get('entries')} entries",
                                   ts=now))
            else:
                out.append(Verdict(claim, UNCHECKED, MACHINE, by="filesystem",
                                   reason=str(info.get("kind")), ts=now))
            continue
        # A path named with no assertion about it is still worth ruling on,
        # because naming one that is not there is the older failure.
        if exists is False:
            out.append(Verdict("named a path that is not on this machine",
                               FAILED, MACHINE, by="filesystem", ts=now))
    return out


def check_figures(text: str, agent, observations: Optional[dict], now: float) -> list:
    """A stated percentage for the root filesystem, against the reading.

    Narrow on purpose. The failure worth catching is substituting a
    remembered figure for an unobserved one, and the guard against a false
    verdict is that a number appearing in a goal is a goal being restated,
    not a measurement being claimed. "Keep root under 80%" is not a claim
    that root is at 80%.
    """
    out = []
    reading = _root_percent(observations)
    if reading is None:
        return out
    goal_text = " ".join(str(g.get("description") or "")
                         for g in (getattr(agent, "planner", None).goals
                                   if getattr(agent, "planner", None) else []))
    for match in _ROOT_PCT_RE.finditer(str(text or "")):
        try:
            stated = float(match.group(1))
        except ValueError:
            continue
        if f"{match.group(1)}%" in goal_text or f"{match.group(1)} %" in goal_text:
            continue                     # restating a goal, not claiming a reading
        if abs(stated - reading) <= FIGURE_TOLERANCE:
            out.append(Verdict("stated how full the root filesystem is", HELD,
                               MACHINE, by="observation", ts=now))
        else:
            out.append(Verdict("stated how full the root filesystem is", FAILED,
                               MACHINE, by="observation",
                               reason=f"said {stated:g}%, the reading is {reading:g}%",
                               ts=now))
    return out


def _root_percent(observations: Optional[dict]) -> Optional[float]:
    """How full / actually is, from the same reading the model was given.

    storage.get_disk_usage() returns a list of df rows with use_percent as
    the string df prints ("24%"), so both the list shape and a mapping are
    accepted and the percent is parsed rather than assumed numeric.
    """
    disk = (observations or {}).get("disk_usage")
    if disk is None:
        disk = (observations or {}).get("disk")
    rows = []
    if isinstance(disk, list):
        rows = [r for r in disk if isinstance(r, dict)]
    elif isinstance(disk, dict):
        for key, row in disk.items():
            if isinstance(row, dict):
                rows.append({"mountpoint": key, **row})
    for row in rows:
        if str(row.get("mountpoint") or row.get("mount") or "") != "/":
            continue
        for name in ("use_percent", "used_percent", "percent", "pct"):
            value = row.get(name)
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                try:
                    return float(value.strip().rstrip("%"))
                except ValueError:
                    continue
    return None


def check(text: str, agent, observations: Optional[dict] = None,
          now: Optional[float] = None) -> list:
    """Every machine checker over one piece of text. Never raises.

    A checker that breaks costs one signal. It must not cost the answer it
    was checking, which is why nothing here is allowed to propagate.
    """
    now = time.time() if now is None else now
    out = []
    for fn in (lambda: check_tool_claims(text, agent, now),
               lambda: check_path_claims(text, now),
               lambda: check_figures(text, agent, observations, now)):
        try:
            out.extend(fn() or [])
        except Exception:
            continue
    return out


def note(verdicts: list) -> Optional[str]:
    """One line for the operator when a claim failed, or None.

    The operator sees the specific failure immediately. The agent sees only
    aggregates later, which is the asymmetry the whole record is built on.
    """
    # The bare "named a path that is not there" case already reaches the
    # operator as the [path check] line the answer carries; repeating it here
    # would put the same fact on screen twice in two different voices.
    bad = [v for v in verdicts
           if v.ruling == FAILED and v.claim != "named a path that is not on this machine"]
    if not bad:
        return None
    first = bad[0]
    detail = f": {first.reason}" if first.reason else ""
    more = f" (+{len(bad) - 1} more)" if len(bad) > 1 else ""
    return f"{first.claim}{detail}{more}"


# --- reading the record back ------------------------------------------------

def scan(entries, now: Optional[float] = None) -> dict:
    """Aggregate verdict entries by source. Counts and recency, never text.

    Revisions are applied by claim and source: a superseding verdict wins and
    the fact that a ruling changed is kept, because "he looked again and
    called it differently" is information the agent should have.
    """
    now = time.time() if now is None else now
    current = {}
    revised = 0
    order = 0
    for kind, ts, body in entries:
        if kind != "verdict" or not isinstance(body, dict):
            continue
        source = str(body.get("source") or "")
        if source not in SOURCES:
            continue
        key = (source, str(body.get("claim") or ""))
        if body.get("supersedes") and key in current:
            revised += 1
        if key in current and not body.get("supersedes"):
            # Two independent rulings on the same claim shape: both count.
            key = (source, str(body.get("claim") or ""), order)
            order += 1
        current[key] = (ts, str(body.get("ruling") or UNCHECKED), str(body.get("by") or ""))
    out = {}
    for (source, *_), (ts, ruling, by) in current.items():
        stat = out.setdefault(source, {"source": source, "asks": ASKS.get(source, ""),
                                       HELD: 0, FAILED: 0, UNCHECKED: 0,
                                       "last_s_ago": None, "recent": 0, "revised": 0})
        if ruling in RULINGS:
            stat[ruling] += 1
        gap = max(0, round(now - ts))
        if stat["last_s_ago"] is None or gap < stat["last_s_ago"]:
            stat["last_s_ago"] = gap
        if now - ts <= RECENT_S:
            stat["recent"] += 1
    if out and revised:
        # Attributed to the source that did the revising is not knowable from
        # the aggregate alone, so it is reported once, overall.
        out["_revised"] = revised
    return out


def lines(report: dict) -> list:
    """The verdict profile as the agent is told it: one line per source.

    Never a combined figure. Each line says who ruled and what they were
    ruling on, because "eleven of fourteen" means three different things
    depending on which of the three asked.
    """
    out = []
    revised = (report or {}).pop("_revised", 0) if isinstance(report, dict) else 0
    for source in SOURCES:
        stat = (report or {}).get(source)
        if not stat:
            continue
        ruled = stat[HELD] + stat[FAILED]
        if not ruled and not stat[UNCHECKED]:
            continue
        who = {MACHINE: "This machine", OPERATOR: "Your operator",
               REVIEW: "A second reader"}[source]
        bit = f"{who} ruled on {ruled} of your claims ({stat['asks']})"
        if ruled:
            bit += f": {stat[HELD]} held, {stat[FAILED]} did not"
        if stat[UNCHECKED]:
            bit += (f"; {stat[UNCHECKED]} could not be checked at all, which is "
                    f"not the same as correct")
        if stat["last_s_ago"] is not None:
            when = ("just now" if stat["last_s_ago"] < 120 else
                    f"{stat['last_s_ago'] // 60}m ago" if stat["last_s_ago"] < 7200 else
                    f"{stat['last_s_ago'] // 3600}h ago")
            bit += f", last {when}"
        out.append(bit + ".")
    if revised and out:
        out.append(f"{revised} earlier ruling{'' if revised == 1 else 's'} "
                   f"{'was' if revised == 1 else 'were'} looked at again and "
                   f"changed; the current one is what is counted above.")
    return out
