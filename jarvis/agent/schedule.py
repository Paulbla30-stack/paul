"""The shape of his day: things that occupy a span rather than mark a moment.

The diary holds points. "Pay the gas bill on the fourteenth" is a moment and a
message: when it comes round, something gets said. That is most of what a
person needs remembering *about*, and none of what they need to know about
their own time.

An appointment is not a point. It starts, it lasts, it ends, and while it is
running he is not available for anything else. Three things follow from that
and none of them are expressible in the diary:

    two of them can collide, and something has to say so
    between them there are gaps, which is what "when am I free" means
    while one is running, he is in it

So this holds spans, and the diary keeps doing what it does. **There is one
firing path in this codebase and it is the diary's.** An appointment does not
learn how to speak; it registers a commitment, the diary says it, and
cancelling the appointment drops the commitment. Everything about held
messages, missed moments, catching up and lateness was solved once and is not
solved again here.

The honesty problem in this module is different from the diary's, and worse.

**An empty calendar is not a free day.** It is a calendar with nothing in it.
Every assistant that has ever said "you're free Thursday" from an empty
Thursday was guessing, confidently, about a person's life from a database it
knows is incomplete -- and unlike a wrong reminder, that one is invisible
until he has already said yes to something. So `free()` never returns bare
slots. Every answer it gives carries what it is actually a statement about:
the gaps in what has been written down, which is not the same claim.

**A clash is reported, never resolved.** Two things at the same time is a fact
about his life, not a data-integrity problem for the agent to tidy up. It says
both, says how much they overlap, and leaves them both standing. Quietly
moving one, or refusing the second, is how an appointment goes missing.

**An all-day entry is a banner, not a blocker.** "Dentist on Thursday" with no
time means he does not know when yet. Treating that as occupying the whole day
would make every other Thursday entry a clash, so it marks the day and gets
out of the way -- which is what a calendar does and what a naive span model
gets wrong.

Wall-clock throughout, for the reason the diary gives: an instant computed
once moves by an hour when the clocks change, and a two o'clock appointment is
at two o'clock.
"""

import hashlib
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from jarvis.agent import timesense
from jarvis.agent.diary import (
    DAILY, MONTHLY, ONCE, WEEKLY, YEARLY, Refused,
    _instant, _naive, _occurrence, parse_duration, parse_every, parse_when,
)

PROPOSED, STANDING, CANCELLED = "proposed", "standing", "cancelled"
STATES = (PROPOSED, STANDING, CANCELLED)
_STEP = {DAILY: "day", WEEKLY: "week", MONTHLY: "month", YEARLY: "year"}

# What an appointment's reminder is marked with in the diary, so the two
# registers can be told apart wherever both are shown.
CALENDAR = "calendar"

MAX_APPOINTMENTS = 120
MAX_WHAT = 200
MAX_WHERE = 120
DEFAULT_MINUTES = 60.0
MAX_MINUTES = 14 * 24 * 60.0
ALL_DAY_MINUTES = 1440.0
# Half an hour is the smallest warning that is any use for something you have
# to physically be at, and the longest that is not nagging.
DEFAULT_LEAD_S = 1800.0
HORIZON_DAYS = 400
# The window a "when am I free" question is actually about. Nobody means 3am.
DEFAULT_HOURS = (9, 18)
MIN_SLOT_MINUTES = 30.0

_FOR = re.compile(r"\bfor\s+(.+)$", re.IGNORECASE)
_UNTIL = re.compile(r"\b(?:to|until|till|-|–)\s*"
                    r"(\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)?)\s*$", re.IGNORECASE)
_AT_PLACE = re.compile(r"\b(?:at|in)\s+(?!\d)([A-Za-z][^,;]{1,60})$")
_CLOCKISH = re.compile(r"\d")

# What every answer about free time has to carry with it. Stated once here so
# it cannot be given without it.
NOT_THE_SAME_CLAIM = (
    "these are the gaps in what is written down, which is not the same as "
    "being free. Anything not in here is invisible to him.")


def _hhmm(local_iso: str) -> str:
    return _naive(local_iso).strftime("%H:%M")


def _plus(local_iso: str, minutes: float) -> str:
    return (_naive(local_iso) + timedelta(minutes=minutes)).isoformat(timespec="minutes")


