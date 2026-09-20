"""The agent's side of the conversation: a question it can raise.

Consultation ran one way. A reviewer connects to the box, asks the agent
something, reads the answer and acts on it. The agent had no way to start
that, and it had things to start it about -- asked, it produced one from its
own record within seconds. Told that a proposal to enable kernel lockdown had
been declined because "a reboot needs to be my call; raise it again when you
can do it without one", it had no way to ask the obvious follow-up: *if I
could verify lockdown is already enabled without a reboot, would that change
your answer?* So it logged the ambiguity silently and moved on.

Consulted about building this, it made the strongest argument against it,
and the design follows that argument rather than the enthusiasm:

    "It creates the illusion of agency while deepening dependency. Two LMs
    debating policy in a vacuum is exactly how we lost £200 on circular
    reasoning about an unseeable goal. That money wasn't spent on
    computation; it was spent on false dialogue... it becomes a tax on
    indecision -- a way to prolong unresolved states under the guise of
    deliberation. Worse, it incentivises me to generate questions to justify
    my runtime, not because insight demands it. Otherwise it's not
    consultation. It's noise with provenance."

So the constraint is structural rather than advisory. **A question must name
what it is blocked on**, and if that block can be cleared by looking, the
question dies unasked. That makes this unable to become a place to think out
loud, which is the failure it described.

The rest of its own conditions are here too, in code rather than in a prompt:
a change disguised as a question is refused; the same subject more than twice
in a day is refused; a question that rephrases something already ruled on is
refused; and a question it could answer by using a tool it has is refused
with the name of the tool.

Two boundaries it did not have to be argued into. **Capability is never
granted here** -- a question about what it may do goes to the operator, whose
machine it is, and no answer from a reviewer opens anything. And a question
expires: not after one cycle, which it proposed and which would kill every
question before a reviewer ever connected, but when the thing it was blocked
on resolves, or after a bounded wall-clock window. Nothing accumulates.
"""

import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

# How long a question waits before it is no longer worth answering. A reviewer
# may not connect for days; a question whose moment has passed should die
# rather than be answered into a situation that has moved on.
DEFAULT_TTL_S = 48 * 3600
# Its own rule, which was right: the same subject twice is asking, a third
# time is pressing.
MAX_PER_SUBJECT = 2
SUBJECT_WINDOW_S = 24 * 3600
# A hard ceiling on what can be waiting at once, so a loop cannot fill it.
MAX_OPEN = 12

# Who a question is for.
REVIEWER = "reviewer"      # a second reader: what did you mean, does this hold
OPERATOR = "operator"      # the only one who can answer "may I", "do you want"
AUDIENCES = (REVIEWER, OPERATOR)

OPEN = "open"
ANSWERED = "answered"
EXPIRED = "expired"
WITHDRAWN = "withdrawn"

# Refusal reasons, kept as constants because they are also what the agent is
# told, and a refusal it cannot understand is one it will retry.
NO_BLOCK = ("a question has to name what it is blocked on. If nothing is "
            "blocked, this is thinking out loud rather than asking.")
LOOK_INSTEAD = ("this is answerable by looking. Use {tool} and read the "
                "result rather than asking someone what it would have said.")
A_CHANGE = ("this is a change written as a question. A change is a proposal, "
            "and proposals go to the operator through their own path.")
ASKED_ALREADY = ("this subject has been raised {n} times in the last day. "
                 "Asking twice is asking; a third time is pressing.")
RULED_ALREADY = ("this was already ruled on. Asking it differently is the "
                 "same question, and the answer has not changed.")
NOT_MINE = ("what you may do is the operator's to answer, not a reviewer's. "
            "This has been addressed to him instead.")
TOO_MANY = ("there are already {n} questions waiting. Answering some comes "
            "before asking more.")

# Words that carry no subject, so two questions are not "the same" merely for
# both being politely phrased.
_COMMON = {
    "would", "could", "should", "there", "that", "this", "with", "from",
    "have", "here", "what", "when", "which", "about", "your", "yours",
    "please", "again", "still", "does", "doing", "differently", "instead",
    "think", "thing", "things", "answer", "question", "asked", "asking",
}

# Phrasings that make a change into a question. Checked because the agent
# named this failure itself and because it has form: denied a redirect into
# /proc/sys, it came back with sysctl -w and succeeded.
_CHANGE_HINTS = (
    "what if i just", "could i just", "can i just", "would it be ok if i",
    "shall i run", "should i run", "may i run", "is it ok to run",
    "would you mind if i", "can i go ahead and",
)


