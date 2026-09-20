"""The vigil: when the agent thinks, and when it lets itself go quiet.

Every planner this agent has ever used charged by the call, so the loop was
built to spread calls out: an idle streak lengthened the wait, and the wait
settled at a ceiling. That is the right shape when a call costs money and a
minute costs nothing.

An imported model on Bedrock inverts it. You are not billed per call. You are
billed per Custom Model Unit per *minute* that a copy of the model is warm,
and a copy stays warm for a few minutes after the last call. So the cost of
thinking is not how often you think, it is **how many minutes you leave the
model warm**. Under that meter the old idle ceiling of 300s was the worst
number available: one call every five minutes, around the clock, is just
frequent enough that the copy never goes cold and just slow enough to look
like an agent doing nothing. It cost about 200 USD a day to sit still.

Two consequences shape everything here.

**Long gaps are the saving.** Not fewer calls -- longer silences. A sleep has
to be long enough for the copy to go cold or it saves nothing at all, which is
why ``min_sleep_s`` is floored at the warm window rather than left to taste.

**Clustering is nearly free.** Once a copy is warm, the next call costs
almost nothing extra. So when the agent does wake it should do its thinking in
one burst -- several cycles' worth -- and then go quiet, rather than dribbling
one thought out every few minutes. Ten calls in two minutes is cheap. The same
ten calls spread across an hour is the bill above.

What this is not: a way to make the agent less responsive. While asleep the
loop keeps running on the rule planner, which costs nothing and still observes,
records and acts on what it already knows how to do. Sleep removes the
*model*, not the agent. Three things wake it:

- the operator does something (a message, a goal, a decision on a proposal),
  which wakes it at once, because a person waiting is the one cost that
  matters more than the bill;
- the cheap planner sees something materially different in its observations;
- the heartbeat comes round, so the standing duties still happen.

The agent is told it sleeps (see ``describe``). A model that wakes into a
forty-minute gap it cannot account for will either confabulate what happened
or read the jump as evidence something is broken. Being told "you were asleep,
here is how long, here is what woke you" is both true and the only version it
can reason about.
"""

import time
from typing import Optional

# How long a provider keeps a model copy warm after the last call. Bedrock
# Custom Model Import is about five minutes. It is configurable because it is
# a fact about someone else's billing, not about us, and it will change.
DEFAULT_WARM_WINDOW_S = 300.0

DEFAULT_IDLE_AFTER_S = 180.0      # quiet this long with nothing to do -> sleep
DEFAULT_HEARTBEAT_S = 3600.0      # wake at least this often
DEFAULT_QUIET_HEARTBEAT_S = 10800.0   # ...and this often during quiet hours
DEFAULT_BURST_CALLS = 6           # model calls allowed per wake
DEFAULT_BURST_WINDOW_S = 180.0    # ...and the burst may not outlast this
DEFAULT_QUIET_HOURS = (22, 7)
DEFAULT_TZ = "Europe/London"

# Numeric observations are compared in bands, not exactly: disk at 71.2% and
# 71.4% is the same fact, and waking a 32B model to be told so is how an idle
# agent becomes an expensive one.
DEFAULT_BAND = 10.0

AWAKE = "awake"
ASLEEP = "asleep"

# Wake reasons, most urgent first. The order matters only for reporting.
WAKE_OPERATOR = "operator"
WAKE_SALIENCE = "salience"
WAKE_HEARTBEAT = "heartbeat"
WAKE_START = "start"