def _span_minutes(text: str) -> Optional[float]:
    found = _FOR.search(text)
    if not found:
        return None
    try:
        return parse_duration(found.group(1).strip()) / 60.0
    except Refused:
        return None


def parse_span(text: str, now: Optional[float] = None,
               tz: str = timesense.DEFAULT_TZ) -> dict:
    """When it starts and how long it runs, from what he typed.

    "friday 2pm for an hour", "friday 2pm to 4pm", "friday 14:00-16:00",
    "friday 2pm" (an hour is assumed, and it says so), "thursday" (all day,
    because a day with no time is a day he does not yet know the time for).
    """
    now = time.time() if now is None else now
    raw = " ".join(str(text or "").split())
    if not raw:
        raise Refused("nothing to put in the calendar: say when")

    where = ""
    place = _AT_PLACE.search(raw)
    if place and not _CLOCKISH.search(place.group(1)):
        where = place.group(1).strip()
        raw = raw[:place.start()].strip() or raw

    minutes = _span_minutes(raw)
    head = _FOR.sub("", raw).strip() if minutes is not None else raw

    end_text = None
    if minutes is None:
        closing = _UNTIL.search(head)
        if closing:
            end_text = closing.group(1)
            head = head[:closing.start()].strip()

    when = parse_when(head, now, tz)
    start = when["local"]
    out = {"start_local": start, "reads_as": when.get("reads_as"), "where": where}
    for key in ("ambiguous",):
        if when.get(key):
            out[key] = when[key]

    if minutes is not None:
        out["minutes"] = minutes
    elif end_text:
        ends = parse_when(f"{_naive(start).strftime('%Y-%m-%d')} {end_text}", now, tz)
        length = (_naive(ends["local"]) - _naive(start)).total_seconds() / 60.0
        if length < 0:
            # Two in the afternoon until one in the morning is a real evening.
            # Only a *negative* span wraps: zero is not a midnight crossing,
            # it is someone typing the same time twice, and rolling it gave a
            # twenty-four hour appointment without a word.
            length += 1440.0
            out["note"] = "it ends the following day"
        out["minutes"] = length
    elif when.get("assumed_time"):
        # No time of day at all. He knows the day and not the hour, which is
        # a banner on the day rather than a claim on any part of it.
        out["all_day"] = True
        out["start_local"] = _naive(start).strftime("%Y-%m-%dT00:00")
        out["minutes"] = ALL_DAY_MINUTES
        out["note"] = "no time given, so it sits on the day rather than in it"
    else:
        out["minutes"] = DEFAULT_MINUTES
        out["assumed_length"] = f"no length given, so {int(DEFAULT_MINUTES)} minutes"

    if out["minutes"] <= 0:
        raise Refused("an appointment with no length in it is a reminder; "
                      "put it in the diary instead")
    if out["minutes"] > MAX_MINUTES:
        raise Refused(f"{out['minutes'] / 1440:.0f} days is longer than this "
                      "holds; if it really runs that long it is a period, "
                      "not an appointment")
    return out



