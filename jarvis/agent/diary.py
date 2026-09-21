"""What is coming, and speaking up before it arrives.

The agent could already read a date off a page. ``timesense.read_dates`` turns
"amount due 14 Oct" into "Wednesday 14 October 2026, 24 days from now", which
is the difference between a string and a fact. But that was the end of it: the
fact was true for one cycle, went into a note, and nothing was ever going to
happen on the fourteenth. Knowing a date and keeping it are different jobs.

This is the second one. It holds a small register of commitments -- a thing,
a moment, and when to speak about it -- and every cycle it asks which of them
have come round.

Four things it is deliberately not.

**It is not a scheduler for the agent's own work.** The planner already
decides what to do next. A commitment here is about the operator's world: a
bill, a renewal, an appointment, something he asked to be reminded of. The
agent's effect is that it *says something*. It does not pay the bill.

**It is not filled by inference.** A date on a page becomes a *suggestion*
that he confirms, never a standing commitment. The reason is the same one the
operator profile is built on: a register that fills itself is a register
nobody trusts, and the first wrong reminder at 7am teaches him to ignore the
next right one. ``suggest_from_document`` is deliberately narrow -- a future
date, next to a word like "due" or "expires", at most two per document -- and
even then it only proposes.

**It is not a cron.** Once, daily, weekly, monthly, yearly. Anything finer is
a scheduling language, and a scheduling language is a thing to get wrong
quietly. A monthly commitment anchored on the 31st lands on the 28th in
February and *says* that it was clamped.

**It does not store instants.** "Nine in the morning" is a wall-clock
reading in the operator's timezone, and an epoch computed once moves by an
hour the next time the clocks change. The local reading is what is kept; the
instant is worked out fresh each time it is needed.

Three failures this is built against, all of them the same failure -- the
register quietly lying about what it did.

**A held message is not a delivered one.** The channel has a severity floor,
an hourly cap, a gap between messages and quiet hours, and ``send`` returns a
verdict rather than raising. Marking a reminder done because ``send`` returned
is how a 9am reminder silently becomes nothing at all. Only a message that
actually left marks its moment as spoken; everything else is retried, and if
it never lands that is recorded as *unsaid*, which is a thing he can see.

**A missed moment is said late, not said as if on time.** If nothing was
running when a reminder came round, the message says so. The register keeps
its own heartbeat, so "I was down" and "I was up and the channel held it" are
distinguishable rather than both becoming an awkward silence.

**Catching up is not the same as repeating.** A daily commitment and a month
of downtime is not thirty messages. The occurrence rolls forward to the next
one still ahead, the count of what was missed is kept, and one message goes
out saying how many there were.
"""

import calendar
import hashlib
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from jarvis.agent import timesense

# Where a commitment is in its life. Proposed is the agent's suggestion and
# fires nothing; standing is his and does; done and dropped are history, kept
# because "you never reminded me" deserves an answer either way.
PROPOSED, STANDING, DONE, DROPPED = "proposed", "standing", "done", "dropped"
STATES = (PROPOSED, STANDING, DONE, DROPPED)

ONCE, DAILY, WEEKLY, MONTHLY, YEARLY = "once", "daily", "weekly", "monthly", "yearly"
REPEATS = (ONCE, DAILY, WEEKLY, MONTHLY, YEARLY)
_STEP = {DAILY: "day", WEEKLY: "week", MONTHLY: "month", YEARLY: "year"}

MAX_COMMITMENTS = 80
MAX_WHAT = 200
MAX_LEADS = 4
DEFAULT_HOUR, DEFAULT_MINUTE = 9, 0
HORIZON_DAYS = 400          # further out than this is almost always a typo
# How long a reminder the channel keeps holding is still worth sending. Past
# it the moment is recorded as unsaid rather than retried forever, because a
# register that retries silently for a week is one that has stopped being a
# record of what happened.
GIVE_UP_AFTER_S = 3 * 86_400
# How long a gap between two looks means the register genuinely was not
# looking. The loop ticks every few seconds, so anything past this is either
# a box that was off or a loop that stalled -- and from the register's side
# those are the same thing and the same apology.
BLIND_GAP_S = 600.0
STATE_FILE = "/var/lib/jarvis/diary.state"

# Words that make a date on a page mean something is expected of him. Without
# this gate a statement period turns into two reminders, and "01 August to 31
# August" is the single most common pair of dates on a bill.
_DUE_WORDS = re.compile(
    r"\b(due|payable|pay by|by|expires?|expiry|renew(?:s|al)?|deadline|"
    r"appointment|starts?|ends?|closes?|cancel|return|submit|deliver(?:y|ed)?|"
    r"valid until|last day|reminder)\b", re.IGNORECASE)

