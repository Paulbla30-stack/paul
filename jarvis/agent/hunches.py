"""Something is wrong and the readings say it is fine.

The operator was asked what he would do if a carer said a resident did not
look right, while every observation on the chart was normal. He said he would
trust the instinct and monitor. That is the correct clinical answer and it is
the whole design of this module, because it contains the two halves that
matter and keeps them apart:

    **trust it**     -- the carer has a baseline on that person that the
                        chart does not carry, and a normal set of obs is not
                        the same as a well resident
    **and monitor**  -- the instinct does not authorise treatment. It
                        authorises looking more often

A gut feeling is a real signal and it is also the single easiest thing in this
system to manufacture. An agent permitted to have feelings about things will
produce them, because a feeling costs nothing to assert and cannot be checked
at the moment it is asserted. Everything else here is grounded in a reading; a
hunch is by definition not. So it is allowed, under four conditions, and the
fourth is the one doing the work.

**It must name what it is contradicting.** ``despite`` is required. A hunch
with no contrary evidence is not a hunch, it is an observation, and it should
be said as one. The carer's report is only interesting *because* the obs are
normal -- that is what makes it information rather than agreement.

**It must be falsifiable.** ``expect`` is required: what would be seen if it
is right, and by when. "Something feels off" with no test attached is not a
hunch, it is a mood, and a register full of moods is a horoscope.

**It must carry a number.** A confidence is stated when the hunch is filed,
between nought and one exclusive -- certainty at either end is a claim or a
silence, and neither is this. The number is what makes the register scorable.

**And it authorises watching, never acting.** This is the fourth and it is the
reason the other three are safe. A hunch raises the rate of observation on its
subject and does nothing else. It cannot start a task, change a file, or send
anything. The most an instinct has ever been allowed to do in a hospital is
bring someone back to the bedside sooner, and that is what it does here.

What comes out is a calibration. Of the hunches that resolved, how many were
borne out, bucketed by the confidence stated at the time. That is how an
instinct earns the right to be listened to -- not by being respected, but by
being counted. An agent whose hunches land two times in ten should be told so,
and so should one whose hunches land eight times in ten.
"""

import hashlib
import time
from dataclasses import dataclass, field
from typing import Optional

WATCHING, BORNE_OUT, PASSED, WITHDRAWN = "watching", "borne out", "passed", "withdrawn"
STATES = (WATCHING, BORNE_OUT, PASSED, WITHDRAWN)

MAX_OPEN = 5              # an agent with fifteen hunches has none
MAX_TEXT = 300
MIN_CONFIDENCE = 0.05     # below this it is not worth anyone's attention
MAX_CONFIDENCE = 0.95     # above it, it is a claim; make the claim
DEFAULT_WINDOW_S = 7 * 86_400
MAX_WINDOW_S = 90 * 86_400

# The confidence bands the calibration is reported in. Coarse on purpose: a
# register of forty hunches cannot support ten buckets, and a calibration
# curve drawn from four observations is a decoration.
BANDS = ((0.0, 0.35, "low"), (0.35, 0.65, "even"), (0.65, 1.0, "high"))


class Refused(ValueError):
    """Not filed, and the reason says what would make it a hunch."""