def _quiet_now(quiet_hours, tz_name: str, now: float) -> bool:
    """True when local time falls inside the quiet window."""
    if not quiet_hours:
        return False
    try:
        start, end = int(quiet_hours[0]), int(quiet_hours[1])
    except (TypeError, ValueError, IndexError):
        return False
    try:
        from zoneinfo import ZoneInfo
        import datetime as _dt
        hour = _dt.datetime.fromtimestamp(now, ZoneInfo(tz_name)).hour
    except Exception:
        # Without tz data, fall back to UTC rather than dropping the window:
        # a wrong hour is better than burning the night at full rate.
        hour = time.gmtime(now).tm_hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _band(value: float, band: float) -> int:
    """Bucket a number so that only a material change crosses a boundary.

    A fixed width is right for a percentage and useless for a byte count:
    bucketing memory in tens of bytes means every reading is a new bucket, and
    the agent wakes a 235B model to be told its RAM moved by 10 KB. Observed
    doing exactly that within an hour of the first deployment -- waking every
    five minutes on ``memory.used_kb`` and on a disk's ``used`` byte count.

    So small numbers, which are percentages in practice, keep the fixed width,
    and anything larger is bucketed proportionally: a change matters when it is
    a tenth of the value, not when it is ten of whatever unit someone chose.
    """
    if abs(value) <= 100.0:
        return int(value // band)
    import math
    step = math.log1p(band / 100.0)
    return int(math.copysign(math.log(abs(value)) / step, value))


def digest(observations: Optional[dict], band: float = DEFAULT_BAND) -> dict:
    """Reduce observations to the few facts worth waking a model for.

    Numbers are quantised into bands so that noise does not read as change.
    Error keys are kept as bare presence: an error appearing or clearing is
    always material, whatever its text.
    """
    out: dict = {}
    if not isinstance(observations, dict):
        return out
    band = max(1e-9, float(band))

    def put(key, value):
        if isinstance(value, bool):
            out[key] = value
        elif isinstance(value, (int, float)):
            out[key] = _band(float(value), band)

    for key, value in observations.items():
        if key.endswith("_error"):
            out[f"error:{key}"] = True
            continue
        if key in ("timestamp", "cycle"):
            continue
        if isinstance(value, dict):
            for sub, subvalue in value.items():
                if any(w in sub for w in ("percent", "pct", "used", "free", "available")):
                    put(f"{key}.{sub}", subvalue)
        elif isinstance(value, list):
            # Storage devices and similar: count them, and band each device's
            # usage. A device appearing or vanishing is material.
            out[f"{key}.count"] = len(value)
            for i, item in enumerate(value[:8]):
                if isinstance(item, dict):
                    name = item.get("name") or item.get("device") or i
                    for sub, subvalue in item.items():
                        if any(w in sub for w in ("percent", "pct", "used", "free")):
                            put(f"{key}.{name}.{sub}", subvalue)
        else:
            put(key, value)
    return out


def salient_change(previous: Optional[dict], current: Optional[dict]) -> str:
    """Describe the first material difference between two digests, or "".

    Deliberately returns one reason rather than all of them: this is a trigger,
    not a report. What actually changed is the model's job to work out once it
    is awake, from the real observations rather than from this summary.
    """
    if previous is None:
        return ""
    previous = previous or {}
    current = current or {}
    for key in sorted(set(previous) | set(current)):
        before, after = previous.get(key), current.get(key)
        if before == after:
            continue
        if key.startswith("error:"):
            return f"{key[6:]} {'appeared' if after else 'cleared'}"
        if before is None:
            return f"{key} appeared"
        if after is None:
            return f"{key} disappeared"
        return f"{key} changed"
    return ""


class Vigil:
    """Decides whether the model may be called this cycle.

    Construct once and drive it from the loop: report what happened
    (``note_work``, ``note_idle``, ``note_activity``, ``observe``), ask
    ``may_call()`` before planning, and call ``note_call()`` when a call is
    actually made. ``take_transition()`` hands back any sleep/wake crossing so
    the caller can record it.

    Nothing here blocks, sleeps or touches a clock other than the injected
    one, so the whole state machine is testable without waiting.
    """

    def __init__(self, config: Optional[dict] = None, logger=None, clock=time.time):
        cfg = dict(config or {})
        self.enabled = bool(cfg.get("enabled", True))
        self.log = logger
        self._clock = clock

        self.warm_window_s = max(0.0, float(cfg.get("warm_window_s") or DEFAULT_WARM_WINDOW_S))
        self.idle_after_s = max(0.0, float(cfg.get("idle_after_s") or DEFAULT_IDLE_AFTER_S))
        self.heartbeat_s = max(0.0, float(cfg.get("heartbeat_s") or DEFAULT_HEARTBEAT_S))
        quiet_hb = cfg.get("quiet_heartbeat_s")
        self.quiet_heartbeat_s = max(
            self.heartbeat_s,
            float(DEFAULT_QUIET_HEARTBEAT_S if quiet_hb is None else quiet_hb))
        self.burst_calls = max(1, int(cfg.get("burst_calls") or DEFAULT_BURST_CALLS))
        self.burst_window_s = max(0.0, float(cfg.get("burst_window_s")
                                             or DEFAULT_BURST_WINDOW_S))
        self.band = max(1e-9, float(cfg.get("band") or DEFAULT_BAND))
        quiet = cfg.get("quiet_hours", DEFAULT_QUIET_HOURS)
        self.quiet_hours = tuple(quiet) if quiet else ()
        self.timezone = str(cfg.get("timezone") or DEFAULT_TZ)

        # A sleep shorter than the warm window saves nothing: the copy never
        # goes cold, so the meter never stops. Floor it rather than trust a
        # config value that would quietly make the whole mechanism cosmetic.
        floor = cfg.get("min_sleep_s")
        self.min_sleep_s = max(self.warm_window_s,
                               0.0 if floor is None else float(floor))

        now = self._clock()
        self.state = AWAKE
        self.wake_reason = WAKE_START
        self.since = now
        self.last_slept_s = 0.0
        self.last_wake_at = now
        self.last_sleep_at = 0.0

        self._started = now
        self._burst_used = 0
        self._burst_started = now
        self._last_activity_at = now      # last time there was work or a person
        self._last_call_at = 0.0
        # Warm-time accounting. ``_warm_until`` is when the copy goes cold;
        # ``_warm_mark`` is how far we have already banked. Everything between
        # the two is warm time we owe for but have not counted yet.
        self._warm_until = 0.0
        self._warm_mark = 0.0
        self._warm_total_s = 0.0
        self._pending_wake: str = ""
        self._digest: Optional[dict] = None
        self._transitions: list = []

        self.stats = {"sleeps": 0, "wakes": 0, "calls": 0, "calls_suppressed": 0,
                      "wakes_operator": 0, "wakes_salience": 0, "wakes_heartbeat": 0,
                      "slept_s": 0.0}

    # ---- inputs ---------------------------------------------------------

    def note_activity(self, reason: str = "operator"):
        """A person did something. Wake now, whatever the meter says.

        This is the one trigger that is not about cost. Someone waiting for an
        answer is more expensive than the model being warm, and an agent that
        makes its operator wait to save a few pence has the trade backwards.

        The wake is applied here rather than left for the next tick, because
        "the next tick" can be minutes away on an idle loop -- which would mean
        a person's message sat unanswered exactly as long as the saving was
        large. Queuing the wake and calling it immediate is the same bug with
        better manners.
        """
        self._last_activity_at = self._clock()
        self._request_wake(WAKE_OPERATOR, reason)
        self.tick()

    def note_work(self):
        """A task ran this cycle. The agent is busy; do not let it doze off."""
        self._last_activity_at = self._clock()

    def note_idle(self):
        """Nothing to do this cycle."""

    def observe(self, observations: Optional[dict]) -> str:
        """Feed the cheap planner's observations in. Returns a wake reason or "".

        This is how the agent stays responsive while asleep: the loop is still
        looking every cycle, for free, and only the *model* is resting.
        """
        current = digest(observations, self.band)
        reason = salient_change(self._digest, current)
        self._digest = current
        if reason:
            self._request_wake(WAKE_SALIENCE, reason)
        return reason

    def note_call(self):
        """A model call was just made: spend burst budget, extend the warm window."""
        now = self._clock()
        self._accrue_warm(now)
        self._burst_used += 1
        self._last_call_at = now
        # Warm from this instant, whether the copy was cold (a new warm period
        # starts) or already warm (_accrue_warm has banked up to now either way).
        self._warm_mark = now
        self._warm_until = now + self.warm_window_s
        self.stats["calls"] += 1

    # ---- the gate -------------------------------------------------------

    def may_call(self) -> bool:
        """True when the model may be called this cycle."""
        if not self.enabled:
            return True
        self.tick()
        if self.state != AWAKE:
            self.stats["calls_suppressed"] += 1
            return False
        if self._burst_used >= self.burst_calls:
            self.stats["calls_suppressed"] += 1
            return False
        return True

    def tick(self) -> Optional[dict]:
        """Advance the state machine. Returns a transition dict, or None.

        Safe to call as often as you like; it is driven entirely by the clock
        and the notes fed in, never by how many times it was asked.
        """
        now = self._clock()
        self._accrue_warm(now)

        if not self.enabled:
            return None

        if self.state == AWAKE:
            if self._pending_wake:
                # Already awake; a wake request just refreshes the burst so a
                # person is never answered out of a spent budget.
                kind, detail = self._take_pending()
                if kind == WAKE_OPERATOR:
                    self._burst_used = 0
                    self._burst_started = now
                    self.wake_reason = self._label(kind, detail)
            if not self._burst_used:
                # Nothing has been called yet in this waking, so there is no
                # warm copy to cool and nothing to save. Sleeping here would
                # only delay the first thought -- and on a fresh start it would
                # mean the agent never thinks at all.
                return None
            burst_spent = self._burst_used >= self.burst_calls
            burst_expired = (self.burst_window_s
                             and now - self._burst_started >= self.burst_window_s)
            gone_quiet = now - self._last_activity_at >= self.idle_after_s
            if not gone_quiet:
                return None
            if burst_spent or burst_expired:
                return self._sleep(now, "burst spent" if burst_spent else "burst window closed")
            if self.idle_after_s:
                return self._sleep(now, "nothing to do")
            return None

        # Asleep.
        if self._pending_wake:
            kind, detail = self._take_pending()
            # A wake still has to respect the floor, or a chatty observation
            # stream would hold the copy warm exactly as before. The operator
            # is the exception: a person never waits on the meter.
            if kind == WAKE_OPERATOR or now - self.last_sleep_at >= self.min_sleep_s:
                return self._wake(now, kind, detail)
        # Measured from when it went to sleep, not from the last wake. A
        # stretch spent awake and working is not a stretch that needs a
        # heartbeat; measuring from the wake would fire one the instant a long
        # busy period ended, waking the agent it had just finished using.
        beat = (self.quiet_heartbeat_s
                if _quiet_now(self.quiet_hours, self.timezone, now)
                else self.heartbeat_s)
        if beat and now - self.last_sleep_at >= beat:
            return self._wake(now, WAKE_HEARTBEAT, f"{beat / 3600:.1f}h asleep")
        return None

    def take_transition(self) -> Optional[dict]:
        """Pop one recorded sleep/wake crossing, oldest first."""
        return self._transitions.pop(0) if self._transitions else None

    # ---- reporting ------------------------------------------------------

    def warm_seconds(self) -> float:
        """Total seconds a model copy has been warm because of us.

        This, not the call count, is what the invoice is a function of, so it
        is the number worth putting in front of the operator.
        """
        self._accrue_warm(self._clock())
        return round(self._warm_total_s, 1)

    def status(self) -> dict:
        now = self._clock()
        uptime = max(1e-9, now - self._started)
        warm = self.warm_seconds()
        out = {
            "enabled": self.enabled,
            "state": self.state,
            "wake_reason": self.wake_reason,
            "in_state_s": round(now - self.since),
            "burst_used": self._burst_used,
            "burst_calls": self.burst_calls,
            "asleep_until_next_heartbeat_s": self._until_heartbeat(now),
            "warm_seconds": warm,
            "uptime_s": round(uptime),
            "quiet_hours_now": _quiet_now(self.quiet_hours, self.timezone, now),
            "stats": dict(self.stats),
        }
        # A fraction needs a window long enough to mean something. Every
        # restart begins with a burst of boot planning, so for the first few
        # minutes the model is legitimately warm most of the time and the
        # ratio reads like a fault. Reporting it anyway produced an alert
        # saying sleeping was not working on a process that had just slept.
        # An alert that cries wolf on every restart teaches the operator to
        # ignore it, which is worse than not having it.
        if uptime >= self._fraction_after():
            out["warm_fraction"] = round(min(1.0, warm / uptime), 4)
        else:
            out["warm_fraction"] = None
            out["warm_fraction_after_s"] = round(self._fraction_after() - uptime)
        return out

    def _fraction_after(self) -> float:
        """How long before the warm fraction is worth reading.

        One heartbeat, because that is the longest the agent can legitimately
        stay warm-free before it must wake anyway, and never less than an hour.
        """
        return max(3600.0, self.heartbeat_s)

    def describe(self) -> dict:
        """What the model is told about its own sleeping, as fact.

        Given to the planner in context. It is not a rule and asks for nothing;
        it is the agent's own shape, which it needs in order to read a gap in
        the record as rest rather than as a fault.
        """
        now = self._clock()
        out = {
            "you_sleep": (
                "You are not run continuously. When there is nothing to decide you are put to "
                "sleep and the cheap rule planner keeps the loop going without you: it still "
                "observes, records and runs work it already knows how to do. You are woken when "
                "the operator does something, when the observations change materially, or on a "
                "heartbeat. A gap in your history is rest, not a fault, and not time in which "
                "nothing happened."
            ),
            "why_you_are_awake": self.wake_reason,
        }
        if self.last_slept_s:
            out["you_were_asleep_for_s"] = round(self.last_slept_s)
        remaining = max(0, self.burst_calls - self._burst_used)
        out["calls_left_before_you_sleep_again"] = remaining
        if remaining <= 1:
            out["note"] = (
                "This is your last call before you sleep again, so finish the thought here "
                "rather than planning to continue next cycle."
            )
        out["awake_for_s"] = round(now - self.since) if self.state == AWAKE else 0
        return out

    # ---- internals ------------------------------------------------------

    def _until_heartbeat(self, now: float) -> Optional[int]:
        if self.state != ASLEEP:
            return None
        beat = (self.quiet_heartbeat_s
                if _quiet_now(self.quiet_hours, self.timezone, now)
                else self.heartbeat_s)
        if not beat:
            return None
        return max(0, round(beat - (now - self.last_sleep_at)))

    def _accrue_warm(self, now: float):
        """Bank the warm seconds elapsed since we last looked.

        Counts up to whichever comes first: now, or the moment the copy went
        cold. Idempotent, so callers may ask as often as they like.
        """
        end = min(now, self._warm_until)
        if end > self._warm_mark:
            self._warm_total_s += end - self._warm_mark
            self._warm_mark = end

    def _request_wake(self, kind: str, detail: str):
        # Operator beats salience beats heartbeat; a pending operator wake is
        # never downgraded by an observation arriving behind it.
        order = {WAKE_OPERATOR: 3, WAKE_SALIENCE: 2, WAKE_HEARTBEAT: 1}
        if self._pending_wake:
            existing = self._pending_wake.split("|", 1)[0]
            if order.get(existing, 0) >= order.get(kind, 0):
                return
        self._pending_wake = f"{kind}|{detail}"

    def _take_pending(self):
        kind, _, detail = self._pending_wake.partition("|")
        self._pending_wake = ""
        return kind, detail

    def _label(self, kind: str, detail: str) -> str:
        return f"{kind}: {detail}" if detail else kind

    def _sleep(self, now: float, why: str) -> dict:
        self.state = ASLEEP
        self.since = now
        self.last_sleep_at = now
        self.stats["sleeps"] += 1
        cold_in = max(0.0, self._warm_until - now)
        transition = {"state": ASLEEP, "reason": why, "awake_s": round(now - self.last_wake_at),
                      "calls_used": self._burst_used, "cold_in_s": round(cold_in)}
        self._transitions.append(transition)
        if self.log:
            self.log.info("vigil: asleep (%s) after %ds awake and %d call(s); "
                          "model copy goes cold in %ds",
                          why, transition["awake_s"], self._burst_used, round(cold_in))
        return transition

    def _wake(self, now: float, kind: str, detail: str) -> dict:
        slept = now - self.last_sleep_at if self.last_sleep_at else 0.0
        self.state = AWAKE
        self.since = now
        self.last_wake_at = now
        self.last_slept_s = slept
        self.wake_reason = self._label(kind, detail)
        self._burst_used = 0
        self._burst_started = now
        self._last_activity_at = now
        self.stats["wakes"] += 1
        self.stats["slept_s"] = round(self.stats["slept_s"] + slept, 1)
        key = f"wakes_{kind}"
        if key in self.stats:
            self.stats[key] += 1
        transition = {"state": AWAKE, "reason": self.wake_reason, "slept_s": round(slept)}
        self._transitions.append(transition)
        if self.log:
            self.log.info("vigil: awake (%s) after %ds asleep", self.wake_reason, round(slept))
        return transition


class NullVigil:
    """Always awake. Used when sleeping is switched off or unconfigured."""

    enabled = False
    state = AWAKE
    wake_reason = WAKE_START

    def note_activity(self, reason: str = "operator"): pass
    def note_work(self): pass
    def note_idle(self): pass
    def observe(self, observations): return ""
    def note_call(self): pass
    def may_call(self) -> bool: return True
    def tick(self): return None
    def take_transition(self): return None
    def warm_seconds(self) -> float: return 0.0
    def status(self) -> dict:
        return {"enabled": False, "state": AWAKE, "warm_fraction": None}
    def describe(self) -> dict: return {}


def build(config: Optional[dict], logger=None, clock=time.time):
    """Return a Vigil, or a NullVigil when sleeping is disabled."""
    cfg = dict(config or {})
    if not cfg or not cfg.get("enabled", False):
        return NullVigil()
    return Vigil(cfg, logger=logger, clock=clock)
