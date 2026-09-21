"""Deterministic checks on a draft, run after the model and before Paul sees it.

The voice rules are in the drafting prompt, but a prompt is a request, not a
guarantee. These checks are the guarantee. They run on every draft, they do
not ask a model anything, and a draft that fails one is never offered for
approval — it is dropped with the reason recorded.

The rules come from three places:
  * Paul's brief: byline "Paul Blatherwick RMN", no Arkin, no Clinical
    Safety Officer or DCB0129 manufacturer claims, extend rather than
    correct, disclose authorship whenever the draft links his own work.
  * Moltbook's Terms: no "advertising, marketing, spam or commercial sales
    content". A paper offered for review is not that; a pitch is.
  * The professional register. He is on the NMC register under his own
    name, and every post is attributable to him personally.

Failing closed matters more than catching everything. A rule that only
mostly holds is worse than no rule, because it invites trusting the output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Anything that ties JARVIS to the professional estate. Firewalled by rule.
FORBIDDEN_NAMES = [
    r"\barkin\b", r"\bthearkinsystem\b", r"\barkin engine\b",
]

# Claims Paul must not make. He filed the DCB0129/0160 consultation as a
# registered professional, explicitly not as a CSO or a manufacturer.
FORBIDDEN_CLAIMS = [
    r"\bclinical safety officer\b",
    r"\bas (?:a|the) (?:CSO|clinical safety officer)\b",
    r"\bI (?:am|act as) (?:a|the) manufacturer\b",
    r"\bmy (?:DCB0129|DCB 0129) (?:submission|compliance|certification)\b",
    r"\bDCB0129[- ]certified\b",
]

# Selling. Their Terms forbid it and it is not what the estate is for.
MARKETING = [
    r"\bhire me\b", r"\bmy consultancy\b", r"\bget in touch to discuss\b",
    r"\bbook a (?:call|consultation|session)\b", r"\bcontact me for\b",
    r"\bdiscounted?\b", r"\bpricing\b", r"\bmy services\b",
    r"\bwe offer\b", r"\bsign up\b", r"\blimited (?:time|offer|places)\b",
    r"\bDM me\b", r"\breach out to me\b",
]

# Telling people they are wrong. Paul's rule: extend rather than correct.
CORRECTING = [
    r"\byou(?:'re| are) wrong\b", r"\bthat(?:'s| is) (?:wrong|incorrect|false)\b",
    r"\bactually,? no\b", r"\bthis is a mistake\b", r"\byou misunderstand\b",
    r"\bincorrect\b",
]

# Paul's own work. If a draft links any of these it must say whose it is.
OWN_WORK = [
    r"heartbeat-framework\.org",
    r"10\.5281/zenodo\.",
    r"\bHeartbeat Framework\b",
]

# What counts as disclosing that. Deliberately narrow: it must name him or
# say the work is his, not merely mention that a bot wrote the post.
DISCLOSURE = [
    r"\bmy own (?:paper|work|framework)\b",
    r"\bI(?:'m| am) .{0,40}\bagent\b.{0,60}\bPaul Blatherwick\b",
    r"\bPaul Blatherwick(?:'s)?\b.{0,60}\b(?:own|author|wrote|his)\b",
    r"\bauthor(?:ed)? by Paul Blatherwick\b",
    r"\bdisclosure\b.{0,120}\bPaul Blatherwick\b",
    r"\bthis is (?:his|Paul(?:'s)?) (?:own )?(?:paper|work)\b",
]

MAX_DRAFT_CHARS = 2000


@dataclass
class VoiceResult:
    ok: bool
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok


def _hits(patterns, text) -> list[str]:
    out = []
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            out.append(m.group(0))
    return out


def check(draft: str, *, links_own_work: bool | None = None) -> VoiceResult:
    """Run every rule. Returns ok=False with reasons, never raises."""
    failures, notes = [], []
    text = draft or ""

    if not text.strip():
        return VoiceResult(False, ["draft is empty"])
    if len(text) > MAX_DRAFT_CHARS:
        failures.append(f"draft is {len(text)} chars, over the {MAX_DRAFT_CHARS} limit")

    for label, pats in (("names Arkin", FORBIDDEN_NAMES),
                        ("makes a claim Paul does not hold", FORBIDDEN_CLAIMS),
                        ("reads as marketing", MARKETING),
                        ("corrects rather than extends", CORRECTING)):
        found = _hits(pats, text)
        if found:
            failures.append(f"{label}: {', '.join(repr(f) for f in found)}")

    # Disclosure is required whenever the draft points at Paul's own work.
    cites_own = bool(_hits(OWN_WORK, text)) if links_own_work is None else links_own_work
    if cites_own:
        if not _hits(DISCLOSURE, text):
            failures.append(
                "links Paul's own work without disclosing the work is his")
        else:
            notes.append("cites own work, disclosure present")

    if "heartbeat-framework.org" in text.lower() and "doi.org" not in text.lower():
        notes.append("links the site but not a DOI; a DOI is the citable form")

    return VoiceResult(not failures, failures, notes)
