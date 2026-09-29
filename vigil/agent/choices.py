"""Options in chat: Vigil asks, Paul taps.

Paul, 27 September 2026, after being asked in a coding session what "redo
the website" meant and choosing from three options: "This is actually
interesting... Can you build in the option window into the chat."

When a reply turns on a choice only Paul can make -- two readings of what he
said, or routes that genuinely differ -- Vigil may end the reply with one
fenced block:

    ```choices
    {"question": "...", "options": [{"label": "...", "description": "..."}, ...],
     "multi": false}
    ```

This module takes that block out of the reply and checks it. The page shows
it as tappable options with a free-text "Other" beside them, and whatever
Paul picks goes back as an ordinary message from him. So an option carries
no authority of its own: choosing one is Paul saying it, and nothing runs
because a button was pressed. Everything in the block is model output, so
it is length-capped, stripped of invisible and direction-changing
characters, and shown as plain text. A block that does not check out is
dropped, and the reply is still shown without it.
"""

import json
import re
import unicodedata
from typing import Optional, Tuple

BLOCK = re.compile(r"```choices[ \t]*\n(.*?)\n?```", re.S)
MIN_OPTIONS, MAX_OPTIONS = 2, 4
MAX_QUESTION, MAX_LABEL, MAX_DESCRIPTION = 300, 60, 200


def _plain(text, limit: int) -> str:
    """One line, no invisible or bidi characters, capped."""
    cleaned = "".join(ch for ch in str(text or "")
                      if unicodedata.category(ch) not in ("Cf", "Cc", "Co", "Cs"))
    return " ".join(cleaned.split())[:limit]


def check(raw) -> Optional[dict]:
    """A valid choices dict, or None."""
    if not isinstance(raw, dict):
        return None
    question = _plain(raw.get("question"), MAX_QUESTION)
    options = raw.get("options")
    if not question or not isinstance(options, list):
        return None
    out, seen = [], set()
    for item in options[:MAX_OPTIONS]:
        if isinstance(item, str):
            item = {"label": item}
        if not isinstance(item, dict):
            continue
        label = _plain(item.get("label"), MAX_LABEL)
        if not label or label.lower() in seen:
            continue
        seen.add(label.lower())
        out.append({"label": label, "description": _plain(item.get("description"), MAX_DESCRIPTION)})
    if len(out) < MIN_OPTIONS:
        return None
    return {"question": question, "options": out, "multi": bool(raw.get("multi"))}


def split(answer: Optional[str]) -> Tuple[Optional[str], Optional[dict]]:
    """(reply without the block, checked choices or None).

    Only the first block is used; any others are removed too, so a reply
    cannot smuggle a second set of options past the first.
    """
    if not answer or "```choices" not in answer:
        return answer, None
    found = BLOCK.search(answer)
    text = BLOCK.sub("", answer).strip()
    if found is None:
        return text or answer, None
    try:
        raw = json.loads(found.group(1))
    except ValueError:
        return text, None
    return text, check(raw)


PROMPT = (
    "When your reply turns on a choice only the operator can make -- two readings of what he "
    "asked, or routes that genuinely differ -- you may end the reply with one fenced block "
    "tagged choices, holding JSON: {\"question\": str, \"options\": [{\"label\": str, "
    "\"description\": str}], \"multi\": bool}. Two to four options, labels of a few words, one "
    "sentence each saying what that choice means. He can always answer in his own words "
    "instead. Use it only for a real fork, never for yes/no, never to ask permission for "
    "something he has already asked for, and at most once per reply.")
