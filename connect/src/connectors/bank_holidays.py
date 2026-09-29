"""UK bank holidays from GOV.UK's published JSON (no key, no account)."""

import json
import re

HOST = "www.gov.uk"
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# The titles GOV.UK uses. A title not on this list is replaced with a fixed
# phrase rather than carried, so the feed cannot put its own words in front
# of the model.
KNOWN_TITLES = frozenset({
    "New Year’s Day", "New Year's Day", "2nd January", "St Patrick’s Day",
    "St Patrick's Day", "Good Friday", "Easter Monday", "Early May bank holiday",
    "Spring bank holiday", "Battle of the Boyne (Orangemen’s Day)",
    "Battle of the Boyne (Orangemen's Day)", "Summer bank holiday",
    "St Andrew’s Day", "St Andrew's Day", "Christmas Day", "Boxing Day",
})


def build(operation: str, params: dict) -> str:
    return f"https://{HOST}/bank-holidays.json"


def normalise(operation: str, body: bytes, params: dict) -> list:
    data = json.loads(body)
    events = ((data.get(params["division"]) or {}).get("events")) or []
    out = []
    for event in events:
        date = event.get("date")
        if not isinstance(date, str) or not _DATE.match(date):
            continue
        title = event.get("title")
        out.append({
            "date": date,
            "title": title if title in KNOWN_TITLES else "bank holiday",
            "substitute": bool(isinstance(event.get("notes"), str)
                               and "substitute" in event["notes"].lower()),
        })
    return out
