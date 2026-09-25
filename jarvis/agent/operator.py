"""What the agent knows about the person it works for, and where it came from.

Until now it knew the machine and nothing about Paul. It could tell you the
root filesystem was at 25% and could not have told you what he was for. For
something meant to help with a life rather than a server, that is the whole
gap.

The obvious way to close it is the wrong one. This codebase already says, in
the memory design, that an agent writing its own inferences into a record of
a person would quietly destroy the thing that makes such a record worth
having -- that it contains what the person actually said. So this profile is
**given**, not gathered. Nothing here is learned by watching. Every line is
put in by the operator or by a named reviewer on his instruction, and every
line carries which of the two it was.

That distinction is the point, so it survives into what the model reads:

    stated    Paul said this, in his own words or close to them. Treat it as
              instruction. If it conflicts with an observation, it wins.
    observed  someone's read of how he works, recorded at his request. Useful,
              weaker, and his to correct. Never quote it back to him as fact
              about himself.

Three structural rules rather than three intentions.

**No contact details, ever.** A phone number, an email address or a postcode
is a credential in this system, not a preference: the one destination the
agent may reach is chosen in config the model cannot read, and notify.py's
promise depends on that staying true. The profile refuses to store one, so
the promise cannot be undone by someone being helpful.

**Never consolidated.** Memory consolidation merges and derives, and a
derived summary of a person is exactly how "he writes quickly" becomes "he is
careless". These rows are pinned and marked so the consolidator leaves them
alone; a fact about a person is not raw material.

**Always inspectable and always removable.** He can read every line the agent
holds about him and take any of them out. A profile its subject cannot see is
a rumour with his name on it.
"""

import re
import time
from dataclasses import dataclass, field
from typing import Optional

STATED = "stated"
OBSERVED = "observed"
SOURCES = (STATED, OBSERVED)

MAX_FACTS = 60
MAX_LEN = 400

