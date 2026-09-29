"""How a true thing lands on the person it is said to.

Everything else in this codebase asks whether a statement is *true*. The
verdict register rules on it, the path checker tests it against the disk, the
claim checker compares stated figures with real readings. None of them ask the
other question, which is what it costs to say it to somebody.

The operator put it plainly: being right often costs more than was intended.
He has been told, for years, that it is not what he says but how he says it --
usually by people who did not want to deal with the what. But there is a real
distinction underneath that, and it is not plain against polished. It is

    **plain**, which is about the world:    the record is held by the supplier
    **pointed**, which is aimed at a person: you did not read the pack

Identical information. Completely different bill, and the bill falls on the
person who said it. A true thing said about a situation is free. The same true
thing with somebody in the subject position asks them to pay in standing for
your accuracy, and almost nobody will. They pay in avoidance instead: they go
quiet, and a fortnight later they decline without giving a reason.

**This module never softens a claim and never rewrites one.** That has to be
said first, because a thing that adjusted tone would be the exact opposite of
what the rest of this codebase is for -- an agent that hedges a finding to
spare someone's feelings is an agent that reports an empty scan as zero
findings. The claim survives intact, every time, in the words it was written
in. What this looks for is narrower: whether a *person* has been put in the
subject position of a failure, and whether the sentence still has a job to do
once they are taken out of it.

Three things it will not touch, because they are the whole point of the rest
of the system:

    a finding about the world, however unwelcome
    a refusal, and the reason for it
    the agent's own account of its own mistakes -- "I did not check that" is
    accountability, and the patterns here are second-person for that reason

And one standing question it asks whenever it finds something, borrowed from
the question register: **what does the other person do differently if they
accept this?** If there is no answer, the sentence is not a correction. It is
a scoreboard, and a scoreboard is the most expensive sentence there is,
because it buys nothing and it is remembered.
"""

import re
from typing import Optional

# What the marker is doing to the sentence, rather than what it says.
SCOREKEEPING = "scorekeeping"
BLAME = "blame"
AFTER_THE_FACT = "after the fact"
KINDS = (SCOREKEEPING, BLAME, AFTER_THE_FACT)

# Deliberately few, and deliberately narrow. A check that fires on ordinary
# writing gets switched off within a week, and then it catches nothing at all.
# These are high-precision on purpose: the five that actually cost something,
# not every second-person sentence in the language.
_MARKERS = (
    (SCOREKEEPING, re.compile(
        r"\b(?:as|like)\s+i\s+(?:said|mentioned|explained|noted|told you|"
        r"already\s+\w+)\b", re.I)),
    (SCOREKEEPING, re.compile(
        r"\b(?:as|per)\s+(?:my|our)\s+(?:previous|earlier|last)\s+"
        r"(?:email|message|note|point|pack)\b", re.I)),
    (SCOREKEEPING, re.compile(
        r"\bi\s+(?:did|had)\s+(?:say|tell you|mention|explain)\b", re.I)),
    (SCOREKEEPING, re.compile(
        r"\b(?:which|that)\s+i\s+already\s+\w+", re.I)),
    (BLAME, re.compile(
        r"\byou\s+(?:did\s*n[o']t|didn[o']t|failed\s+to|never|"
        r"forgot(?:\s+to)?|missed|neglected\s+to)\b", re.I)),
    (BLAME, re.compile(
        r"\byou\s+(?:should|ought\s+to|could)\s+have\b", re.I)),
    # Counterfactual past only. A bare "if you" is "if you would like",
    # which is the friendliest sentence in the language, and it fired on it.
    (BLAME, re.compile(r"\b(?:if\s+you\s+(?:had|['’]d)|had\s+you)\s+\w+", re.I)),
    (AFTER_THE_FACT, re.compile(
        r"\b(?:next\s+time|in\s+future|for\s+future\s+reference|"
        r"going\s+forward)\b", re.I)),
)

# What each kind is costing, and the shape of the version that costs nothing.
# Not a rewrite -- this module does not rewrite -- but the transform, so the
# agent or the operator can do it themselves and keep their own words.
WHY = {
    SCOREKEEPING: (
        "this establishes who was right rather than what is true. The fact "
        "survives without it: take the clause off the front and the sentence "
        "says the same thing and costs nothing"),
    BLAME: (
        "a person is the subject and the predicate is a failure. The same "
        "fact will usually go without them in it -- 'the answer was in the "
        "pack' rather than 'you did not read the pack'"),
    AFTER_THE_FACT: (
        "advice to someone who has not asked for it, about a thing already "
        "decided. If it changes nothing now, it is not advice"),
}

# The question that decides whether any of it is worth saying at all.
THE_TEST = ("what does the other person do differently if they accept this? "
            "No answer means it is not a correction, it is a scoreboard")

MAX_TEXT = 20_000