@dataclass
class Hunch:
    about: str                      # the subject: a path, a system, a situation
    feeling: str                    # what is off
    despite: str                    # what the evidence says, which it contradicts
    expect: str                     # what would be seen if it is right
    confidence: float
    by: float                       # when it should have shown itself
    state: str = WATCHING
    raised_at: float = field(default_factory=time.time)
    resolved_at: Optional[float] = None
    outcome: str = ""
    looks: int = 0                  # times the subject was looked at because of it

    @property
    def id(self) -> str:
        seed = f"{self.about}|{self.feeling}|{self.raised_at}"
        return hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()[:10]

    @property
    def band(self) -> str:
        for low, high, name in BANDS:
            if low <= self.confidence < high:
                return name
        return "high"

    def overdue(self, now: float) -> bool:
        return self.state == WATCHING and now >= self.by

    def line(self, now: Optional[float] = None) -> str:
        from jarvis.agent import timesense
        now = time.time() if now is None else now
        return (f"{self.about}: {self.feeling} -- despite {self.despite}. "
                f"Confidence {self.confidence:.0%}. Would show as: {self.expect}, "
                f"by {timesense.gap(self.by, now)}.")

    def state_dict(self, now: Optional[float] = None) -> dict:
        from jarvis.agent import timesense
        now = time.time() if now is None else now
        out = {"id": self.id, "about": self.about, "feeling": self.feeling,
               "despite": self.despite, "expect": self.expect,
               "confidence": self.confidence, "band": self.band,
               "state": self.state, "looks": self.looks,
               "by": timesense.gap(self.by, now),
               "raised": timesense.gap(self.raised_at, now),
               "authorises": "watching this more closely, and nothing else"}
        if self.outcome:
            out["outcome"] = self.outcome
        return out

    def as_meta(self) -> dict:
        return {"hunch": True, "never_consolidate": True,
                "about": self.about, "feeling": self.feeling,
                "despite": self.despite, "expect": self.expect,
                "confidence": self.confidence, "by": self.by,
                "state": self.state, "raised_at": self.raised_at,
                "resolved_at": self.resolved_at, "outcome": self.outcome,
                "looks": self.looks}

    @classmethod
    def from_meta(cls, meta: dict):
        return cls(about=str(meta.get("about") or ""),
                   feeling=str(meta.get("feeling") or ""),
                   despite=str(meta.get("despite") or ""),
                   expect=str(meta.get("expect") or ""),
                   confidence=float(meta.get("confidence") or 0.5),
                   by=float(meta.get("by") or 0.0),
                   state=str(meta.get("state") or WATCHING),
                   raised_at=float(meta.get("raised_at") or time.time()),
                   resolved_at=meta.get("resolved_at"),
                   outcome=str(meta.get("outcome") or ""),
                   looks=int(meta.get("looks") or 0))