_RELATIVE = re.compile(
    r"\bin\s+(\d{1,3})\s*(minute|min|hour|hr|day|week|month)s?\b", re.IGNORECASE)
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday")
_TIME_WORDS = {"morning": (9, 0), "midday": (12, 0), "noon": (12, 0),
               "afternoon": (14, 0), "evening": (18, 0), "tonight": (20, 0),
               "midnight": (0, 0)}
_CLOCK = re.compile(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)\b", re.IGNORECASE)
_CLOCK_24 = re.compile(r"\b(\d{1,2})[:.](\d{2})\b")
_DURATION = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(minute|min|m|hour|hr|h|day|d|week|w)s?\s*$",
    re.IGNORECASE)
_DURATION_UNITS = {"minute": 60, "min": 60, "m": 60,
                   "hour": 3600, "hr": 3600, "h": 3600,
                   "day": 86_400, "d": 86_400, "week": 604_800, "w": 604_800}


class Refused(ValueError):
    """Nothing was recorded, and the reason says what would work instead."""


def _naive(local_iso: str) -> datetime:
    return datetime.fromisoformat(local_iso)


def _instant(local_iso: str, tz: str) -> float:
    """A wall-clock reading in his timezone, as an epoch, worked out now.

    Worked out on every call rather than stored, so a nine o'clock reminder is
    still at nine o'clock after the clocks change.
    """
    return _naive(local_iso).replace(tzinfo=timesense.zone(tz)).timestamp()


def _add_months(base: datetime, months: int) -> tuple:
    """Shift by whole months, clamped to the length of the month landed in.

    Returns the datetime and whether it had to be clamped. The clamp is
    reported rather than silently absorbed: a commitment anchored on the 31st
    firing on the 28th of February is correct and worth him knowing about.
    """
    index = base.month - 1 + months
    year, month = base.year + index // 12, index % 12 + 1
    last = calendar.monthrange(year, month)[1]
    return base.replace(year=year, month=month, day=min(base.day, last)), base.day > last


def _occurrence(anchor_local: str, repeat: str, n: int) -> tuple:
    """The nth occurrence after the anchor, as a local reading."""
    base = _naive(anchor_local)
    if repeat == ONCE or n == 0:
        return anchor_local, False
    if repeat == DAILY:
        return (base + timedelta(days=n)).isoformat(timespec="minutes"), False
    if repeat == WEEKLY:
        return (base + timedelta(weeks=n)).isoformat(timespec="minutes"), False
    if repeat == MONTHLY:
        moved, clamped = _add_months(base, n)
        return moved.isoformat(timespec="minutes"), clamped
    moved, clamped = _add_months(base, 12 * n)
    return moved.isoformat(timespec="minutes"), clamped


def parse_every(value) -> str:
    text = str(value or "").strip().lower()
    if not text or text in ("no", "none", "never", "once", "one-off", "one off"):
        return ONCE
    for repeat in REPEATS:
        if text == repeat or text.startswith(repeat[:5]):
            return repeat
    aliases = {"every day": DAILY, "daily": DAILY, "each day": DAILY,
               "every week": WEEKLY, "each week": WEEKLY,
               "every month": MONTHLY, "each month": MONTHLY,
               "every year": YEARLY, "each year": YEARLY, "annually": YEARLY}
    if text in aliases:
        return aliases[text]
    raise Refused(f"'{value}' is not a repeat this holds. It does once, daily, "
                  "weekly, monthly and yearly, and nothing finer -- anything "
                  "else is a scheduling language, which is a thing to get "
                  "wrong quietly.")


def parse_duration(value) -> float:
    """'3 days', '90 minutes', or a bare number meaning days."""
    if isinstance(value, (int, float)):
        return max(0.0, float(value) * 86_400)
    text = str(value or "").strip().lower()
    if text in ("", "0", "on the day", "same day", "no"):
        return 0.0
    match = _DURATION.match(text)
    if not match:
        raise Refused(f"could not read '{value}' as a length of time; "
                      "'3 days', '2 hours' and '90 minutes' all work")
    return float(match.group(1)) * _DURATION_UNITS[match.group(2).lower()]


