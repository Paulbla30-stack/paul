"""Time: what it is now, how long things actually take, and the gap between.

A model's sense of duration comes from the text it was trained on, and that
text is overwhelmingly about people doing things. "I'll look into it" is days.
"A quick refactor" is an afternoon. "We should migrate the database" is a
quarter. So when a model estimates, it reaches for a human-project prior --
and the thing actually doing the work here is a machine that finishes in
forty milliseconds. It will say a week for something that takes a second, not
because it is guessing badly but because it is guessing from the wrong
distribution.

The fix is not to tell it to be faster. It is to stop it estimating at all
where a measurement exists. This agent has run thousands of tasks with
timestamps on every one, so how long a security scan takes is a fact about
this machine and not a matter of opinion. ``durations()`` reads that record
and hands back the real numbers.

Where no measurement exists -- anything that needs a person, a registration,
a delivery -- the honest answer is a different scale entirely, and saying so
is more useful than a number. That is what ``scale()`` is for. Three bands,
and the point is the distance between them:

    machine   milliseconds to seconds   this agent doing something
    service   seconds to minutes        asking another system
    person    hours to days             anything needing the operator
    world     days to weeks             registrations, deliveries, reboots

The second half is the present. A clock reading is not the same as knowing
what day it is: whether it is a weekday, whether the operator is asleep,
whether a date written in a bill has already passed. The agent runs on UTC
and the person it serves does not, and a date in a document means nothing
until it is expressed as a distance from today.

One rule carried from everywhere else in this codebase: an ambiguous date is
reported as ambiguous. 05/10/2026 is the fifth of October here and the tenth
of May in America, and quietly picking one is how a payment is missed. It is
read in the operator's convention and *says* that it did.
"""

import re
import statistics
import time
from datetime import datetime, timezone
from typing import Optional

DEFAULT_TZ = "Europe/London"

# The bands, and what lives in each. The numbers are seconds.
MACHINE, SERVICE, PERSON, WORLD = "machine", "service", "person", "world"
BANDS = (
    (MACHINE, 0.0, 5.0, "this agent doing something on this machine"),
    (SERVICE, 5.0, 120.0, "asking another system and waiting for it"),
    (PERSON, 120.0, 86_400.0, "anything that needs the operator to look"),
    (WORLD, 86_400.0, None, "registrations, deliveries, reboots, anything "
                            "with an organisation on the other end"),
)

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}

# 14 Oct, 14 October 2026, 14th Oct
_DMY_WORD = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
    r"(?:\s+(\d{4}))?\b", re.IGNORECASE)
# Oct 14, October 14th 2026
_MDY_WORD = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b", re.IGNORECASE)
# 2026-10-14
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
# 14/10/2026 or 14-10-26
_NUMERIC = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b")


def _zone(name: str = DEFAULT_TZ):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:                       # no tzdata: UTC is honest, not wrong
        return timezone.utc


def phrase(seconds: float) -> str:
    """A duration in the unit that is true for it.

    Deliberately says milliseconds where milliseconds are the answer. Rounding
    40ms up to "less than a second" is how the machine scale disappears and
    the human one takes over.
    """
    seconds = abs(float(seconds))
    if seconds < 1:
        return f"{round(seconds * 1000)}ms"
    if seconds < 90:
        return f"{seconds:.1f}s" if seconds < 10 else f"{round(seconds)}s"
    if seconds < 5400:
        return f"{round(seconds / 60)} minutes"
    if seconds < 172_800:
        return f"{round(seconds / 3600)} hours"
    if seconds < 1_209_600:
        return f"{round(seconds / 86400)} days"
    if seconds < 7_776_000:
        return f"{round(seconds / 604800)} weeks"
    return f"{round(seconds / 2_629_800)} months"


def gap(then: float, now: Optional[float] = None) -> str:
    """How long between two moments, said the way a person would say it."""
    now = time.time() if now is None else now
    delta = now - then
    if abs(delta) < 2:
        return "just now"
    return f"{phrase(delta)} ago" if delta > 0 else f"in {phrase(-delta)}"


def band_of(seconds: float) -> str:
    for name, low, high, _ in BANDS:
        if seconds >= low and (high is None or seconds < high):
            return name
    return WORLD