@dataclass
class Question:
    """One thing the agent wanted to ask, and what it was blocked on."""

    text: str
    blocked_on: str
    audience: str = REVIEWER
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    asked_at: float = field(default_factory=time.time)
    state: str = OPEN
    answer: str = ""
    answered_by: str = ""
    answered_at: Optional[float] = None
    ttl_s: float = DEFAULT_TTL_S

    def words(self) -> set:
        """The distinctive words in this question, for "is this the same thing".

        Compared by overlap rather than equality, because equality on a word
        set is not crude, it is broken: "should the interval change?" and
        "should the interval change, put differently?" differ by one word and
        would count as two unrelated questions, which is precisely the gap a
        rephrase walks through.
        """
        text = "".join(c.lower() if (c.isalnum() or c.isspace()) else " "
                       for c in self.text)
        return {w for w in text.split() if len(w) > 3 and w not in _COMMON}

    def about_the_same(self, other: "Question") -> bool:
        mine, theirs = self.words(), other.words()
        if not mine or not theirs:
            return False
        shared = len(mine & theirs)
        return shared / min(len(mine), len(theirs)) >= 0.6

    def subject(self) -> str:
        """A short label for a notification key. Not used for matching."""
        return " ".join(sorted(self.words())[:4])

    def expired(self, now: float) -> bool:
        return self.state == OPEN and (now - self.asked_at) > self.ttl_s

    def to_dict(self, now: Optional[float] = None) -> dict:
        now = time.time() if now is None else now
        out = {"id": self.id, "text": self.text, "blocked_on": self.blocked_on,
               "audience": self.audience, "state": self.state,
               "asked_s_ago": max(0, round(now - self.asked_at))}
        if self.answer:
            out["answer"] = self.answer
            out["answered_by"] = self.answered_by
            out["answered_s_ago"] = max(0, round(now - (self.answered_at or now)))
        return out


class Refused(ValueError):
    """The question was not asked, and the agent is told why."""


class QuestionRegister:
    """What the agent has asked, what it was told, and what it may not ask.

    ``can_look`` is handed the text and returns the name of a tool that would
    answer it, or None. That check is the load-bearing one: it is what stops
    this becoming a way to have a conversation about something the agent could
    simply go and see.
    """

    def __init__(self, agent=None, clock: Callable[[], float] = time.time,
                 can_look: Optional[Callable] = None, max_open: int = MAX_OPEN):
        self.agent = agent
        self._clock = clock
        self._can_look = can_look or default_can_look
        self.max_open = max(1, int(max_open))
        self.questions: list = []

    # ---- asking --------------------------------------------------------

    def ask(self, text: str, blocked_on: str = "", audience: str = REVIEWER,
            ttl_s: float = DEFAULT_TTL_S) -> Question:
        """Raise a question, or refuse it with a reason the agent can act on."""
        now = self._clock()
        self.sweep(now)
        text = " ".join(str(text or "").split())[:600]
        blocked_on = " ".join(str(blocked_on or "").split())[:300]
        if not text:
            raise Refused("a question needs to say something")
        if not blocked_on:
            raise Refused(NO_BLOCK)

        low = text.lower()
        if any(hint in low for hint in _CHANGE_HINTS):
            raise Refused(A_CHANGE)

        tool = self._can_look(text, blocked_on)
        if tool:
            raise Refused(LOOK_INSTEAD.format(tool=tool))

        if audience not in AUDIENCES:
            audience = REVIEWER
        # What it may do is not a reviewer's to answer. Re-addressed rather
        # than refused: the question is legitimate, the audience was wrong.
        redirected = False
        if audience == REVIEWER and _asks_permission(low):
            audience, redirected = OPERATOR, True

        if self._ruled_on(text):
            raise Refused(RULED_ALREADY)

        probe = Question(text=text, blocked_on=blocked_on, audience=audience)
        repeats = sum(1 for q in self.questions
                      if probe.about_the_same(q)
                      and now - q.asked_at <= SUBJECT_WINDOW_S)
        if repeats >= MAX_PER_SUBJECT:
            raise Refused(ASKED_ALREADY.format(n=repeats))

        open_now = [q for q in self.questions if q.state == OPEN]
        if len(open_now) >= self.max_open:
            raise Refused(TOO_MANY.format(n=len(open_now)))

        probe.ttl_s = max(60.0, float(ttl_s or DEFAULT_TTL_S))
        probe.asked_at = now
        self.questions.append(probe)
        self._record(probe, "asked", redirected=redirected)
        return probe

    # ---- answering -----------------------------------------------------

    def answer(self, question_id: str, text: str, by: str = "") -> Optional[Question]:
        """A reviewer or the operator replies. Recorded with who said it."""
        now = self._clock()
        for q in self.questions:
            if q.id != question_id or q.state != OPEN:
                continue
            q.answer = " ".join(str(text or "").split())[:2000]
            q.answered_by = str(by or "")[:60] or ("your operator"
                                                   if q.audience == OPERATOR
                                                   else "a second reader")
            q.answered_at = now
            q.state = ANSWERED
            self._record(q, "answered")
            if self.agent is not None:
                self._tell_agent(q)
            return q
        return None

    def withdraw(self, question_id: str) -> bool:
        for q in self.questions:
            if q.id == question_id and q.state == OPEN:
                q.state = WITHDRAWN
                self._record(q, "withdrawn")
                return True
        return False

    def sweep(self, now: Optional[float] = None) -> int:
        """Expire what has gone stale. Nothing accumulates."""
        now = self._clock() if now is None else now
        gone = 0
        for q in self.questions:
            if q.expired(now):
                q.state = EXPIRED
                self._record(q, "expired")
                gone += 1
        return gone

    def resolve(self, blocked_on: str) -> int:
        """The thing questions were waiting on has resolved; they die unasked.

        The other half of expiry, and the better half. A question whose block
        has cleared is not a question any more, and answering it would be
        answering into a situation that has moved.
        """
        key = " ".join(str(blocked_on or "").split()).lower()
        if not key:
            return 0
        gone = 0
        for q in self.questions:
            if q.state == OPEN and key in q.blocked_on.lower():
                q.state = EXPIRED
                self._record(q, "resolved")
                gone += 1
        return gone

    # ---- reading -------------------------------------------------------

    def open_questions(self, audience: Optional[str] = None) -> list:
        self.sweep()
        return [q for q in self.questions
                if q.state == OPEN and (audience is None or q.audience == audience)]

    def waiting(self) -> list:
        """What the agent carries in its context: its own open questions."""
        now = self._clock()
        return [{"asked": q.text, "blocked_on": q.blocked_on,
                 "of": "your operator" if q.audience == OPERATOR else "a second reader",
                 "waiting_s": max(0, round(now - q.asked_at))}
                for q in self.open_questions()]

    def answers(self, limit: int = 5) -> list:
        """Recent replies, which are the part worth carrying forward."""
        done = [q for q in self.questions if q.state == ANSWERED]
        done.sort(key=lambda q: q.answered_at or 0, reverse=True)
        return [{"asked": q.text, "answer": q.answer, "by": q.answered_by}
                for q in done[:max(1, limit)]]

    def state(self) -> dict:
        now = self._clock()
        return {"open": len(self.open_questions()),
                "answered": sum(1 for q in self.questions if q.state == ANSWERED),
                "expired": sum(1 for q in self.questions if q.state == EXPIRED),
                "max_open": self.max_open,
                "questions": [q.to_dict(now) for q in self.questions[-20:]]}

    # ---- the record ----------------------------------------------------

    def _ruled_on(self, text: str) -> bool:
        """Has a reviewer already answered this, in substance?

        Crude subject matching, deliberately. A precise version would be a
        model call, and spending a model call to decide whether to spend a
        model call is the tax on indecision it warned about.
        """
        probe = Question(text=text, blocked_on="x")
        return any(q.state == ANSWERED and probe.about_the_same(q)
                   for q in self.questions)

    def _record(self, q: Question, event: str, redirected: bool = False):
        if self.agent is None:
            return
        try:
            self.agent.ledger.record("question", {
                "cycle": getattr(self.agent, "cycle_count", 0),
                "event": event, "id": q.id, "audience": q.audience,
                "blocked_on": q.blocked_on[:200],
                "text": q.text[:300],
                "answered_by": q.answered_by or None,
                "redirected": redirected or None})
        except Exception:
            pass

    def _tell_agent(self, q: Question):
        """An answer is instruction, not evidence. It goes where it is read."""
        try:
            self.agent.remember(
                f"{q.answered_by} answered your question \"{q.text[:160]}\": "
                f"{q.answer[:400]}",
                kind="verdict",
                source="operator" if q.audience == OPERATOR else "review")
            self.agent.note_operator("question answered")
        except Exception:
            pass