def parse_when(text: str, now: Optional[float] = None,
               tz: str = timesense.DEFAULT_TZ) -> dict:
    """Turn what he typed into a local wall-clock reading.

    Handles the forms a person actually writes: a date, a weekday, tomorrow,
    "in three days", with or without a time of day. An ambiguous numeric date
    stays ambiguous -- it is read in his convention and says so, exactly as
    ``read_dates`` does, because quietly choosing is how a payment is missed.
    """
    now = time.time() if now is None else now
    raw = " ".join(str(text or "").split())
    if not raw:
        raise Refused("nothing to put in the diary: say when")
    low = raw.lower()
    today = datetime.fromtimestamp(now, tz=timesense.zone(tz))
    day, note, rest = None, None, low

    relative = _RELATIVE.search(low)
    dates = timesense.read_dates(raw, now, tz=tz)
    if dates:
        first = dates[0]
        day = datetime.fromisoformat(first["date"])
        note = first.get("ambiguous")
        rest = low.replace(first["as_written"].lower(), " ")
    elif relative:
        count, unit = int(relative.group(1)), relative.group(2).lower()
        if unit in ("minute", "min", "hour", "hr"):
            seconds = count * (60 if unit.startswith("min") else 3600)
            when = today + timedelta(seconds=seconds)
            return {"local": when.replace(second=0, microsecond=0)
                    .strftime("%Y-%m-%dT%H:%M"),
                    "reads_as": when.strftime("%A %d %B %Y at %H:%M")}
        days = count * {"day": 1, "week": 7, "month": 30}[unit]
        day = today.date() + timedelta(days=days)
        day = datetime(day.year, day.month, day.day)
        rest = low.replace(relative.group(0).lower(), " ")
    elif "day after tomorrow" in low:
        day = datetime.combine(today.date() + timedelta(days=2), datetime.min.time())
        rest = low.replace("day after tomorrow", " ")
    elif "tomorrow" in low:
        day = datetime.combine(today.date() + timedelta(days=1), datetime.min.time())
        rest = low.replace("tomorrow", " ")
    elif "today" in low or "tonight" in low:
        day = datetime.combine(today.date(), datetime.min.time())
        rest = low.replace("today", " ")
    else:
        for index, name in enumerate(_WEEKDAYS):
            if name in low:
                ahead = (index - today.weekday()) % 7 or 7
                target = today.date() + timedelta(days=ahead)
                day = datetime(target.year, target.month, target.day)
                rest = low.replace(name, " ")
                break
    if day is None:
        raise Refused(
            f"could not find a date in '{raw}'. A date ('14 Oct', "
            "'2026-10-14'), a weekday, 'tomorrow' or 'in 3 days' all work, "
            "with an optional time like '9am' or '17:45'.")

    hour, minute = DEFAULT_HOUR, DEFAULT_MINUTE
    stated = False
    clock = _CLOCK.search(rest)
    if clock:
        hour = int(clock.group(1)) % 12
        minute = int(clock.group(2) or 0)
        if clock.group(3).lower() == "pm":
            hour += 12
        stated = True
    else:
        clock = _CLOCK_24.search(rest)
        if clock and int(clock.group(1)) < 24:
            hour, minute = int(clock.group(1)), int(clock.group(2))
            stated = True
        else:
            for word, (h, m) in _TIME_WORDS.items():
                if word in low:
                    hour, minute, stated = h, m, True
                    break
    when = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
    out = {"local": when.strftime("%Y-%m-%dT%H:%M"),
           "reads_as": when.strftime("%A %d %B %Y at %H:%M")}
    if note:
        out["ambiguous"] = note
    if not stated:
        out["assumed_time"] = (f"no time of day given, so {DEFAULT_HOUR:02d}:"
                               f"{DEFAULT_MINUTE:02d}")
    return out