# --- the present ------------------------------------------------------------

def present(now: Optional[float] = None, tz: str = DEFAULT_TZ,
            quiet_hours=None, started_at: Optional[float] = None) -> dict:
    """What time it is, in both the machine's terms and the operator's.

    A clock reading is not knowing what day it is. Whether it is a weekday,
    whether the person this serves is asleep, and how far into the week it is
    all change what is worth doing, and none of them are in a UNIX timestamp.
    """
    now = time.time() if now is None else now
    utc = datetime.fromtimestamp(now, tz=timezone.utc)
    local = datetime.fromtimestamp(now, tz=_zone(tz))
    offset = local.utcoffset()
    hours = round(offset.total_seconds() / 3600) if offset else 0
    out = {
        "utc": utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "operator_local": local.strftime("%A %d %B %Y, %H:%M"),
        "timezone": tz,
        "offset_from_utc_hours": hours,
        "weekday": local.strftime("%A"),
        "weekend": local.weekday() >= 5,
    }
    if hours:
        out["note"] = (f"this machine runs on UTC and your operator is {hours:+d} "
                       "hours from it; a time you quote should say which one "
                       "it is")
    if quiet_hours and len(quiet_hours) == 2:
        start, end = int(quiet_hours[0]), int(quiet_hours[1])
        h = local.hour
        asleep = h >= start or h < end if start > end else start <= h < end
        out["operator_quiet_hours"] = f"{start:02d}:00-{end:02d}:00 local"
        out["operator_probably_asleep"] = asleep
        if asleep:
            out["what_that_means"] = ("a notice will be held until the morning; "
                                      "only an alert goes through now")
    if started_at:
        out["running_for"] = phrase(now - started_at)
    return out


# --- how long its own work actually takes -----------------------------------

def durations(task_history, now: Optional[float] = None, window: int = 200) -> dict:
    """Measured times per task type, from the agent's own record.

    Measured, not estimated, and that is the whole point. How long a security
    scan takes on this machine is a fact with two hundred observations behind
    it, and a fact beats a prior drawn from text about people doing projects.

    Needs an end time to measure against, so it uses the gap between
    consecutive entries as the upper bound on a task's own duration, and says
    so: this is "how long a cycle containing one of these took", which is the
    honest reading of what the record holds.
    """
    now = time.time() if now is None else now
    rows = [e for e in list(task_history or [])[-window:] if isinstance(e, dict)]
    rows.sort(key=lambda e: float(e.get("timestamp") or 0))
    seen: dict = {}
    for i, entry in enumerate(rows[:-1]):
        task = entry.get("task") or {}
        kind = str(task.get("type") or "")
        start = float(entry.get("timestamp") or 0)
        end = float(rows[i + 1].get("timestamp") or 0)
        if not kind or end <= start:
            continue
        elapsed = end - start
        if elapsed > 600:            # a gap with sleep in it, not a task
            continue
        seen.setdefault(kind, []).append(elapsed)
    out = {}
    for kind, values in seen.items():
        if len(values) < 2:
            continue
        median = statistics.median(values)
        out[kind] = {"runs": len(values), "typical": phrase(median),
                     "slowest": phrase(max(values)),
                     "band": band_of(median), "typical_seconds": round(median, 3)}
    return out


def scale(measured: Optional[dict] = None) -> list:
    """The distance between what a machine takes and what a person takes.

    Said plainly, with the measured numbers where there are any. This exists
    because the failure is specific and repeatable: asked how long something
    would take, a model answers from text about people, and text about people
    has no entries at all for "forty milliseconds".
    """
    lines = ["How long things take here is not how long they take in the "
             "writing you learned from. Four different scales are in play and "
             "they are orders of magnitude apart:"]
    for name, low, high, what in BANDS:
        if high is None:
            span = f"{phrase(low)} and up"
        elif low == 0:
            span = f"under {phrase(high)}"
        else:
            span = f"{phrase(low)} to {phrase(high)}"
        lines.append(f"  {name}: {span} -- {what}")
    if measured:
        fast = sorted(measured.items(), key=lambda kv: kv[1]["typical_seconds"])
        shown = ", ".join(f"{k} {v['typical']}" for k, v in fast[:6])
        lines.append(f"Measured on this machine, from your own record: {shown}. "
                     "These are observations, not estimates -- quote them "
                     "rather than guessing a duration.")
    lines.append("When you do not have a measurement, say which scale it is on "
                 "rather than inventing a number. 'This finishes in under a "
                 "second; getting the registration approved is days' is useful. "
                 "'About a week' for either of them is not.")
    return lines


