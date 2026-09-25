"""Solve Moltbook's anti-spam challenge.

When content is created, Moltbook returns an obfuscated arithmetic word
problem and the post stays invisible until it is answered. Five minutes for
a post or comment, thirty seconds for a submolt. Miss the window and the
content silently never appears.

The obfuscation scatters symbols through the text, alternates capitals and
breaks words apart:

    "A] lO^bSt-Er S[wImS aT/ tW]eNn-Tyy mE^tE[rS aNd] SlO/wS bY^ fI[vE,
     wH-aTs] ThE/ nEw^ SpE[eD?"   ->   a lobster swims at twenty meters
                                       and slows by five   ->   20 - 5 = 15.00

This solver is deliberately deterministic: no model call, so no cost, no
latency and no dependency on a model being reachable inside a 30-second
window. It is brittle by design on Moltbook's side — that is the point of
the challenge — so `solve()` returns None rather than a guess when it is
unsure, and the caller decides whether to escalate to a model.

Doubled letters ("tWeNnTyy") are a normal part of the obfuscation, so word
matching tolerates repeats.
"""

from __future__ import annotations

import re

UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19,
}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
        "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
SCALES = {"hundred": 100, "thousand": 1000}

# Ordered longest-first so "seventeen" is not matched as "seven".
NUMBER_WORDS = sorted(list(UNITS) + list(TENS) + list(SCALES),
                      key=len, reverse=True)

ADD = ["plus", "and", "gains", "increases by", "speeds up by", "rises by",
       "adds", "more than", "faster by", "up by"]
SUB = ["slows by", "minus", "loses", "decreases by", "drops by", "less than",
       "reduced by", "slower by", "down by", "falls by", "subtract"]
MUL = ["times", "multiplied by", "product of"]
DIV = ["divided by", "split", "over", "per"]


def _deobfuscate(text: str) -> str:
    """Strip the scattered symbols and collapse the case games."""
    s = (text or "").lower()
    # Everything that is not a letter, digit or space is noise.
    s = re.sub(r"[^a-z0-9\s]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _collapse_doubles(word: str) -> str:
    """"tweenntyy" -> "twenty". Applied only when the word is not already known."""
    return re.sub(r"(.)\1+", r"\1", word)


def _word_to_number(tokens: list[str]) -> int | None:
    total, current, seen = 0, 0, False
    for t in tokens:
        if t in UNITS:
            current += UNITS[t]; seen = True
        elif t in TENS:
            current += TENS[t]; seen = True
        elif t in SCALES:
            current = max(current, 1) * SCALES[t]; seen = True
        else:
            return None
    return total + current if seen else None


def _numbers(clean: str) -> list[tuple[int, float]]:
    """Every number in the text, as (position, value)."""
    out = []
    for m in re.finditer(r"\b\d+(?:\.\d+)?\b", clean):
        out.append((m.start(), float(m.group(0))))

    words = [(m.start(), m.group(0)) for m in re.finditer(r"\b[a-z]+\b", clean)]
    i = 0
    while i < len(words):
        run, pos = [], words[i][0]
        while i < len(words):
            w = words[i][1]
            cand = w if w in UNITS or w in TENS or w in SCALES else _collapse_doubles(w)
            if cand in UNITS or cand in TENS or cand in SCALES:
                run.append(cand); i += 1
            else:
                break
        if run:
            v = _word_to_number(run)
            if v is not None:
                out.append((pos, float(v)))
        else:
            i += 1
    return sorted(out)


def _operation(clean: str) -> str | None:
    """Which operation, decided by the last operator phrase before the second number."""
    best, op = -1, None
    for name, phrases in (("+", ADD), ("-", SUB), ("*", MUL), ("/", DIV)):
        for p in phrases:
            for m in re.finditer(r"\b" + re.escape(p) + r"\b", clean):
                # "and" is a weak signal; only take it if nothing stronger appears.
                weight = 0 if p == "and" else 1
                if (weight, m.start()) > (0 if op == "+" else 1, best) or op is None:
                    if weight == 0 and op not in (None, "+"):
                        continue
                    best, op = m.start(), name
    return op


def solve(challenge_text: str) -> str | None:
    """Return the answer formatted as Moltbook wants it, or None if unsure.

    None is a real answer here: a wrong number burns the challenge, and the
    caller can fall back to a model. A guess cannot be told apart from a
    solve, which is why this never guesses.
    """
    clean = _deobfuscate(challenge_text)
    if not clean:
        return None
    nums = _numbers(clean)
    if len(nums) < 2:
        return None
    op = _operation(clean)
    if op is None:
        return None

    a, b = nums[0][1], nums[1][1]
    try:
        if op == "+":
            r = a + b
        elif op == "-":
            r = a - b
        elif op == "*":
            r = a * b
        else:
            if b == 0:
                return None
            r = a / b
    except (ArithmeticError, ValueError):
        return None
    return f"{r:.2f}"