class Hunches:
    """The register. Files them, watches them, and counts how they landed."""

    def __init__(self, agent=None, config: Optional[dict] = None,
                 clock=time.time):
        cfg = dict(config or {})
        self.agent = agent
        self.clock = clock
        self.max_open = int(cfg.get("max_open") or MAX_OPEN)
        self.items: list = []

    # ---- filing ---------------------------------------------------------

    def raise_one(self, about: str, feeling: str, despite: str, expect: str,
                  confidence: float, within: Optional[float] = None,
                  now: Optional[float] = None) -> Hunch:
        """File a hunch, under the four conditions. Raises Refused otherwise."""
        now = self.clock() if now is None else now
        about = " ".join(str(about or "").split())[:MAX_TEXT]
        feeling = " ".join(str(feeling or "").split())[:MAX_TEXT]
        despite = " ".join(str(despite or "").split())[:MAX_TEXT]
        expect = " ".join(str(expect or "").split())[:MAX_TEXT]
        if not about or not feeling:
            raise Refused("a hunch needs a subject and what is off about it")
        if not despite:
            raise Refused(
                "say what this contradicts. A hunch with no contrary evidence "
                "is an observation -- if the readings agree with you, this is "
                "not a hunch and it should be said plainly as a finding")
        if not expect:
            raise Refused(
                "say what you would see if you are right, and it has to be "
                "something that could fail to appear. Without that this is a "
                "mood, and a register of moods is a horoscope")
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            raise Refused("a hunch carries a number: how likely, between 0 and 1")
        if not MIN_CONFIDENCE <= confidence <= MAX_CONFIDENCE:
            raise Refused(
                f"confidence must be between {MIN_CONFIDENCE} and "
                f"{MAX_CONFIDENCE}. Below that it is not worth raising; above "
                "it you are not having a hunch, you are making a claim, and a "
                "claim goes through the ordinary route where it can be checked")
        # `within or DEFAULT` would turn a deliberate 0 into a seven-day
        # window without a word. A window of nothing is a refusal, not a
        # default.
        window = float(DEFAULT_WINDOW_S if within is None else within)
        if not 0 < window <= MAX_WINDOW_S:
            raise Refused("give it a window it could plausibly resolve inside; "
                          "ninety days is the longest this holds")
        open_now = self.watching()
        if len(open_now) >= self.max_open:
            raise Refused(
                f"{len(open_now)} are already open and {self.max_open} is the "
                "limit. An agent with fifteen hunches has none -- resolve or "
                "withdraw one first")
        for existing in open_now:
            if existing.about.lower() == about.lower():
                raise Refused(f"there is already one open about {about}: "
                              f"{existing.feeling}. Add to it or let it resolve")
        item = Hunch(about=about, feeling=feeling, despite=despite,
                     expect=expect, confidence=confidence,
                     by=now + window, raised_at=now)
        self.items.append(item)
        self._persist(item)
        self._record("hunch_raised", item)
        self._log("info", "Hunch raised about %s (%.0f%%): %s",
                  about, confidence * 100, feeling)
        return item

    # ---- resolving ------------------------------------------------------

    def resolve(self, ident: str, borne_out: bool, outcome: str = "",
                now: Optional[float] = None) -> Optional[Hunch]:
        """It showed itself, or it did not. Both are worth the same."""
        item = self.find(ident)
        if item is None or item.state != WATCHING:
            return None
        now = self.clock() if now is None else now
        item.state = BORNE_OUT if borne_out else PASSED
        item.resolved_at = now
        item.outcome = " ".join(str(outcome or "").split())[:MAX_TEXT]
        self._persist(item)
        self._record("hunch_resolved", item, extra={"borne_out": borne_out})
        return item

    def withdraw(self, ident: str) -> bool:
        item = self.find(ident)
        if item is None or item.state != WATCHING:
            return False
        item.state = WITHDRAWN
        item.resolved_at = self.clock()
        self._persist(item)
        self._record("hunch_withdrawn", item)
        return True

    def tick(self, now: Optional[float] = None) -> dict:
        """Anything past its window without showing itself has passed.

        Not quietly. A hunch that expires unresolved is the most useful entry
        in the register, because it is the one the calibration is built from.
        """
        now = self.clock() if now is None else now
        lapsed = []
        for item in self.watching():
            if item.overdue(now):
                item.state = PASSED
                item.resolved_at = now
                item.outcome = "the window closed and it never showed itself"
                self._persist(item)
                self._record("hunch_lapsed", item)
                lapsed.append({"id": item.id, "about": item.about,
                               "confidence": item.confidence})
        return {"passed": lapsed}

    def looked(self, about: str) -> int:
        """Record that the subject was looked at because of an open hunch.

        The only thing a hunch is allowed to cause.
        """
        count = 0
        for item in self.watching():
            if item.about.lower() in str(about or "").lower() or \
                    str(about or "").lower() in item.about.lower():
                item.looks += 1
                self._persist(item)
                count += 1
        return count

    # ---- reading --------------------------------------------------------

    def find(self, ident: str):
        ident = str(ident or "").strip()
        for item in self.items:
            if item.id == ident:
                return item
        low = ident.lower()
        for item in self.items:
            if low and low in item.about.lower():
                return item
        return None

    def watching(self) -> list:
        return [h for h in self.items if h.state == WATCHING]

    def resolved(self) -> list:
        return [h for h in self.items if h.state in (BORNE_OUT, PASSED)]

    def watch_list(self) -> list:
        """Subjects to look at more often, because something was raised."""
        return sorted({h.about for h in self.watching()})

    def calibration(self) -> dict:
        """How its instincts have actually landed, by the number it stated.

        The point of the register. An instinct earns the right to be listened
        to by being counted, not by being respected.
        """
        done = self.resolved()
        out = {"resolved": len(done), "borne_out": 0, "bands": {}}
        if not done:
            out["reads"] = ("nothing has resolved yet. A calibration drawn "
                            "from no observations is a decoration")
            return out
        out["borne_out"] = sum(1 for h in done if h.state == BORNE_OUT)
        for _, _, name in BANDS:
            band = [h for h in done if h.band == name]
            if not band:
                continue
            hit = sum(1 for h in band if h.state == BORNE_OUT)
            stated = sum(h.confidence for h in band) / len(band)
            out["bands"][name] = {
                "n": len(band), "borne_out": hit,
                "stated": round(stated, 2),
                "actual": round(hit / len(band), 2),
                "over_confident_by": round(stated - hit / len(band), 2)}
        out["rate"] = round(out["borne_out"] / len(done), 2)
        if len(done) < 8:
            out["reads"] = (f"{len(done)} resolved, which is too few to read "
                            "as a rate. Reported so it is not mistaken for one")
        return out

    def state(self, now: Optional[float] = None) -> dict:
        now = self.clock() if now is None else now
        return {"watching": [h.state_dict(now) for h in self.watching()],
                "recent": [h.state_dict(now) for h in self.resolved()[-8:]],
                "calibration": self.calibration(),
                "watch_list": self.watch_list(),
                "what_a_hunch_can_do": (
                    "raise the rate of observation on its subject. It cannot "
                    "start a task, change anything or send anything. The most "
                    "an instinct has ever been allowed to do is bring someone "
                    "back to the bedside sooner")}

    def summary(self, now: Optional[float] = None) -> dict:
        cal = self.calibration()
        return {"watching": len(self.watching()), "resolved": cal["resolved"],
                "borne_out": cal["borne_out"], "rate": cal.get("rate"),
                "watch_list": self.watch_list()}

    def context(self, now: Optional[float] = None) -> Optional[dict]:
        """What the model is told: the rules, and how its own have landed."""
        now = self.clock() if now is None else now
        cal = self.calibration()
        open_now = self.watching()
        out = {"what_this_is": (
            "You may say that something is wrong when the readings say it is "
            "fine. That is a real signal -- a normal set of observations is "
            "not the same as a well patient, and the person at the bedside "
            "knows things the chart does not carry."),
            "the_four_conditions": [
                "name what it contradicts. If the readings agree with you it "
                "is not a hunch, it is a finding, and you should say it as one",
                "say what you would see if you are right, and by when. It has "
                "to be something that could fail to appear",
                "state how likely, between 0.05 and 0.95. Certainty at either "
                "end is a claim or a silence",
                "it authorises watching and nothing else. It cannot start a "
                "task, change a file or send anything"],
            "why_it_is_counted": (
                "Every one resolves, including the ones that quietly never "
                "happen. An instinct earns the right to be listened to by "
                "being counted, not by being respected.")}
        if open_now:
            out["you_are_watching"] = [h.line(now) for h in open_now]
        if cal["resolved"]:
            out["how_yours_have_landed"] = cal
        return out

    # ---- durability -----------------------------------------------------

    def load(self):
        store = getattr(self.agent, "store", None)
        if store is None:
            return
        try:
            rows = store.recent(120, kind="hunch")
        except Exception:
            return
        for row in reversed(rows):
            meta = row.get("meta") or {}
            if not meta.get("hunch"):
                continue
            try:
                item = Hunch.from_meta(meta)
            except Exception:
                continue
            if not item.about or any(h.id == item.id for h in self.items):
                continue
            self.items.append(item)

    def _persist(self, item: Hunch):
        store = getattr(self.agent, "store", None)
        if store is None:
            return
        try:
            store.remember(f"Hunch about {item.about}: {item.feeling} "
                           f"(raised {int(item.raised_at)})",
                           kind="hunch", source="brain", pinned=True,
                           meta=item.as_meta())
        except Exception:
            pass

    def _log(self, level: str, message: str, *args):
        log = getattr(self.agent, "log", None)
        if log is not None:
            getattr(log, level, log.info)(message, *args)

    def _record(self, action: str, item: Hunch, extra: Optional[dict] = None):
        agent = self.agent
        if agent is None:
            return
        body = {"cycle": getattr(agent, "cycle_count", 0), "actor": "agent",
                "action": action, "hunch": item.id, "about": item.about[:200],
                "confidence": item.confidence, "state": item.state}
        if extra:
            body.update(extra)
        try:
            agent.ledger.record("action", body)
        except Exception:
            pass


def build_hunches(agent=None, config: Optional[dict] = None, clock=time.time):
    made = Hunches(agent, (config or {}).get("hunches") or {}, clock=clock)
    made.load()
    return made