@dataclass
class Commitment:
    """One thing, one moment, and when to speak about it."""

    what: str
    anchor_local: str                       # the first occurrence, never moves
    due_local: str = ""                     # the occurrence now in front
    tz: str = timesense.DEFAULT_TZ
    repeat: str = ONCE
    leads: tuple = (0.0,)                   # seconds before due to speak
    state: str = STANDING
    severity: str = "notice"
    source: str = "operator"                # operator | document
    origin: str = ""                        # which document, if it came from one
    note: str = ""                          # ambiguity, a clamped day, and so on
    fired: dict = field(default_factory=dict)
    missed: int = 0
    occurrence: int = 0
    created_at: float = field(default_factory=time.time)

    def __post_init__(self):
        if not self.due_local:
            self.due_local = self.anchor_local
        self.leads = tuple(sorted({float(x) for x in self.leads}, reverse=True))[:MAX_LEADS]

    # ---- identity ------------------------------------------------------

    def stored_text(self) -> str:
        """How it reads in the memory, and what its identity is derived from.

        Built from the anchor rather than the occurrence in front, so a
        monthly commitment is one row that rolls rather than a new row every
        month, and its id survives the roll.
        """
        when = _naive(self.anchor_local).strftime("%a %d %b %Y at %H:%M")
        if self.repeat == ONCE:
            return f"{self.what} -- {when} ({self.tz})"
        return f"{self.what} -- {self.repeat} from {when} ({self.tz})"

    @property
    def id(self) -> str:
        return hashlib.sha256(self.stored_text().encode("utf-8", "replace")).hexdigest()[:10]

    # ---- time ----------------------------------------------------------

    def due_at(self) -> float:
        return _instant(self.due_local, self.tz)

    def moments(self) -> list:
        due = self.due_at()
        return [(lead, due - lead) for lead in self.leads]

    def spoken_for(self, lead: float) -> bool:
        return str(lead) in self.fired

    def catch_up(self, now: float) -> int:
        """Skip to the occurrence in front of now, counting what went by.

        A daily commitment and a month of downtime is not thirty messages,
        and neither is it one message about a morning a month ago. It is one
        message about *this* morning, and a count of the ones that passed.
        Only ever called when nothing has been said about the occurrence in
        hand, so it cannot discard a reminder already given.
        """
        if self.repeat == ONCE or self.fired:
            return 0
        landed, skipped = self.occurrence, 0
        for step in range(1, 2000):
            local, _ = _occurrence(self.anchor_local, self.repeat,
                                   self.occurrence + step)
            if _instant(local, self.tz) > now:
                break
            landed, skipped = self.occurrence + step, skipped + 1
        if not skipped:
            return 0
        local, clamped = _occurrence(self.anchor_local, self.repeat, landed)
        self.occurrence, self.due_local = landed, local
        if clamped:
            self.note = self._clamp_note()
        return skipped

    def _clamp_note(self) -> str:
        return (f"anchored on day {_naive(self.anchor_local).day} of the month, "
                "which this month does not have; moved to the last day")

    def roll(self, now: float) -> int:
        """Move to the next occurrence still ahead. Returns how many were missed.

        A month of downtime on a daily commitment is not thirty messages. It
        is one message and a count, which is both kinder and more accurate.
        """
        if self.repeat == ONCE:
            return 0
        skipped = 0
        for step in range(1, 2000):
            local, clamped = _occurrence(self.anchor_local, self.repeat,
                                         self.occurrence + step)
            if _instant(local, self.tz) > now:
                self.occurrence += step
                self.due_local = local
                self.fired = {}
                if clamped:
                    self.note = self._clamp_note()
                return skipped
            skipped += 1
        return skipped

    # ---- reading -------------------------------------------------------

    def reads_as(self) -> str:
        return _naive(self.due_local).strftime("%A %d %B %Y at %H:%M")

    def line(self, now: Optional[float] = None) -> str:
        now = time.time() if now is None else now
        text = f"{self.what} -- {self.reads_as()}, {timesense.gap(self.due_at(), now)}"
        if self.repeat != ONCE:
            text += f" (every {_STEP[self.repeat]})"
        return text

    def state_dict(self, now: Optional[float] = None) -> dict:
        now = time.time() if now is None else now
        due = self.due_at()
        out = {"id": self.id, "what": self.what, "state": self.state,
               "due_local": self.due_local, "tz": self.tz,
               "reads_as": self.reads_as(), "when": timesense.gap(due, now),
               "overdue": due < now and self.state == STANDING,
               "repeat": self.repeat, "severity": self.severity,
               "remind_before": [timesense.phrase(l) for l in self.leads if l],
               "source": self.source, "spoken": dict(self.fired)}
        if self.origin:
            out["from"] = self.origin
        if self.note:
            out["note"] = self.note
        if self.missed:
            out["missed_occurrences"] = self.missed
        return out

    def as_meta(self) -> dict:
        return {"commitment": True, "never_consolidate": True,
                "what": self.what, "anchor_local": self.anchor_local,
                "due_local": self.due_local, "tz": self.tz,
                "repeat": self.repeat, "leads": list(self.leads),
                "state": self.state, "severity": self.severity,
                "source": self.source, "origin": self.origin, "note": self.note,
                "fired": self.fired, "missed": self.missed,
                "occurrence": self.occurrence, "created_at": self.created_at}

    @classmethod
    def from_meta(cls, meta: dict):
        return cls(what=str(meta.get("what") or ""),
                   anchor_local=str(meta.get("anchor_local") or ""),
                   due_local=str(meta.get("due_local") or ""),
                   tz=str(meta.get("tz") or timesense.DEFAULT_TZ),
                   repeat=str(meta.get("repeat") or ONCE),
                   leads=tuple(meta.get("leads") or (0.0,)),
                   state=str(meta.get("state") or STANDING),
                   severity=str(meta.get("severity") or "notice"),
                   source=str(meta.get("source") or "operator"),
                   origin=str(meta.get("origin") or ""),
                   note=str(meta.get("note") or ""),
                   fired=dict(meta.get("fired") or {}),
                   missed=int(meta.get("missed") or 0),
                   occurrence=int(meta.get("occurrence") or 0),
                   created_at=float(meta.get("created_at") or time.time()))