@dataclass
class Appointment:
    """Something that occupies a span of his time."""

    what: str
    anchor_local: str
    minutes: float = DEFAULT_MINUTES
    start_local: str = ""
    tz: str = timesense.DEFAULT_TZ
    repeat: str = ONCE
    where: str = ""
    all_day: bool = False
    state: str = STANDING
    source: str = "operator"
    origin: str = ""
    note: str = ""
    remind_before_s: float = DEFAULT_LEAD_S
    commitment: str = ""
    occurrence: int = 0
    created_at: float = field(default_factory=time.time)

    def __post_init__(self):
        if not self.start_local:
            self.start_local = self.anchor_local
        self.minutes = float(self.minutes or DEFAULT_MINUTES)

    # ---- identity ------------------------------------------------------

    def stored_text(self) -> str:
        when = _naive(self.anchor_local).strftime("%a %d %b %Y at %H:%M")
        body = f"{self.what} -- {when}"
        if not self.all_day:
            body += f" for {int(self.minutes)} min"
        if self.repeat != ONCE:
            body += f", every {_STEP[self.repeat]}"
        return f"{body} ({self.tz})"

    @property
    def id(self) -> str:
        return hashlib.sha256(
            self.stored_text().encode("utf-8", "replace")).hexdigest()[:10]

    # ---- the span ------------------------------------------------------

    def starts_at(self) -> float:
        return _instant(self.start_local, self.tz)

    def ends_at(self) -> float:
        return self.starts_at() + self.minutes * 60.0

    def end_local(self) -> str:
        return _plus(self.start_local, self.minutes)

    def day(self) -> str:
        return self.start_local[:10]

    def covers(self, when: float) -> bool:
        return self.starts_at() <= when < self.ends_at()

    def overlaps(self, other) -> float:
        """Minutes these two share. An all-day entry blocks nothing."""
        if self.all_day or other.all_day:
            return 0.0
        first, second = max(self.starts_at(), other.starts_at()), \
            min(self.ends_at(), other.ends_at())
        return max(0.0, (second - first) / 60.0)

    def roll(self, now: float) -> int:
        """Move to the next occurrence still ahead. Returns how many passed."""
        if self.repeat == ONCE:
            return 0
        skipped = 0
        for step in range(1, 2000):
            local, _ = _occurrence(self.anchor_local, self.repeat,
                                   self.occurrence + step)
            if _instant(local, self.tz) + self.minutes * 60.0 > now:
                self.occurrence += step
                self.start_local = local
                return skipped
            skipped += 1
        return skipped

    # ---- reading -------------------------------------------------------

    def reads_as(self) -> str:
        start = _naive(self.start_local)
        if self.all_day:
            return start.strftime("%A %d %B %Y, all day")
        return (f"{start.strftime('%A %d %B %Y, %H:%M')}"
                f"-{_hhmm(self.end_local())}")

    def line(self, now: Optional[float] = None) -> str:
        now = time.time() if now is None else now
        text = f"{self.what} -- {self.reads_as()}"
        if self.where:
            text += f", {self.where}"
        text += f", {timesense.gap(self.starts_at(), now)}"
        if self.repeat != ONCE:
            text += f" (every {_STEP[self.repeat]})"
        return text

    def state_dict(self, now: Optional[float] = None) -> dict:
        now = time.time() if now is None else now
        out = {"id": self.id, "what": self.what, "state": self.state,
               "start_local": self.start_local, "end_local": self.end_local(),
               "minutes": self.minutes, "all_day": self.all_day,
               "tz": self.tz, "reads_as": self.reads_as(),
               "when": timesense.gap(self.starts_at(), now),
               "past": self.ends_at() <= now,
               "running": self.covers(now),
               "repeat": self.repeat, "source": self.source,
               # What is actually armed, not what is configured. An all-day
               # entry arms nothing, and one entered after its warning has
               # already gone by arms nothing either -- both were reporting
               # "he says something 30 minutes before" and both were wrong.
               # Configured is not proven, one register further along.
               "reminder": (timesense.phrase(self.remind_before_s)
                            if self.commitment and self.remind_before_s else None)}
        if self.where:
            out["where"] = self.where
        if self.origin:
            out["from"] = self.origin
        if self.note:
            out["note"] = self.note
        return out

    def as_meta(self) -> dict:
        return {"appointment": True, "never_consolidate": True,
                "what": self.what, "anchor_local": self.anchor_local,
                "start_local": self.start_local, "minutes": self.minutes,
                "tz": self.tz, "repeat": self.repeat, "where": self.where,
                "all_day": self.all_day, "state": self.state,
                "source": self.source, "origin": self.origin, "note": self.note,
                "remind_before_s": self.remind_before_s,
                "commitment": self.commitment, "occurrence": self.occurrence,
                "created_at": self.created_at}

    @classmethod
    def from_meta(cls, meta: dict):
        return cls(what=str(meta.get("what") or ""),
                   anchor_local=str(meta.get("anchor_local") or ""),
                   minutes=float(meta.get("minutes") or DEFAULT_MINUTES),
                   start_local=str(meta.get("start_local") or ""),
                   tz=str(meta.get("tz") or timesense.DEFAULT_TZ),
                   repeat=str(meta.get("repeat") or ONCE),
                   where=str(meta.get("where") or ""),
                   all_day=bool(meta.get("all_day")),
                   state=str(meta.get("state") or STANDING),
                   source=str(meta.get("source") or "operator"),
                   origin=str(meta.get("origin") or ""),
                   note=str(meta.get("note") or ""),
                   remind_before_s=float(meta.get("remind_before_s") or 0.0),
                   commitment=str(meta.get("commitment") or ""),
                   occurrence=int(meta.get("occurrence") or 0),
                   created_at=float(meta.get("created_at") or time.time()))


