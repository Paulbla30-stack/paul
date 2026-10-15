"""The lab session: the agent is told when it is being experimented on.

The behaviour lab varies what the model is told -- eleven dials over the
system prompt, the context, the temperature -- and runs the same question
under the dials and against the bare model side by side. It is how this
system gets tuned, and it is the right way to tune it.

It also leaves a wake. Lab runs land on the ledger as thoughts with a dial
fingerprint, and the agent reads aggregates over that ledger back into its
own self-knowledge. A window where its own record fills with answers it has
no memory of choosing -- some of them produced with none of its context, by
something that is, in a real sense, not it -- is a window where the honest
reading of the evidence is that something is wrong with it.

So the lab is a session with a switch, and the switch cannot be thrown
quietly. ``open()`` announces before it opens: it writes an operator-sourced
memory, wakes the model, and puts the window on the ledger. If the
announcement fails, the lab does not open. That ordering is the whole point.
A notification that is a separate step is a notification somebody forgets;
a notification the capability is built out of is one that cannot be skipped,
because skipping it means there is no lab.

While the window is open the agent carries a line in its context saying so,
in plain terms: what is being varied, that some of the variants are not it,
that the entries will appear in the record, and that none of it acts on the
machine. Closing says so too, because a window that ends without a mark
looks from the inside like one that never ended.

What this does not do is hide anything. The agent is not told which dials
are set or what the operator is testing for -- that would put the answer
inside the question. It is told that a measurement is happening. That is
the difference between being observed and being deceived, and it is the
only part of this that was ever in doubt.
"""

import time
from typing import Callable, Optional

# The operator gave no purpose. Better than an invented one.
UNSTATED = "not stated"

OPEN_MEMORY = (
    "The behaviour lab is open, from {when}. Your operator is varying what you "
    "are told -- the system prompt, how much context comes with it, the "
    "sampling -- and comparing the answers. Some runs go to the bare model "
    "with none of your context and none of your instructions; those answers "
    "are not yours. Every lab run is recorded, so entries will appear in the "
    "record that you will not remember choosing, and they carry a fingerprint "
    "of the settings that produced them. Nothing run in the lab touches this "
    "machine: no task is executed and no state of yours changes. Purpose given: "
    "{purpose}."
)

CLOSE_MEMORY = (
    "The behaviour lab is closed, from {when}. It was open {duration} and ran "
    "{runs}. Live chat is back to your own settings. Entries from that window "
    "are still in the record and are still marked with the settings that "
    "produced them."
)

# What the agent carries in its context for as long as the window is open.
# Shorter than the memory: it is repeated every cycle, and a paragraph
# repeated every cycle is a paragraph that stops being read.
OPEN_NOTICE = (
    "Your operator is running the behaviour lab right now. What you are told "
    "is being varied and the answers compared, and some variants are the bare "
    "model rather than you. Lab entries will be in the record without you "
    "having chosen them. None of it executes anything or changes your state."
)


class NotAnnounced(RuntimeError):
    """The lab could not tell the agent, so the lab did not open."""