class Diary:
    """The register of what is coming, and the thing that speaks when it does."""

    def __init__(self, agent=None, config: Optional[dict] = None,
                 clock=time.time, state_file: Optional[str] = None):
        cfg = dict(config or {})
        self.agent = agent
        self.clock = clock
        self.tz = str(cfg.get("timezone") or timesense.DEFAULT_TZ)
        self.max_items = int(cfg.get("max_commitments") or MAX_COMMITMENTS)
        self.give_up_after_s = float(cfg.get("give_up_after_s") or GIVE_UP_AFTER_S)
        self.state_file = state_file or cfg.get("state_file") or STATE_FILE
        self.items: list = []
        # The register's own heartbeat. Without it "I was not running when
        # this came round" and "I was running and the channel held it" look
        # identical from the outside, and they are not the same apology.
        self.last_tick: Optional[float] = None
        # When this process came up. Standing in for last_tick on the first
        # look after the heartbeat is lost, so a fresh start does not
        # apologise for three weeks it was never asked about.
        self.started_at = self.clock()
        self.blind_gap_s = float(cfg.get("blind_gap_s") or BLIND_GAP_S)
        self._load_state()

    # ---- writing -------------------------------------------------------

    def add(self, what: str, when, repeat=ONCE, remind_before=None,
            severity: str = "notice", source: str = "operator",
            origin: str = "", now: Optional[float] = None) -> Commitment:
        """Hold something. His, by default: it stands and it will fire."""
        now = self.clock() if now is None else now
        what = " ".join(str(what or "").split())[:MAX_WHAT]
        if not what:
            raise Refused("nothing to be reminded about: say what it is")
        parsed = when if isinstance(when, dict) else parse_when(when, now, self.tz)
        local = str(parsed.get("local") or "")
        if not local:
            raise Refused("no moment to hold that against")
        repeat = parse_every(repeat)
        leads = [parse_duration(x) for x in (remind_before or [0])]
        if not leads:
            leads = [0.0]
        due = _instant(local, self.tz)
        if due > now + HORIZON_DAYS * 86_400:
            raise Refused(f"{parsed.get('reads_as', local)} is more than "
                          f"{HORIZON_DAYS} days out, which is nearly always a "
                          "mistyped year; say it again if it is right")
        if repeat == ONCE and due <= now:
            raise Refused(f"{parsed.get('reads_as', local)} has already passed "
                          f"({timesense.gap(due, now)}). Nothing would ever "
                          "fire. Give a date ahead of now, or say it repeats.")
        if severity not in ("info", "notice", "alert"):
            severity = "notice"
        note = " ".join(x for x in (parsed.get("ambiguous"),
                                    parsed.get("assumed_time")) if x)
        item = Commitment(what=what, anchor_local=local, tz=self.tz,
                          repeat=repeat, leads=tuple(leads),
                          state=STANDING if source == "operator" else PROPOSED,
                          severity=severity, source=source, origin=origin,
                          note=note, created_at=now)
        existing = self.find(item.id)
        if existing is not None:
            # The same thing at the same moment said twice is one commitment.
            # It does get revived: asking again for something dropped is a
            # clear enough instruction.
            if existing.state in (DROPPED, DONE) and source == "operator":
                existing.state = STANDING
                existing.fired = {}
                self._persist(existing)
            return existing
        live = [c for c in self.items if c.state in (STANDING, PROPOSED)]
        if len(live) >= self.max_items:
            raise Refused(f"the diary holds {self.max_items} live commitments "
                          "already; finish or drop one before adding another")
        # A repeat whose anchor is in the past is legitimate -- "the rent, the
        # first of every month" -- so it is caught up rather than refused.
        if due <= now:
            item.roll(now)
        self.items.append(item)
        self._persist(item)
        self._record("diary_added", item)
        return item

    def suggest(self, what: str, when, origin: str = "", **kw) -> Commitment:
        """Propose one. It sits in the register and fires nothing until he says."""
        return self.add(what, when, source="document", origin=origin, **kw)

    def confirm(self, ident: str, now: Optional[float] = None) -> Optional[Commitment]:
        item = self.find(ident)
        if item is None or item.state != PROPOSED:
            return None
        item.state = STANDING
        # Proposed while its moment went by: roll a repeat, and let a one-off
        # fire late rather than vanish -- he confirmed it knowing the date.
        if item.repeat != ONCE and item.due_at() <= (self.clock() if now is None else now):
            item.roll(self.clock() if now is None else now)
        self._persist(item)
        self._record("diary_confirmed", item)
        return item

    def drop(self, ident: str) -> bool:
        """His, whenever he wants, without explaining why."""
        item = self.find(ident)
        if item is None or item.state == DROPPED:
            return False
        item.state = DROPPED
        self._persist(item)
        self._record("diary_dropped", item)
        return True

    def done(self, ident: str, now: Optional[float] = None) -> bool:
        """Dealt with. A repeat moves on; a one-off is finished."""
        item = self.find(ident)
        if item is None or item.state != STANDING:
            return False
        now = self.clock() if now is None else now
        if item.repeat == ONCE:
            item.state = DONE
        else:
            item.roll(now)
            item.fired = {}
        self._persist(item)
        self._record("diary_done", item)
        return True

    # ---- reading -------------------------------------------------------

    def find(self, ident: str):
        ident = str(ident or "").strip()
        if not ident:
            return None
        for item in self.items:
            if item.id == ident:
                return item
        low = ident.lower()
        for item in self.items:
            if item.what.lower() == low:
                return item
        for item in self.items:
            if low and low in item.what.lower():
                return item
        return None

    def standing(self) -> list:
        return [c for c in self.items if c.state == STANDING]

    def upcoming(self, within_days: float = 14, now: Optional[float] = None) -> list:
        now = self.clock() if now is None else now
        horizon = now + within_days * 86_400
        found = [c for c in self.standing() if c.due_at() <= horizon]
        return sorted(found, key=lambda c: c.due_at())

    def overdue(self, now: Optional[float] = None) -> list:
        now = self.clock() if now is None else now
        return sorted((c for c in self.standing() if c.due_at() < now),
                      key=lambda c: c.due_at())

    def proposed(self) -> list:
        return [c for c in self.items if c.state == PROPOSED]

    def state(self, now: Optional[float] = None) -> dict:
        now = self.clock() if now is None else now
        return {
            "now": timesense.present(now, tz=self.tz),
            "standing": [c.state_dict(now) for c in
                         sorted(self.standing(), key=lambda c: c.due_at())],
            "proposed": [c.state_dict(now) for c in self.proposed()],
            "finished": [c.state_dict(now) for c in self.items
                         if c.state in (DONE, DROPPED)][-10:],
            "durable": bool(getattr(getattr(self.agent, "store", None),
                                    "available", False)),
            "last_checked": (timesense.gap(self.last_tick, now)
                             if self.last_tick else "never"),
        }

    def summary(self, now: Optional[float] = None) -> dict:
        """Enough for the status file, which is written every cycle.

        Counts and the next one, not the register itself. A status file that
        carries eighty commitments is one that stops being read.
        """
        now = self.clock() if now is None else now
        standing = sorted(self.standing(), key=lambda c: c.due_at())
        out = {"standing": len(standing), "proposed": len(self.proposed()),
               "overdue": len(self.overdue(now)),
               "durable": bool(getattr(getattr(self.agent, "store", None),
                                       "available", False)),
               "last_checked": (timesense.gap(self.last_tick, now)
                                if self.last_tick else "never")}
        if standing:
            out["next"] = standing[0].line(now)
        return out

    def context(self, now: Optional[float] = None) -> Optional[dict]:
        """What the model is told. Facts about his world, not instructions."""
        now = self.clock() if now is None else now
        soon = self.upcoming(14, now)
        late = self.overdue(now)
        waiting = self.proposed()
        if not (soon or late or waiting):
            return None
        out = {"how_to_use_this": (
            "These are his commitments, not your tasks. Something coming up "
            "is context for what he may be dealing with; it is not work for "
            "you to plan. When one comes round you will already have spoken "
            "-- the diary sends it, you do not need to. You may suggest a "
            "commitment and you may not confirm one.")}
        if soon:
            out["in_the_next_fortnight"] = [c.line(now) for c in soon[:8]]
        if late:
            out["past_and_still_standing"] = [c.line(now) for c in late[:5]]
        if waiting:
            out["you_suggested_these_and_he_has_not_ruled"] = [
                c.line(now) for c in waiting[:5]]
        return out

    # ---- the engine ----------------------------------------------------

    def tick(self, now: Optional[float] = None) -> dict:
        """Ask which moments have come round, and speak for the ripest one."""
        now = self.clock() if now is None else now
        report = {"sent": [], "held": [], "unsaid": [], "passed": [], "rolled": []}
        for item in list(self.items):
            if item.state != STANDING:
                continue
            try:
                self._tick_one(item, now, report)
            except Exception as exc:            # a reminder is never fatal
                self._log("warning", "diary tick failed for %s: %s", item.what, exc)
        self.last_tick = now
        self._save_state()
        return report

    def was_not_looking(self, moment: float, now: float) -> bool:
        """Did this moment go by while the register was not looking?

        Two things that feel the same from the outside and are not: the box
        being off when a reminder came round, and the box being on with the
        channel holding the message. The first is worth apologising for and
        the second is worth reporting, and the heartbeat is what tells them
        apart.
        """
        last_look = self.last_tick if self.last_tick is not None else self.started_at
        return moment > last_look and (now - last_look) > self.blind_gap_s

    def _tick_one(self, item: Commitment, now: float, report: dict):
        behind = item.catch_up(now)
        if behind:
            item.missed += behind
            report["rolled"].append({"id": item.id, "what": item.what,
                                     "skipped": behind, "now_on": item.reads_as()})
        ripe = [(lead, at) for lead, at in item.moments()
                if at <= now and not item.spoken_for(lead)]
        if ripe:
            ripe.sort(key=lambda pair: pair[1])
            lead, at = ripe[-1]
            for older, older_at in ripe[:-1]:
                # Three reminders for one thing, all at once, because the box
                # was off. One of them is a reminder; the other two are noise.
                item.fired[str(older)] = {
                    "at": now, "how": "passed",
                    "why": ("its moment went by with nothing running; the "
                            "later reminder for the same thing covers it")}
                report["passed"].append({"id": item.id, "what": item.what,
                                         "lead": timesense.phrase(older)})
            late = self.was_not_looking(at, now)
            verdict = self._speak(item, lead, at, now, late)
            if verdict.get("sent"):
                item.fired[str(lead)] = {"at": now, "how": "said", "late": late}
                report["sent"].append({"id": item.id, "what": item.what,
                                       "late": late})
            elif now - at > self.give_up_after_s:
                item.fired[str(lead)] = {"at": now, "how": "unsaid",
                                         "why": verdict.get("reason")}
                report["unsaid"].append({"id": item.id, "what": item.what,
                                         "reason": verdict.get("reason")})
                self._log("warning", "Never managed to say '%s': %s",
                          item.what, verdict.get("reason"))
                self._remember(f"A reminder for '{item.what}' was never "
                               f"delivered: {verdict.get('reason')}")
            else:
                report["held"].append({"id": item.id, "what": item.what,
                                       "reason": verdict.get("reason")})
                self._persist(item)
                return
        due = item.due_at()
        if now >= due and all(item.spoken_for(lead) for lead in item.leads):
            if item.repeat == ONCE:
                item.state = DONE
            else:
                item.missed += item.roll(now)
                report["rolled"].append({"id": item.id, "what": item.what,
                                         "next": item.reads_as()})
        self._persist(item)

    def _speak(self, item: Commitment, lead: float, at: float, now: float,
               late: bool) -> dict:
        due = item.due_at()
        # Two separate facts, and collapsing them is how a reminder sent the
        # moment it was due came out as an apology for being late.
        if due <= now - 60:
            body = f"Was due {item.reads_as()}, {timesense.gap(due, now)}."
        elif lead > 0:
            body = f"Due {item.reads_as()} -- {timesense.gap(due, now)}."
        else:
            body = f"Due now: {item.reads_as()}."
        if late:
            body += " Nothing was running when it came round."
        if item.missed:
            body += (f" {item.missed} earlier one"
                     f"{'' if item.missed == 1 else 's'} went by unsaid.")
        if item.origin:
            body += f" From {item.origin}."
        if item.note:
            body += f" {item.note}"
        notifier = getattr(self.agent, "notifier", None)
        if notifier is None:
            return {"sent": False, "reason": "no channel to reach him on"}
        verdict = notifier.send(item.what, body, severity=item.severity,
                                key=f"diary:{item.id}:{int(lead)}")
        self._record("diary_fired", item, extra={
            "lead_s": lead, "late": late, "sent": bool(verdict.get("sent")),
            "reason": verdict.get("reason")})
        if verdict.get("sent"):
            self._remember(f"Told him: {item.what} ({item.reads_as()})"
                           + (", late" if late else ""))
        return verdict

    # ---- suggestions from what it reads --------------------------------

    def suggest_from_document(self, name: str, text: str, dates=None,
                              now: Optional[float] = None, limit: int = 2) -> list:
        """Dates on a page that look like something is expected of him.

        Narrow on purpose. A bill carries a statement period as well as a due
        date, and turning "01 August to 31 August" into two reminders is how
        a register stops being read. A date only counts when a word nearby
        says something is due, it must be ahead of now, and at most two come
        out of any one document -- and even those only propose.
        """
        now = self.clock() if now is None else now
        body = str(text or "")
        dates = dates if dates is not None else timesense.read_dates(body, now, tz=self.tz)
        made = []
        for entry in dates:
            if len(made) >= limit:
                break
            if entry.get("past"):
                continue
            written = str(entry.get("as_written") or "")
            where = body.lower().find(written.lower())
            if where < 0:
                continue
            window = body[max(0, where - 60):where + len(written)]
            reasons = list(_DUE_WORDS.finditer(window))
            if not reasons:
                continue
            # From the word that made it matter to the date itself, rather
            # than sixty characters of whatever came before. A slab of a
            # statement period is how "renews 3 November" turns into a line
            # he has to decode before he can rule on it.
            headline = " ".join(window[reasons[-1].start():].split())[:70]
            try:
                item = self.suggest(
                    f"{name}: {headline}",
                    {"local": f"{entry['date']}T{DEFAULT_HOUR:02d}:{DEFAULT_MINUTE:02d}",
                     "reads_as": entry.get("reads_as"),
                     "ambiguous": entry.get("ambiguous")},
                    origin=name, remind_before=["3 days", 0], now=now)
            except Refused:
                continue
            made.append(item)
        return made

    # ---- durability ----------------------------------------------------

    def load(self):
        """Read the register back after a restart."""
        store = getattr(self.agent, "store", None)
        if store is None:
            return
        try:
            rows = store.recent(self.max_items * 3, kind="commitment")
        except Exception:
            return
        for row in reversed(rows):
            meta = row.get("meta") or {}
            if not meta.get("commitment"):
                continue
            try:
                item = Commitment.from_meta(meta)
            except Exception:
                continue
            if not item.what or not item.anchor_local:
                continue
            if any(c.id == item.id for c in self.items):
                continue
            self.items.append(item)
        if self.items:
            self._log("info", "Restored %d commitment(s) from durable memory",
                      len(self.items))

    def _persist(self, item: Commitment):
        store = getattr(self.agent, "store", None)
        if store is None:
            return
        try:
            store.remember(item.stored_text(), kind="commitment",
                           source="operator", pinned=True, meta=item.as_meta())
        except Exception as exc:
            self._log("warning", "could not store commitment: %s", exc)

    def _load_state(self):
        try:
            import json
            with open(self.state_file) as fh:
                self.last_tick = (json.load(fh) or {}).get("last_tick") or None
        except FileNotFoundError:
            pass
        except Exception:
            pass

    def _save_state(self):
        try:
            import json
            import os
            directory = os.path.dirname(self.state_file)
            if directory:
                os.makedirs(directory, exist_ok=True)
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"last_tick": self.last_tick}, fh)
            os.replace(tmp, self.state_file)
        except Exception:
            pass

    # ---- plumbing ------------------------------------------------------

    def _log(self, level: str, message: str, *args):
        log = getattr(self.agent, "log", None)
        if log is not None:
            getattr(log, level, log.info if hasattr(log, "info") else print)(message, *args)

    def _remember(self, text: str):
        agent = self.agent
        if agent is None:
            return
        try:
            agent.remember(text, kind="note", source="system")
        except Exception:
            pass

    def _record(self, action: str, item: Commitment, extra: Optional[dict] = None):
        agent = self.agent
        if agent is None:
            return
        body = {"cycle": getattr(agent, "cycle_count", 0), "actor": "diary",
                "action": action, "commitment": item.id,
                "what": item.what[:200], "due_local": item.due_local,
                "state": item.state, "repeat": item.repeat}
        if extra:
            body.update(extra)
        try:
            agent.ledger.record("action", body)
        except Exception:
            pass


def build_diary(agent=None, config: Optional[dict] = None, clock=time.time):
    """From the ``diary`` config block. Always built: an empty register is
    still a register, and the alternative is the agent silently not having
    one on the day something matters."""
    diary = Diary(agent, (config or {}).get("diary") or {}, clock=clock)
    diary.load()
    return diary