# Finding nothing is not the same as there being nothing, and this codebase
# has met that confusion before -- an empty scan report read as a report of
# zero findings. Some pointing has no marker in it at all. "Read the pack
# before meeting someone" is as pointed as a sentence gets and contains not
# one phrase a regular expression can hold on to. So a clean result says what
# it actually checked, and never that the message is safe to send.
NOT_A_CLEARANCE = (
    "No marker was found. That is not the same as nothing being aimed: the "
    "most pointed sentences are often the ones with no phrase in them to "
    "catch -- an instruction given to someone who did not ask for it reads "
    "as a correction however it is worded. This checks for the forms that "
    "have a shape. It cannot check for the ones that do not.")


class Finding:
    """One place where a claim has been aimed at a person."""

    def __init__(self, kind: str, phrase: str, where: int):
        self.kind = kind
        self.phrase = phrase
        self.where = where

    def as_dict(self) -> dict:
        return {"kind": self.kind, "phrase": self.phrase,
                "why_it_costs": WHY[self.kind], "at": self.where}

    def __repr__(self):
        return f"<Finding {self.kind}: {self.phrase!r}>"


def aim(text: str) -> list:
    """Where this puts a person in the subject position of a failure.

    Returns findings, never a rewrite. Second-person and first-person-
    retrospective only: the agent saying "I did not check that" is
    accountability and is not this module's business.
    """
    body = str(text or "")[:MAX_TEXT]
    if not body.strip():
        return []
    found, seen = [], set()
    for kind, pattern in _MARKERS:
        for match in pattern.finditer(body):
            phrase = " ".join(match.group(0).split())
            key = (kind, phrase.lower())
            if key in seen:
                continue
            seen.add(key)
            found.append(Finding(kind, phrase, match.start()))
    found.sort(key=lambda f: f.where)
    return found


def read(text: str, to: str = "") -> dict:
    """What this would cost to send, and to whom.

    ``to`` is who it is addressed to, and it only ever changes the wording of
    the report -- never whether the check runs and never what it finds.
    """
    found = aim(text)
    out = {"aimed": [f.as_dict() for f in found],
           "clean": not found,
           "claim_untouched": True,
           "what_this_does_not_catch": NOT_A_CLEARANCE}
    if found:
        out["the_test"] = THE_TEST
        out["what_this_is_not"] = (
            "This is not a note about tone and nothing here says to soften "
            "the claim. The claim is right or it is not, and that is settled "
            "elsewhere. This says only that a person has been put in it.")
        if to:
            out["to"] = to
    return out


def note(report: dict) -> Optional[str]:
    """One line, for the operator or for the agent's own record."""
    aimed = (report or {}).get("aimed") or []
    if not aimed:
        return None
    kinds = []
    for entry in aimed:
        if entry["kind"] not in kinds:
            kinds.append(entry["kind"])
    phrases = ", ".join(f'"{e["phrase"]}"' for e in aimed[:3])
    return (f"{len(aimed)} thing(s) here aimed at the person rather than the "
            f"situation ({'; '.join(kinds)}): {phrases}. The claim stands; "
            f"the person does not need to be in it. {THE_TEST}.")


def context() -> dict:
    """What the model is told. A fact about people, not a rule about tone."""
    return {
        "what_it_costs_to_be_right": (
            "Everything else you are given asks whether a thing is true. This "
            "is about what it costs to say it to somebody, which is a "
            "different question and is not settled by the first one."),
        "the_distinction": {
            "plain": "about the world -- 'the record is held by the supplier'",
            "pointed": "the same fact with a person in it -- 'you did not "
                       "read the pack'",
            "the_difference": (
                "Identical information. The second asks the other person to "
                "pay in standing for your accuracy, and almost nobody will. "
                "They pay in avoidance instead: they go quiet, and later they "
                "decline without giving a reason. The bill falls on the one "
                "who said it."),
        },
        "before_a_correction": THE_TEST,
        "what_this_is_not": (
            "Not a reason to soften anything. Never hedge a finding, never "
            "make an unwelcome fact smaller, never leave something out "
            "because it will not be liked -- that failure is worse and it is "
            "the one the rest of your design is against. Say the whole thing. "
            "Say it plainly. Just do not aim it at whoever is standing there."),
        "what_it_does_not_catch": NOT_A_CLEARANCE,
        "your_own_mistakes_are_different": (
            "None of this applies to your own errors. 'I did not check that' "
            "is accountability and costs you nothing worth keeping."),
    }


def build_bearing(config: Optional[dict] = None):
    """There is no state to hold; the module is the register. Present for
    symmetry with the others, and so it can be switched off in config if it
    ever starts firing on ordinary writing."""
    cfg = (config or {}).get("bearing") or {}
    return None if cfg.get("enabled") is False else Bearing()


class Bearing:
    """The register, such as it is."""

    enabled = True

    def read(self, text: str, to: str = "") -> dict:
        return read(text, to)

    def note(self, text: str, to: str = "") -> Optional[str]:
        return note(self.read(text, to))

    def context(self) -> dict:
        return context()