# What must never end up in here. Contact details are how the agent would
# reach a person, and where it may reach is settled in config the model cannot
# read -- see notify.py, which promises exactly that. A postcode and a bank
# card are here for the same reason: a profile is for how someone works, and
# anything that identifies or authorises them belongs behind the fence.
_CONTACT = (
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b"), "an email address"),
    (re.compile(r"(?:\+\d[\d\s().-]{7,}\d)|(?:\b0\d[\d\s().-]{7,}\d\b)"), "a phone number"),
    (re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b"), "a postcode"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "a long number that may be a card"),
    (re.compile(r"\b\d{2}-\d{2}-\d{2}\b"), "a sort code"),
)


class Refused(ValueError):
    """The line was not stored, and the reason says what to do instead."""


@dataclass
class Fact:
    text: str
    source: str = STATED
    by: str = ""
    at: float = field(default_factory=time.time)

    def line(self) -> str:
        return self.text if self.source == STATED else f"{self.text} (observed)"


def check(text: str) -> Optional[str]:
    """Why this must not go in the profile, or None."""
    for pattern, what in _CONTACT:
        if pattern.search(text):
            return (f"this looks like {what}. Contact details are not profile: "
                    "where the agent may reach its operator is settled in "
                    "config the model cannot read, and putting one here would "
                    "undo that. Describe the preference instead.")
    return None


class OperatorProfile:
    """Given, not gathered. Inspectable, correctable, never consolidated."""

    def __init__(self, agent=None, name: str = "your operator"):
        self.agent = agent
        self.name = name
        self.facts: list = []

    # ---- writing -------------------------------------------------------

    def add(self, text: str, source: str = STATED, by: str = "") -> Fact:
        text = " ".join(str(text or "").split())[:MAX_LEN]
        if not text:
            raise Refused("nothing to record")
        why = check(text)
        if why:
            raise Refused(why)
        if source not in SOURCES:
            source = OBSERVED
        low = text.lower()
        for existing in self.facts:
            if existing.text.lower() == low:
                # A restatement promotes an observation to stated; it never
                # demotes. Him saying a thing outranks anyone's read of him.
                if source == STATED:
                    existing.source, existing.by = STATED, by
                return existing
        if len(self.facts) >= MAX_FACTS:
            raise Refused(f"the profile holds {MAX_FACTS} lines already; "
                          "take one out before adding another")
        fact = Fact(text=text, source=source, by=str(by or "")[:60])
        self.facts.append(fact)
        self._persist(fact)
        return fact

    def forget(self, text: str) -> bool:
        """Take a line out. His, whenever he wants, without explaining why."""
        wanted = " ".join(str(text or "").split()).lower()
        for i, fact in enumerate(self.facts):
            if fact.text.lower() == wanted or wanted in fact.text.lower():
                self.facts.pop(i)
                self._retire(fact)
                return True
        return False

    # ---- reading -------------------------------------------------------

    def lines(self) -> list:
        """What the model is told, stated first so instruction leads."""
        stated = [f.text for f in self.facts if f.source == STATED]
        observed = [f.text for f in self.facts if f.source == OBSERVED]
        return stated + [f"{t} (someone's read of him, not his words)"
                         for t in observed]

    def context(self) -> Optional[dict]:
        if not self.facts:
            return None
        return {
            "who": self.name,
            "what_he_has_told_you": [f.text for f in self.facts
                                     if f.source == STATED],
            "what_others_have_observed": [f.text for f in self.facts
                                          if f.source == OBSERVED],
            "how_to_use_this": (
                "The first list is what he said, and it is instruction: where "
                "it conflicts with anything you have inferred, he wins. The "
                "second is someone's read of how he works, recorded at his "
                "request -- useful for judging tone and pace, and never to be "
                "quoted back to him as a fact about himself. None of this was "
                "learned by watching him, and you should not add to it by "
                "watching him either: if you think something belongs here, "
                "say so and let him decide."),
        }

    def state(self) -> dict:
        """Everything held, for him to read and correct."""
        return {"who": self.name, "count": len(self.facts),
                "facts": [{"text": f.text, "source": f.source,
                           "by": f.by or None, "at": f.at} for f in self.facts]}

    # ---- durability ----------------------------------------------------

    def load(self):
        """Read the profile back after a restart."""
        if self.agent is None:
            return
        try:
            rows = self.agent.store.recent(MAX_FACTS * 2, kind="operator")
        except Exception:
            return
        for row in reversed(rows):
            meta = row.get("meta") or {}
            if not meta.get("profile"):
                continue
            if str(row.get("state") or "live") != "live":
                continue
            text = str(row.get("text") or "")
            if not text or any(f.text == text for f in self.facts):
                continue
            self.facts.append(Fact(text=text,
                                   source=meta.get("source") or STATED,
                                   by=meta.get("by") or "",
                                   at=float(row.get("ts") or time.time())))

    def _persist(self, fact: Fact):
        if self.agent is None:
            return
        try:
            self.agent.store.remember(
                fact.text, kind="operator", source="operator", pinned=True,
                meta={"profile": True, "source": fact.source, "by": fact.by,
                      # Consolidation merges and derives, and a derived
                      # summary of a person is how "he writes quickly"
                      # becomes "he is careless".
                      "never_consolidate": True})
        except Exception:
            pass
        try:
            self.agent.ledger.record("action", {
                "cycle": getattr(self.agent, "cycle_count", 0),
                "actor": "operator", "action": "operator_profile",
                "source": fact.source, "by": fact.by or None,
                "text": fact.text[:300]})
        except Exception:
            pass

    def _retire(self, fact: Fact):
        if self.agent is None:
            return
        try:
            for row in self.agent.store.recent(MAX_FACTS * 2, kind="operator"):
                if str(row.get("text") or "") == fact.text:
                    self.agent.store.set_state(row["id"], "superseded")
                    break
            self.agent.ledger.record("action", {
                "cycle": getattr(self.agent, "cycle_count", 0),
                "actor": "operator", "action": "operator_profile_removed",
                "text": fact.text[:300]})
        except Exception:
            pass
