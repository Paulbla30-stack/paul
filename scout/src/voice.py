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

# Paul is a sole author. "We published" is a small falsehood that makes the
# estate sound like an organisation, which is the opposite of what it is.
# Written by the first clean draft, so it is not hypothetical.
PLURAL_AUTHOR = [
    r"\bwe (?:published|wrote|produced|developed|built|created)\b",
    r"\bour (?:paper|framework|research|work|instrument)\b",
    r"\bthe team (?:published|wrote|behind)\b",
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
    # "This is the author's own paper" — written by the first real draft and
    # missed by the patterns below, which only knew "his" and "Paul's".
    r"\bthe author(?:'s|\u2019s)? own (?:paper|work|framework)\b",
    r"\bauthor(?:'s|\u2019s)? own (?:paper|work)\b",
    r"\bI(?:'m| am) .{0,40}\bagent\b.{0,60}\bPaul Blatherwick\b",
    r"\bPaul Blatherwick(?:'s)?\b.{0,60}\b(?:own|author|wrote|his)\b",
    r"\bauthor(?:ed)? by Paul Blatherwick\b",
    r"\bdisclosure\b.{0,120}\bPaul Blatherwick\b",
    r"\bthis is (?:his|Paul(?:'s)?) (?:own )?(?:paper|work)\b",
]

# Causal and efficacy claims. The rule came from the agent, reviewing this
# design: "no modality may introduce causal claims not explicitly in the
# source". One rule covering text, image and audio is better than a separate
# judgement per medium, and it is the failure mode that matters most on a
# public page read by patients rather than researchers.
#
# What this check can and cannot do, stated plainly because the module's own
# doctrine is that a rule which only mostly holds invites trusting the output:
#
#   IT CANNOT verify a claim against the source. No regex can. That comparison
#   needs the paper in hand, and it is the operator's job at the approval gate.
#   IT CAN require that a claim about clinical effect be ATTRIBUTED. "This
#   could change how we treat depression" asserts efficacy in Paul's own
#   voice; "the authors propose a model for depression treatment" reports
#   what a paper says. The second is defensible on the register he posts
#   under; the first is an endorsement he is not making.
#
# So: an efficacy or practice-change phrase with no attribution anywhere in
# the draft fails. Attributed, it passes and the truth of it is Paul's call.
EFFICACY = [
    r"\b(?:could|will|would|should|may)\s+(?:change|transform|revolutionise|revolutionize)\s+(?:how|the way)\b",
    r"\bclinicians?\s+should\b", r"\bnurses?\s+should\b", r"\bdoctors?\s+should\b",
    r"\bshould\s+be\s+(?:used|adopted|deployed|implemented|prescribed)\b",
    r"\b(?:proves|demonstrates|shows)\s+that\b",
    r"\bis\s+(?:effective|safe|safer|superior|better)\s+(?:for|at|than|in)\b",
    r"\b(?:reduces|prevents|cures|eliminates|improves)\s+(?:risk|harm|mortality|outcomes|symptoms)\b",
    r"\brecommended\s+for\b",
    r"\bevidence\s+shows\b",
]

# What turns an assertion into a report of one. Deliberately broad: the point
# is to catch the draft speaking in Paul's own voice, not to police citation
# style.
ATTRIBUTION = [
    r"\bthe\s+(?:authors?|paper|study|trial|review|preprint|article)\b",
    r"\bthey\s+(?:report|propose|find|found|argue|suggest|conclude)\b",
    r"\baccording\s+to\b", r"\breports?\s+that\b", r"\bproposes?\s+(?:a|an|that)\b",
    r"\bfinds?\s+that\b", r"\bargues?\s+that\b", r"\bsuggests?\s+that\b",
    r"\bconcludes?\s+that\b", r"\bclaims?\s+that\b",
]

MAX_DRAFT_CHARS = 2000

# Any DOI that looks like a Zenodo record.
_DOI = re.compile(r"10\.5281/zenodo\.\d+", re.IGNORECASE)
_ANY_DOI = re.compile(r"\b10\.\d{4,9}/[^\s,;)\]]+", re.IGNORECASE)


def _own_dois() -> set[str]:
    """The DOIs a draft may present as Paul's own work, from config.toml."""
    import os
    import tomllib
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "config.toml")
    try:
        with open(path, "rb") as fh:
            cfg = tomllib.load(fh)
            return {r["doi"].lower() for r in cfg["citations"]["records"]}
    except Exception:                                    # noqa: BLE001
        return set()


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


def check(draft: str, *, links_own_work: bool | None = None,
          known_dois: set[str] | None = None) -> VoiceResult:
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
                        ("speaks as a group; Paul is a sole author", PLURAL_AUTHOR),
                        ("corrects rather than extends", CORRECTING)):
        found = _hits(pats, text)
        if found:
            failures.append(f"{label}: {', '.join(repr(f) for f in found)}")

    # An efficacy claim in Paul's own voice, with nothing attributing it.
    efficacy = _hits(EFFICACY, text)
    if efficacy and not _hits(ATTRIBUTION, text):
        failures.append(
            "asserts clinical effect or practice change in Paul's own voice, "
            "unattributed: " + ", ".join(repr(f) for f in efficacy))
    elif efficacy:
        notes.append("makes an attributed claim about effect; "
                     "whether the source supports it is the operator's check")

    # Disclosure is required whenever the draft points at Paul's own work.
    cites_own = bool(_hits(OWN_WORK, text)) if links_own_work is None else links_own_work
    if cites_own:
        if not _hits(DISCLOSURE, text):
            failures.append(
                "links Paul's own work without disclosing the work is his")
        else:
            notes.append("cites own work, disclosure present")

    # Citations. A fabricated DOI is the worst thing a draft can contain,
    # because a wrong one may still RESOLVE — to a real record belonging to
    # somebody else — and then reads as a genuine citation for as long as
    # the post exists.
    allowed = _own_dois() if known_dois is None else {d.lower() for d in known_dois}
    cited = {m.group(0).lower() for m in _ANY_DOI.finditer(text)}
    if cited and allowed:
        unknown = sorted(d for d in cited if d not in allowed)
        if unknown:
            failures.append(
                "cites a DOI that is not one of Paul's records: "
                + ", ".join(unknown[:3]))
    elif cited and not allowed:
        failures.append("cites a DOI but the allowed list could not be read; "
                        "refusing rather than guessing")

    if "heartbeat-framework.org" in text.lower() and "doi.org" not in text.lower():
        notes.append("links the site but not a DOI; a DOI is the citable form")

    return VoiceResult(not failures, failures, notes)
