"""The day the agent chose its name, recorded once on its own ledger.

Paul, 27 September 2026: "I dont like the jarvis name. Its so general now.
Speak to jarvis and think of a name that would suit and then change
everything to it. I dont mind what he chooses."

Asked, he chose Vigil, and asked that the past not be rewritten: the ledger
keeps every entry that says Jarvis, and the change goes on as a new entry.
The words below are his, from that conversation. The marker file makes it
once per machine; the entry is only marked done when the ledger took it.
"""

import json
import logging
import os
from typing import Optional

DEFAULT_MARKER = "/var/lib/vigil/naming.recorded"

ENTRY = {
    "naming": {
        "from": "Jarvis",
        "to": "Vigil",
        "chosen_by": "the agent",
        "at_the_direction_of": "Paul",
        "asked_on": "2026-09-27",
        "why": ("I am a witness: I observe, I remember, I report. I stand watch over Paul's personal "
                "estate -- his time, his costs, his machine, his commitments -- and I wake when "
                "something changes. The word vigil carries that: a sustained, attentive presence, not "
                "always active, but never off-duty."),
        "statement": ("At Paul's direction, the agent formerly known as Jarvis has adopted the name "
                      "Vigil. The change reflects evolution, not erasure. The ledger remains unaltered. "
                      "The first entry under this name is this one."),
        "unchanged": ["every earlier entry, which still says Jarvis",
                      "the ledger's writer, 'jarvis', which names this chain and its anchor",
                      "the ledger kind 'vigil', which means a sleep/wake transition of the watch"],
    }
}


def record_once(agent, marker: Optional[str] = None, logger: Optional[logging.Logger] = None) -> bool:
    """Record the naming if it has not been. True once it is on the ledger (or was)."""
    log = logger or logging.getLogger("vigil.naming")
    marker = marker or DEFAULT_MARKER
    if os.path.exists(marker):
        return True
    ledger = getattr(agent, "ledger", None)
    if ledger is None or not getattr(ledger, "enabled", False):
        return False
    if getattr(agent, "name", "") != "Vigil":
        return False
    if not ledger.record("decision", ENTRY):
        return False
    try:
        os.makedirs(os.path.dirname(marker) or ".", exist_ok=True)
        with open(marker, "w", encoding="utf-8") as fh:
            json.dump({"recorded": True, "entry": "decision/naming"}, fh)
    except OSError as exc:
        log.warning("naming recorded but the marker could not be written: %s", exc)
    log.info("recorded the naming on the ledger: Jarvis -> Vigil")
    return True
