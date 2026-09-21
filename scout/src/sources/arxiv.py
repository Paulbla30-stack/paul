"""arXiv new submissions via the public Atom API.

MUST be https. The http:// form of export.arxiv.org/api/query returns
HTTP 200 with a well-formed but EMPTY feed — indistinguishable from "no new
papers" unless you are looking for it. Confirmed 21 Sep 2026.

arXiv asks for no more than one request every three seconds, honoured here
between category queries.
"""

from __future__ import annotations

import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import timedelta

from . import Item, http_get, sanitise, safe_url, utc, MAX_TITLE, MAX_BODY, MAX_AUTHOR

SOURCE = "arxiv"
ATOM = "{http://www.w3.org/2005/Atom}"


def fetch(cfg: dict, now, lookback_days: int, limit: int) -> list[Item]:
    endpoint = cfg["endpoint"]
    if not endpoint.startswith("https://"):
        raise ValueError(
            "arxiv endpoint must be https; the http form returns an empty feed"
        )
    interval = float(cfg.get("min_interval_s", 3.0))
    cutoff = now - timedelta(days=lookback_days)
    per_cat = max(1, limit // max(1, len(cfg["categories"])))
    out: dict[str, Item] = {}

    for i, cat in enumerate(cfg["categories"]):
        if i:
            time.sleep(interval)
        params = {
            "search_query": f"cat:{cat}",
            "start": "0",
            "max_results": str(min(100, per_cat)),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        raw = http_get(f"{endpoint}?{urllib.parse.urlencode(params)}",
                       timeout=30.0, retries=4, backoff=interval)
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            continue

        for e in root.findall(f"{ATOM}entry"):
            aid = (e.findtext(f"{ATOM}id") or "").strip()
            if not aid:
                continue
            short = aid.rsplit("/", 1)[-1]
            if short in out:
                continue
            try:
                published = utc(e.findtext(f"{ATOM}published"))
            except Exception:
                continue
            if published < cutoff:
                continue
            authors = [
                sanitise(a.findtext(f"{ATOM}name"), 80)
                for a in e.findall(f"{ATOM}author")
            ]
            cats = [c.get("term") for c in e.findall(f"{ATOM}category") if c.get("term")]
            out[short] = Item(
                source=SOURCE,
                external_id=short,
                url=safe_url(aid),
                title=sanitise(e.findtext(f"{ATOM}title"), MAX_TITLE),
                author=sanitise(", ".join(a for a in authors if a)[:MAX_AUTHOR], MAX_AUTHOR),
                published=published,
                body=sanitise(e.findtext(f"{ATOM}summary"), MAX_BODY),
                extra={"categories": cats[:8]},
            )
    return list(out.values())