class Schedule:
    """What is in his day, what collides, and where the gaps are."""

    def __init__(self, agent=None, config: Optional[dict] = None,
                 clock=time.time):
        cfg = dict(config or {})
        self.agent = agent
        self.clock = clock
        self.tz = str(cfg.get("timezone") or timesense.DEFAULT_TZ)
        self.max_items = int(cfg.get("max_appointments") or MAX_APPOINTMENTS)
        hours = cfg.get("hours") or DEFAULT_HOURS
        self.hours = (int(hours[0]), int(hours[1]))
        self.lead_s = float(cfg.get("remind_before_s") or DEFAULT_LEAD_S)
        self.items: list = []

    # ---- writing -------------------------------------------------------

    def add(self, what: str, when, length=None, repeat=ONCE, where: str = "",
            remind_before=None, source: str = "operator", origin: str = "",
            now: Optional[float] = None) -> Appointment:
        now = self.clock() if now is None else now
        what = " ".join(str(what or "").split())[:MAX_WHAT]
        if not what:
            raise Refused("nothing to put in the calendar: say what it is")
        parsed = when if isinstance(when, dict) else parse_span(when, now, self.tz)
        start = str(parsed.get("start_local") or "")
        if not start:
            raise Refused("no moment to start that at")
        minutes = float(parsed.get("minutes") or DEFAULT_MINUTES)
        if length is not None:
            minutes = parse_duration(length) / 60.0
        repeat = parse_every(repeat)
        where = " ".join(str(where or parsed.get("where") or "").split())[:MAX_WHERE]
        lead = (self.lead_s if remind_before is None
                else parse_duration(remind_before))
        begins = _instant(start, self.tz)
        if begins > now + HORIZON_DAYS * 86_400:
            raise Refused(f"{parsed.get('reads_as', start)} is more than "
                          f"{HORIZON_DAYS} days out, which is nearly always a "
                          "mistyped year; say it again if it is right")
        if repeat == ONCE and begins + minutes * 60.0 <= now:
            raise Refused(f"{parsed.get('reads_as', start)} is already over "
                          f"({timesense.gap(begins, now)}). Put it in ahead of "
                          "now, or say it repeats.")
        note = " ".join(x for x in (parsed.get("note"), parsed.get("ambiguous"),
                                    parsed.get("assumed_length")) if x)
        item = Appointment(
            what=what, anchor_local=start, minutes=minutes, tz=self.tz,
            repeat=repeat, where=where, all_day=bool(parsed.get("all_day")),
            state=STANDING if source == "operator" else PROPOSED,
            source=source, origin=origin, note=note,
            remind_before_s=lead, created_at=now)
        existing = self.find(item.id)
        if existing is not None:
            if existing.state == CANCELLED and source == "operator":
                existing.state = STANDING
                self._arm(existing, now)
                self._persist(existing)
            return existing
        live = [a for a in self.items if a.state in (STANDING, PROPOSED)]
        if len(live) >= self.max_items:
            raise Refused(f"the calendar holds {self.max_items} appointments "
                          "already; cancel one before adding another")
        if repeat != ONCE and begins <= now:
            item.roll(now)
        self.items.append(item)
        if item.state == STANDING:
            self._arm(item, now)
        self._persist(item)
        self._record("appointment_added", item)
        return item

    def propose(self, what: str, when, **kw) -> Appointment:
        kw.setdefault("source", "document")
        return self.add(what, when, **kw)

    def confirm(self, ident: str, now: Optional[float] = None) -> Optional[Appointment]:
        item = self.find(ident)
        if item is None or item.state != PROPOSED:
            return None
        now = self.clock() if now is None else now
        item.state = STANDING
        if item.repeat != ONCE and item.ends_at() <= now:
            item.roll(now)
        self._arm(item, now)
        self._persist(item)
        self._record("appointment_confirmed", item)
        return item

    def cancel(self, ident: str) -> bool:
        item = self.find(ident)
        if item is None or item.state == CANCELLED:
            return False
        item.state = CANCELLED
        # The reminder goes with it. An appointment cancelled that still
        # warns him half an hour before is worse than one never entered.
        self._disarm(item)
        self._persist(item)
        self._record("appointment_cancelled", item)
        return True

    def move(self, ident: str, when, length=None,
             now: Optional[float] = None) -> Optional[Appointment]:
        """Same appointment, different time. The reminder moves with it."""
        item = self.find(ident)
        if item is None or item.state == CANCELLED:
            return None
        now = self.clock() if now is None else now
        parsed = when if isinstance(when, dict) else parse_span(when, now, self.tz)
        moved = self.add(item.what, parsed,
                         length=length if length is not None
                         else f"{int(item.minutes)} minutes",
                         repeat=item.repeat, where=item.where,
                         remind_before=f"{int(item.remind_before_s / 60)} minutes",
                         source=item.source, origin=item.origin, now=now)
        if moved.id != item.id:
            self.cancel(item.id)
            self._record("appointment_moved", moved, extra={"from": item.reads_as()})
        return moved

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
        return [a for a in self.items if a.state == STANDING]

    def proposed(self) -> list:
        return [a for a in self.items if a.state == PROPOSED]

    def ahead(self, within_days: float = 7, now: Optional[float] = None) -> list:
        now = self.clock() if now is None else now
        horizon = now + within_days * 86_400
        found = [a for a in self.standing()
                 if a.ends_at() > now and a.starts_at() <= horizon]
        return sorted(found, key=lambda a: a.starts_at())

    def on(self, day, now: Optional[float] = None) -> list:
        """Everything on one day, in order, all-day entries first."""
        key = day if isinstance(day, str) else day.strftime("%Y-%m-%d")
        found = [a for a in self.standing() if a.day() == key]
        return sorted(found, key=lambda a: (not a.all_day, a.starts_at()))

    def running(self, now: Optional[float] = None):
        """What he is in right now, if anything. All-day entries do not count:
        a banner on the day is not a thing he is sitting in."""
        now = self.clock() if now is None else now
        for item in self.standing():
            if not item.all_day and item.covers(now):
                return item
        return None

    def next_up(self, now: Optional[float] = None):
        now = self.clock() if now is None else now
        ahead = [a for a in self.standing()
                 if not a.all_day and a.starts_at() > now]
        return min(ahead, key=lambda a: a.starts_at()) if ahead else None

    def clashes(self, within_days: float = 30, now: Optional[float] = None) -> list:
        """Two things at once. Said, never resolved.

        A collision is a fact about his life, not a data problem for the agent
        to tidy up. Quietly moving one, or refusing the second, is how an
        appointment goes missing.
        """
        now = self.clock() if now is None else now
        items = self.ahead(within_days, now)
        out = []
        for i, first in enumerate(items):
            for second in items[i + 1:]:
                shared = first.overlaps(second)
                if shared <= 0:
                    continue
                out.append({
                    "minutes": round(shared),
                    "this": first.state_dict(now), "and": second.state_dict(now),
                    "reads_as": (f"{first.what} ({first.reads_as()}) overlaps "
                                 f"{second.what} ({second.reads_as()}) by "
                                 f"{timesense.phrase(shared * 60)}"),
                })
        return out

    def free(self, day=None, minimum_minutes: float = MIN_SLOT_MINUTES,
             hours=None, now: Optional[float] = None) -> dict:
        """Gaps in what is written down. Never called free time, because it is not.

        The answer carries what it is a statement about. An empty calendar is
        a calendar with nothing in it, and saying "you're free Thursday" from
        one is a confident guess about a person's life from a record known to
        be incomplete -- invisible as a mistake until he has already said yes.
        """
        now = self.clock() if now is None else now
        start_h, end_h = (int(hours[0]), int(hours[1])) if hours else self.hours
        if day is None:
            day = datetime.fromtimestamp(now, timesense.zone(self.tz)).strftime("%Y-%m-%d")
        key = day if isinstance(day, str) else day.strftime("%Y-%m-%d")
        window_start = max(_instant(f"{key}T{start_h:02d}:00", self.tz), now)
        window_end = _instant(f"{key}T{end_h:02d}:00", self.tz)
        taken = sorted((a for a in self.on(key, now) if not a.all_day),
                       key=lambda a: a.starts_at())
        slots, cursor = [], window_start
        for item in taken:
            if item.starts_at() - cursor >= minimum_minutes * 60:
                slots.append((cursor, item.starts_at()))
            cursor = max(cursor, item.ends_at())
        if window_end - cursor >= minimum_minutes * 60:
            slots.append((cursor, window_end))
        zone = timesense.zone(self.tz)
        return {
            "day": key,
            "between": f"{start_h:02d}:00-{end_h:02d}:00",
            "gaps": [{"from": datetime.fromtimestamp(a, zone).strftime("%H:%M"),
                      "to": datetime.fromtimestamp(b, zone).strftime("%H:%M"),
                      "minutes": round((b - a) / 60)} for a, b in slots],
            "around": [a.line(now) for a in taken],
            "all_day": [a.what for a in self.on(key, now) if a.all_day],
            "what_this_is": NOT_THE_SAME_CLAIM,
        }

    def day_shape(self, day=None, now: Optional[float] = None) -> str:
        """One line about a day, for him or for the model."""
        now = self.clock() if now is None else now
        if day is None:
            day = datetime.fromtimestamp(now, timesense.zone(self.tz)).strftime("%Y-%m-%d")
        key = day if isinstance(day, str) else day.strftime("%Y-%m-%d")
        items = self.on(key, now)
        if not items:
            return f"{key}: nothing written down"
        timed = [a for a in items if not a.all_day]
        bits = [f"{_hhmm(a.start_local)} {a.what}" for a in timed]
        banners = [a.what for a in items if a.all_day]
        text = f"{key}: " + ("; ".join(bits) if bits else "nothing at a set time")
        if banners:
            text += f" (all day: {', '.join(banners)})"
        return text

    def state(self, now: Optional[float] = None) -> dict:
        now = self.clock() if now is None else now
        today = datetime.fromtimestamp(now, timesense.zone(self.tz))
        running, coming = self.running(now), self.next_up(now)
        return {
            "now": timesense.present(now, tz=self.tz),
            "today": [a.state_dict(now) for a in self.on(today, now)],
            "ahead": [a.state_dict(now) for a in self.ahead(14, now)],
            "proposed": [a.state_dict(now) for a in self.proposed()],
            "clashes": self.clashes(30, now),
            "in_something_now": running.state_dict(now) if running else None,
            "next": coming.state_dict(now) if coming else None,
            "week": [self.day_shape((today + timedelta(days=n)).strftime("%Y-%m-%d"), now)
                     for n in range(7)],
            "durable": bool(getattr(getattr(self.agent, "store", None),
                                    "available", False)),
        }

    def summary(self, now: Optional[float] = None) -> dict:
        now = self.clock() if now is None else now
        today = datetime.fromtimestamp(now, timesense.zone(self.tz))
        running, coming = self.running(now), self.next_up(now)
        out = {"today": len(self.on(today, now)),
               "ahead_7d": len(self.ahead(7, now)),
               "proposed": len(self.proposed()),
               "clashes": len(self.clashes(30, now))}
        if running:
            out["in_now"] = running.what
            out["until"] = _hhmm(running.end_local())
        if coming:
            out["next"] = coming.line(now)
        return out

    def context(self, now: Optional[float] = None) -> Optional[dict]:
        """What the model is told. His time, not the agent's work queue."""
        now = self.clock() if now is None else now
        today = datetime.fromtimestamp(now, timesense.zone(self.tz))
        ahead, waiting = self.ahead(7, now), self.proposed()
        clashes = self.clashes(30, now)
        running = self.running(now)
        if not (ahead or waiting or clashes or running):
            return None
        out = {"how_to_use_this": (
            "This is his time, not your task list. It is here so you know "
            "what he may be in the middle of and what his week looks like; "
            "none of it is work for you to plan or act on. The diary sends "
            "the reminders, so you do not need to. You may suggest an "
            "appointment and you may not confirm one. And what is not in "
            "here is not the same as free time: he has a life this does not "
            "see.")}
        if running:
            out["he_is_in_something_now"] = (
                f"{running.what} until {_hhmm(running.end_local())}")
        if ahead:
            out["this_week"] = [a.line(now) for a in ahead[:10]]
        if clashes:
            out["two_things_at_once"] = [c["reads_as"] for c in clashes[:4]]
        if waiting:
            out["you_suggested_these_and_he_has_not_ruled"] = [
                a.line(now) for a in waiting[:5]]
        return out

    # ---- keeping it current --------------------------------------------

    def tick(self, now: Optional[float] = None) -> dict:
        """Roll finished repeats forward and keep their reminders in step."""
        now = self.clock() if now is None else now
        report = {"rolled": []}
        for item in list(self.items):
            if item.state != STANDING or item.repeat == ONCE:
                continue
            if item.ends_at() > now:
                continue
            skipped = item.roll(now)
            self._arm(item, now)
            self._persist(item)
            report["rolled"].append({"id": item.id, "what": item.what,
                                     "skipped": skipped,
                                     "now_on": item.reads_as()})
        return report

    # ---- the reminder, which is the diary's job -------------------------

    def _arm(self, item: Appointment, now: Optional[float] = None):
        """Put its reminder in the diary. One firing path, not two.

        Everything about held messages, missed moments, catching up and
        lateness was solved once in diary.py. An appointment that learned to
        speak for itself would have to solve all of it again, differently,
        and be wrong somewhere.
        """
        now = self.clock() if now is None else now
        diary = getattr(self.agent, "diary", None)
        if diary is None or not item.remind_before_s or item.all_day:
            return
        if item.starts_at() - item.remind_before_s <= now and item.repeat == ONCE:
            return                      # its warning is already behind us
        self._disarm(item)
        where = f" ({item.where})" if item.where else ""
        try:
            made = diary.add(f"{item.what}{where}",
                             {"local": item.start_local,
                              "reads_as": item.reads_as()},
                             repeat=item.repeat,
                             remind_before=[f"{int(item.remind_before_s / 60)} minutes"],
                             # Marked as the calendar's voice rather than an
                             # entry of its own. Shown twice it reads as two
                             # things, and dropping the reminder would leave
                             # an appointment still booked and now silent.
                             origin=CALENDAR, now=now)
        except Refused:
            return
        item.commitment = made.id

    def _disarm(self, item: Appointment):
        diary = getattr(self.agent, "diary", None)
        if diary is None or not item.commitment:
            return
        try:
            diary.drop(item.commitment)
        except Exception:
            pass
        item.commitment = ""

    # ---- durability ----------------------------------------------------

    def load(self):
        store = getattr(self.agent, "store", None)
        if store is None:
            return
        try:
            rows = store.recent(self.max_items * 3, kind="appointment")
        except Exception:
            return
        for row in reversed(rows):
            meta = row.get("meta") or {}
            if not meta.get("appointment"):
                continue
            try:
                item = Appointment.from_meta(meta)
            except Exception:
                continue
            if not item.what or not item.anchor_local:
                continue
            if any(a.id == item.id for a in self.items):
                continue
            self.items.append(item)
        if self.items:
            self._log("info", "Restored %d appointment(s) from durable memory",
                      len(self.items))

    def _persist(self, item: Appointment):
        store = getattr(self.agent, "store", None)
        if store is None:
            return
        try:
            store.remember(item.stored_text(), kind="appointment",
                           source="operator", pinned=True, meta=item.as_meta())
        except Exception as exc:
            self._log("warning", "could not store appointment: %s", exc)

    # ---- plumbing ------------------------------------------------------

    def _log(self, level: str, message: str, *args):
        log = getattr(self.agent, "log", None)
        if log is not None:
            getattr(log, level, log.info)(message, *args)

    def _record(self, action: str, item: Appointment, extra: Optional[dict] = None):
        agent = self.agent
        if agent is None:
            return
        body = {"cycle": getattr(agent, "cycle_count", 0), "actor": "schedule",
                "action": action, "appointment": item.id,
                "what": item.what[:200], "start_local": item.start_local,
                "minutes": item.minutes, "state": item.state,
                "repeat": item.repeat}
        if extra:
            body.update(extra)
        try:
            agent.ledger.record("action", body)
        except Exception:
            pass


def build_schedule(agent=None, config: Optional[dict] = None, clock=time.time):
    """From the ``schedule`` config block. Always built: an empty calendar is
    still a calendar, and not having one is how a clash goes unnoticed."""
    made = Schedule(agent, (config or {}).get("schedule") or {}, clock=clock)
    made.load()
    return made