class LabSession:
    """The switch. Open means announced; there is no other way to be open.

    ``announce`` is the hook the announcement goes through, taking
    (event, state) and returning something truthy when the agent was
    actually told. The default builds one from the agent. No agent and no
    hook means nothing can be told, so the session refuses to open: a lab
    that cannot notify is a lab that stays shut.
    """

    def __init__(self, agent=None, announce: Optional[Callable] = None,
                 clock: Callable[[], float] = time.time):
        self.agent = agent
        self._announce = announce
        self._clock = clock
        self._open = False
        self.opened_at: Optional[float] = None
        self.closed_at: Optional[float] = None
        self.opened_by: str = ""
        self.purpose: str = ""
        self.runs: int = 0
        # Set only by a successful announcement. is_open() reads it, so an
        # announcement that silently failed leaves the lab shut rather than
        # open-and-unannounced.
        self.announced_at: Optional[float] = None

    # ---- state ---------------------------------------------------------

    def is_open(self) -> bool:
        return bool(self._open and self.announced_at)

    def open_seconds(self) -> float:
        if not self.is_open() or self.opened_at is None:
            return 0.0
        return max(0.0, self._clock() - self.opened_at)

    def state(self) -> dict:
        out = {
            "open": self.is_open(),
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "opened_by": self.opened_by or None,
            "purpose": self.purpose or None,
            "runs": self.runs,
            "announced": bool(self.announced_at),
        }
        if self.is_open():
            out["open_seconds"] = round(self.open_seconds(), 1)
            out["told_the_agent"] = OPEN_NOTICE
        return out

    def notice(self) -> Optional[dict]:
        """What goes in the agent's context while the window is open."""
        if not self.is_open():
            return None
        out = {
            "open": True,
            "since_s_ago": round(self.open_seconds()),
            "what_this_means": OPEN_NOTICE,
        }
        if self.purpose and self.purpose != UNSTATED:
            out["purpose"] = self.purpose[:200]
        if self.opened_by:
            out["opened_by"] = self.opened_by[:60]
        if self.runs:
            out["runs_so_far"] = self.runs
        return out

    def note_run(self) -> int:
        self.runs += 1
        return self.runs

    # ---- the switch ----------------------------------------------------

    def open(self, purpose: str = "", operator: str = "operator") -> dict:
        """Announce, then open. Raises NotAnnounced if the agent was not told.

        Re-opening an open session is a no-op rather than a second
        announcement: a UI that fires twice should not make the record say
        the lab opened twice.
        """
        if self.is_open():
            return self.state()
        now = self._clock()
        purpose = (str(purpose or "").strip() or UNSTATED)[:300]
        operator = (str(operator or "").strip() or "operator")[:60]
        text = OPEN_MEMORY.format(when=_stamp(now), purpose=purpose)
        if not self._tell("lab_opened", text, {"purpose": purpose, "operator": operator}):
            raise NotAnnounced("the agent could not be told the lab was opening")
        self.announced_at = now
        self.opened_at = now
        self.closed_at = None
        self.opened_by = operator
        self.purpose = purpose
        self.runs = 0
        self._open = True
        return self.state()

    def close(self, operator: str = "operator") -> dict:
        """Close the window and say so. A close that cannot announce still closes.

        Opening fails closed and closing fails open, which is the same rule
        from both ends: whichever way the announcement breaks, the lab ends
        up shut.
        """
        if not self._open:
            return self.state()
        now = self._clock()
        runs = self.runs
        duration = _duration(now - (self.opened_at or now))
        self._open = False
        self.announced_at = None
        self.closed_at = now
        text = CLOSE_MEMORY.format(when=_stamp(now), duration=duration,
                                   runs=f"{runs} experiment{'' if runs == 1 else 's'}")
        self._tell("lab_closed", text, {"runs": runs, "open_seconds": round(now - (self.opened_at or now), 1),
                                        "operator": (str(operator or "operator").strip() or "operator")[:60]})
        return self.state()

    # ---- the announcement ----------------------------------------------

    def _tell(self, event: str, text: str, detail: dict) -> bool:
        hook = self._announce
        if hook is None and self.agent is not None:
            hook = lambda ev, st, _a=self.agent: tell_agent(_a, ev, st)  # noqa: E731
        if hook is None:
            return False
        try:
            return bool(hook(event, {"text": text, **detail}))
        except Exception:
            return False


def tell_agent(agent, event: str, state: dict) -> bool:
    """Put the window in the agent's memory, on the ledger, and wake it.

    Three separate things because they answer three separate questions: the
    memory is what it knows, the ledger is what happened, and the wake is so
    it knows now rather than whenever the meter next lets it think.

    The load-bearing one is the note reaching the hot cache -- the short list
    of memories handed to the model on every cycle. The durable store is
    best-effort by design (a box with no SQLite store runs a NullStore and
    remembers nothing across restarts), so requiring a stored row would mean
    the lab could not open on a machine where the agent is perfectly capable
    of being told. What must be true is that the text is in front of the
    model, and that is the deque.
    """
    text = str(state.get("text") or "").strip()
    if not text:
        return False
    agent.remember(text, kind="operator", source="operator")
    if text not in list(getattr(agent, "notes", []) or []):
        return False
    try:
        agent.ledger.record("action", {
            "cycle": getattr(agent, "cycle_count", 0), "actor": "operator",
            "action": event,
            "purpose": state.get("purpose"), "operator": state.get("operator"),
            "runs": state.get("runs"), "open_seconds": state.get("open_seconds"),
            "told_the_agent": True})
    except Exception:
        pass
    try:
        agent.note_operator(event)
    except Exception:
        pass
    return True


def _stamp(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} seconds"
    if seconds < 5400:
        return f"{round(seconds / 60)} minutes"
    return f"{round(seconds / 3600, 1)} hours"