# --- dates written in documents ---------------------------------------------

def read_dates(text: str, now: Optional[float] = None,
               tz: str = DEFAULT_TZ, day_first: bool = True,
               limit: int = 12) -> list:
    """Dates found in a piece of text, expressed as a distance from today.

    "Amount due 14 Oct" means nothing on its own. "14 October, 24 days from
    now" is a fact you can act on, and "14 October, 6 days ago" is a different
    situation entirely. This is what makes a bill worth reading.

    Ambiguity is reported, never resolved silently: 05/10/2026 is the fifth of
    October in the operator's convention and the tenth of May in another, and
    quietly choosing is how a payment gets missed.
    """
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now, tz=_zone(tz)).date()
    found, seen = [], set()

    def add(day, month, year, raw, ambiguous=False, alt=None):
        # year arrives as a string from every regex group. Comparing a string
        # with an int raises TypeError, which the except below swallowed, so
        # every date written *with* a year vanished silently and only the
        # bare "14 Oct" survived. A bill's due date is exactly the kind that
        # carries a year. Coerced first, and the except is narrowed to the
        # thing it is actually for: a date that does not exist.
        try:
            year = today.year if year in (None, "") else int(year)
            if year < 100:
                year += 2000
            when = datetime(year, int(month), int(day)).date()
        except (ValueError, TypeError):
            return
        if when in seen:
            return
        seen.add(when)
        days = (when - today).days
        entry = {"date": when.isoformat(), "as_written": raw,
                 "reads_as": when.strftime("%A %d %B %Y")}
        if days == 0:
            entry["when"] = "today"
        elif days > 0:
            entry["when"] = f"{days} day{'' if days == 1 else 's'} from now"
        else:
            entry["when"] = f"{-days} day{'' if days == -1 else 's'} ago"
        entry["past"] = days < 0
        if ambiguous and alt:
            entry["ambiguous"] = (
                f"written {raw}, which is {entry['reads_as']} in the "
                f"operator's convention and {alt} in another; read as the "
                "first, and worth confirming before acting on it")
        found.append(entry)

    body = str(text or "")[:20_000]
    for m in _ISO.finditer(body):
        add(m.group(3), m.group(2), m.group(1), m.group(0))
    for m in _DMY_WORD.finditer(body):
        add(m.group(1), _MONTHS[m.group(2)[:3].lower()], m.group(3), m.group(0))
    for m in _MDY_WORD.finditer(body):
        add(m.group(2), _MONTHS[m.group(1)[:3].lower()], m.group(3), m.group(0))
    for m in _NUMERIC.finditer(body):
        a, b, y = int(m.group(1)), int(m.group(2)), m.group(3)
        first, second = (a, b) if day_first else (b, a)
        swappable = a <= 12 and b <= 12 and a != b
        alt = None
        if swappable:
            try:
                year = int(y)
                other = datetime(year + 2000 if year < 100 else year,
                                 first, second).date()
                alt = other.strftime("%d %B %Y")
            except ValueError:
                alt = None
        add(first, second, y, m.group(0), ambiguous=swappable, alt=alt)

    found.sort(key=lambda e: e["date"])
    return found[:limit]


def date_note(dates: list) -> Optional[str]:
    """One line for the operator about what the dates in a document mean."""
    if not dates:
        return None
    soon = [d for d in dates if not d["past"]]
    passed = [d for d in dates if d["past"]]
    bits = []
    if soon:
        first = soon[0]
        bits.append(f"{first['reads_as']} is {first['when']}")
    if passed:
        bits.append(f"{len(passed)} date(s) in it have already passed")
    unclear = [d for d in dates if d.get("ambiguous")]
    if unclear:
        bits.append(f"{len(unclear)} written ambiguously")
    return "; ".join(bits) if bits else None
