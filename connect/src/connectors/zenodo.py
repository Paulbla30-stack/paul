"""Metadata of Paul's own Zenodo records (public API, no key).

Only the record ids in the manifest can be asked for. Only fields that are
facts about the record come back: its DOI, dates, version, licence and the
counts Zenodo publishes. The title is Paul's own, but it is still text from
outside the box, so it is capped and marked as the record's title rather
than something anyone said.
"""

import json
import re

HOST = "zenodo.org"
_DOI = re.compile(r"^10\.5281/zenodo\.\d{1,12}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def build(operation: str, params: dict) -> str:
    return f"https://{HOST}/api/records/{params['record']}"


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _date(value):
    return value[:10] if isinstance(value, str) and _DATE.match(value) else None


def normalise(operation: str, body: bytes, params: dict) -> list:
    data = json.loads(body)
    meta = data.get("metadata") or {}
    stats = data.get("stats") or {}
    doi = data.get("doi") if isinstance(data.get("doi"), str) and _DOI.match(data["doi"]) else None
    title = meta.get("title") if isinstance(meta.get("title"), str) else ""
    licence = (meta.get("license") or {}).get("id") if isinstance(meta.get("license"), dict) else None
    return [{
        "record": params["record"],
        "doi": doi,
        "title": " ".join(title.split())[:160],
        "version": str(meta.get("version"))[:20] if meta.get("version") is not None else None,
        "published": _date(meta.get("publication_date")),
        "updated": _date(data.get("updated")),
        "licence": licence if isinstance(licence, str) and len(licence) <= 40 else None,
        "views": _int(stats.get("unique_views")),
        "downloads": _int(stats.get("unique_downloads")),
    }]
