"""Rule-based relevance scoring.

There is no model here. Every number a hit carries can be traced back to a
rule in config.toml and an explicit match in the text, which is the point:
the digest says why each item matched, and that reason is checkable.

The score is built in five steps.

  1. Find distinct keyword matches in title and body, per tier.
  2. Weight each match by tier, doubling it if it was in the title.
  3. Sort matches by weight and apply diminishing returns to all but the
     strongest, so twelve weak terms never outrank one strong one.
  4. Multiply by the source weight, then by a recency decay.
  5. Subtract a flat penalty for each distinct negative keyword matched.

Steps 3 and 5 exist because of how the noise actually arrives. Job adverts
and funding announcements pick up "clinical decision support" and "AI"
together and would otherwise score respectably.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Match:
    """One keyword that fired, and where."""
    keyword: str
    tier: str          # "a" | "b" | "c"
    where: str         # "title" | "body"
    weight: float      # after the title multiplier, before diminishing


@dataclass
class Score:
    total: float
    matches: list[Match] = field(default_factory=list)
    negatives: list[str] = field(default_factory=list)
    keyword_subtotal: float = 0.0
    source_weight: float = 1.0
    recency_factor: float = 1.0
    penalty: float = 0.0

    @property
    def matched_keywords(self) -> list[str]:
        seen, out = set(), []
        for m in self.matches:
            if m.keyword not in seen:
                seen.add(m.keyword)
                out.append(m.keyword)
        return out

    def why(self) -> str:
        """One human-readable line explaining the score.

        This goes in the digest, so it has to be readable at a glance and
        honest about what actually fired.
        """
        if not self.matches:
            return "no keyword matched"
        bits = []
        for kw in self.matched_keywords[:4]:
            where = "title" if any(
                m.keyword == kw and m.where == "title" for m in self.matches
            ) else "body"
            bits.append(f"{kw} ({where})")
        s = "matched " + ", ".join(bits)
        extra = len(self.matched_keywords) - 4
        if extra > 0:
            s += f", +{extra} more"
        if self.negatives:
            s += f"; penalised for {', '.join(self.negatives[:3])}"
        return s


# Words too common to carry meaning inside a keyword. Dropping them is what
# lets "AI governance healthcare" match "governance of AI in healthcare".
STOPWORDS = {"a", "an", "the", "of", "in", "on", "for", "and", "or", "to",
             "with", "at", "by", "from", "into", "is", "are"}

_WORD = re.compile(r"[a-z0-9][a-z0-9'\-]*", re.IGNORECASE)


def _pattern(keyword: str) -> re.Pattern:
    """Whole-word / whole-phrase, case-insensitive.

    Word boundaries only where the keyword actually starts and ends with a
    word character: "raises $" and "we're hiring" would break a naive
    \\b...\\b wrapper.
    """
    esc = re.escape(keyword)
    esc = esc.replace(r"\ ", r"\s+")          # tolerate line wraps in abstracts
    left = r"\b" if keyword[:1].isalnum() else ""
    right = r"\b" if keyword[-1:].isalnum() else ""
    return re.compile(left + esc + right, re.IGNORECASE)


def _tokens(text: str) -> list[str]:
    return [m.group(0).lower() for m in _WORD.finditer(text or "")]


def _significant(keyword: str) -> list[str]:
    ws = [w for w in _tokens(keyword) if w not in STOPWORDS]
    return ws or _tokens(keyword)


def proximity_match(tokens: list[str], needles: list[str], window: int) -> bool:
    """True if every needle appears within a span of `window` tokens.

    Order does not matter. "governance of AI in healthcare" satisfies
    ["ai", "governance", "healthcare"] with a window of 8. A single-word
    keyword degenerates to a presence check, which is what we want.
    """
    if not needles:
        return False
    if len(needles) == 1:
        return needles[0] in tokens

    positions = []
    index = {n: i for i, n in enumerate(needles)}
    for pos, tok in enumerate(tokens):
        i = index.get(tok)
        if i is not None:
            positions.append((pos, i))
    if len(positions) < len(needles):
        return False

    need = len(needles)
    counts = [0] * need
    have = 0
    left = 0
    for right in range(len(positions)):
        _, ri = positions[right]
        if counts[ri] == 0:
            have += 1
        counts[ri] += 1
        while have == need:
            if positions[right][0] - positions[left][0] <= window:
                return True
            _, li = positions[left]
            counts[li] -= 1
            if counts[li] == 0:
                have -= 1
            left += 1
    return False


def _dedupe(matchers: list) -> list:
    """Collapse proximity keywords that are the same matcher in disguise.

    Under proximity matching, common words are dropped and order does not
    matter, so "healthcare AI" and "AI in healthcare" reduce to exactly the
    same test: {ai, healthcare} within the window. Left in, both fire on the
    same text and the item scores twice for saying one thing — enough to
    push a passing body mention over the digest threshold.

    Phrase keywords are never collapsed: "AI safety case" and "clinical
    safety case" are genuinely different phrases.
    """
    seen, out = set(), []
    for m in matchers:
        if m.mode != "proximity":
            out.append(m)
            continue
        fingerprint = frozenset(m.needles)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        out.append(m)
    return out


class _Matcher:
    """One keyword, matched either as an exact phrase or by proximity."""

    __slots__ = ("keyword", "mode", "pattern", "needles", "window")

    def __init__(self, keyword: str, mode: str, window: int):
        self.keyword = keyword
        self.mode = mode
        self.window = window
        self.pattern = _pattern(keyword) if mode == "phrase" else None
        self.needles = _significant(keyword) if mode == "proximity" else []

    def hits(self, text: str, tokens: list[str]) -> bool:
        if self.mode == "phrase":
            return bool(self.pattern.search(text))
        return proximity_match(tokens, self.needles, self.window)


class Scorer:
    def __init__(self, config: dict):
        kw = config["keywords"]
        sc = config["scoring"]
        self.tier_weights = {
            "a": float(kw["tier_a_weight"]),
            "b": float(kw["tier_b_weight"]),
            "c": float(kw["tier_c_weight"]),
        }
        window = int(sc.get("proximity_window", 8))
        modes = {
            "a": kw.get("tier_a_match", "phrase"),
            "b": kw.get("tier_b_match", "proximity"),
            "c": kw.get("tier_c_match", "proximity"),
        }
        self.modes = modes
        self.tiers = {t: _dedupe(
            [_Matcher(k, modes[t], window) for k in kw[f"tier_{t}"]])
            for t in ("a", "b", "c")}
        self.negatives = [(k, _pattern(k)) for k in kw.get("negative", [])]
        self.vetoes = [(k, _pattern(k)) for k in kw.get("veto", [])]
        self.negative_penalty = float(kw.get("negative_penalty", 0.0))
        self.title_multiplier = float(sc["title_multiplier"])
        self.diminishing = float(sc["diminishing"])
        self.decay_per_day = float(sc["decay_per_day"])
        self.decay_floor = float(sc["decay_floor"])

    def vetoed(self, title: str, body: str) -> str | None:
        """Return the veto keyword that drops this item, or None."""
        text = f"{title}\n{body}"
        for kw, pat in self.vetoes:
            if pat.search(text):
                return kw
        return None

    def recency_factor(self, published: datetime, now: datetime) -> float:
        age_days = max(0.0, (now - published).total_seconds() / 86_400.0)
        return max(self.decay_floor, 1.0 - self.decay_per_day * age_days)

    def score(
        self,
        *,
        title: str,
        body: str,
        source_weight: float,
        published: datetime,
        now: datetime | None = None,
    ) -> Score:
        now = now or datetime.now(timezone.utc)
        title = title or ""
        body = body or ""

        # 1 + 2. Distinct matches, best placement wins for each keyword.
        best: dict[str, Match] = {}
        t_tokens = _tokens(title)
        b_tokens = _tokens(body)
        for tier, entries in self.tiers.items():
            base = self.tier_weights[tier]
            for m in entries:
                kw = m.keyword
                in_title = m.hits(title, t_tokens)
                in_body = m.hits(body, b_tokens)
                if not (in_title or in_body):
                    continue
                where = "title" if in_title else "body"
                weight = base * (self.title_multiplier if in_title else 1.0)
                prior = best.get(kw)
                if prior is None or weight > prior.weight:
                    best[kw] = Match(keyword=kw, tier=tier, where=where, weight=weight)

        matches = sorted(best.values(), key=lambda m: m.weight, reverse=True)

        # 3. Diminishing returns on all but the strongest match.
        subtotal = 0.0
        for i, m in enumerate(matches):
            subtotal += m.weight * (self.diminishing ** i)

        # 4. Source weight and recency.
        recency = self.recency_factor(published, now)
        total = subtotal * source_weight * recency

        # 5. Negative keywords.
        text = f"{title}\n{body}"
        negs = [kw for kw, pat in self.negatives if pat.search(text)]
        penalty = self.negative_penalty * len(negs)
        total -= penalty

        return Score(
            total=round(max(0.0, total), 3),
            matches=matches,
            negatives=negs,
            keyword_subtotal=round(subtotal, 3),
            source_weight=source_weight,
            recency_factor=round(recency, 3),
            penalty=round(penalty, 3),
        )