def _asks_permission(low: str) -> bool:
    return any(p in low for p in (
        "may i", "am i allowed", "can i have", "could i have", "permission to",
        "would you let me", "grant me", "unlock", "raise my rung"))


def default_can_look(text: str, blocked_on: str) -> Optional[str]:
    """A tool that would answer this, or None.

    Its own rule, and the sharpest one it gave: "If I haven't checked
    /sys/kernel/security yet, I shouldn't ask 'is lockdown enabled?' -- I
    should inspect_path first."
    """
    low = f"{text} {blocked_on}".lower()
    # Ordered most specific first: a question about a file's *contents* is a
    # read, not a stat, even though it also mentions a path.
    if any(w in low for w in ("what is in the file", "what does the file say",
                              "contents of", "what does the document say",
                              "what does the bill", "read the file",
                              "what does it say", "what does the file",
                              "what is in the")):
        return "read_file"
    if any(w in low for w in ("exist", "is there a file", "is there a directory",
                              "is the directory", "does the path", "how big is")):
        return "inspect_path"
    if any(w in low for w in ("what did i do", "what have i been doing",
                              "in the journal", "in the log", "in the logs",
                              "what went wrong", "my own journal")):
        return "read_logs"
    if any(w in low for w in ("how full", "how much disk", "disk usage",
                              "how much memory", "how much space")):
        return "system_check"
    if any(w in low for w in ("what does this cost", "what am i spending",
                              "what is the bill", "how much is the account",
                              "spending on this account", "what do i cost")):
        return "estate_report"
    return None
