"""arXiv new submissions.

Two ways in, and the order matters.

**RSS is primary** (`https://rss.arxiv.org/rss/<category>`). It is the
endpoint arXiv publishes for exactly this question — what was announced in
this category — and it is reliable. It carries title, abstract, authors,
date and an `announce_type` that separates genuinely new papers from
revisions of old ones.

**The Atom API is the fallback**, used only when a lookback wider than the
RSS window is asked for, because it is the only way to reach back weeks.
It is also the flakier of the two: under any load it answers `406 Not
Acceptable` rather than 429, and a request with `max_results=100` and
`sortBy=submittedDate` will 406 while a `max_results=1` request to the same
host succeeds seconds later. A failure here is logged and the RSS results
are kept rather than the run failing.

Two things that cost real time and must not be undone:

  * The API endpoint **must be https**. The `http://` form returns HTTP 200
    with a well-formed but EMPTY feed — indistinguishable from "no new
    papers" unless you are looking for it.
  * arXiv asks for no more than one request every three seconds. Honoured
    between category queries.
"""

from __future__ import annotations

import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import timedelta
from email.utils import parsedate_to_datetime

from . import (Item, FetchError, http_get, sanitise, safe_url, utc,
               MAX_TITLE, MAX_BODY, MAX_AUTHOR)

SOURCE = "arxiv"
ATOM = "{http://www.w3.org/2005/Atom}"
RSS_BASE = "https://rss.arxiv.org/rss"

# "new" is a first announcement, "cross" is a cross-list into this category
# (new to a reader of it), "replace" is a revision of something already seen.
KEEP_ANNOUNCE = {"new", "cross", "cross-list"}


def _abstract(description: str) -> str:
    """RSS descriptions are 'arXiv:ID Announce Type: new  Abstract: ...'."""
    d = description or ""
    i = d.find("Abstract:")
    return d[i + 9:] if i >= 0 else d


def _from_rss(cat: str, cutoff, limit: int) -> list[Item]:
    raw = http_get(f"{RSS_BASE}/{urllib.parse.quote(cat)}", timeout=25.0, retries=2)
    root = ET.fromstring(raw)
    ch = root.find("channel")
    if ch is None:
        return []
    out = []
    for e in ch.findall("item"):
        ann = (e.findtext("{http://arxiv.org/schemas/atom}announce_type")
               or e.findtext("announce_type") or "new").strip().lower()
        if ann not in KEEP_ANNOUNCE:
            continue
        link = e.findtext("link") or ""
        aid = link.rsplit("/", 1)[-1]
        if not aid:
            continue
        try:
            published = parsedate_to_datetime(e.findtext("pubDate"))
            published = utc(published.isoformat())
        except Exception:                                   # noqa: BLE001
            continue
        if published < cutoff:
            continue
        creator = (e.findtext("{http://purl.org/dc/elements/1.1/}creator")
                   or e.findtext("creator") or "")
        out.append(Item(
            source=SOURCE, external_id=aid, url=safe_url(link),
            title=sanitise(e.findtext("title"), MAX_TITLE),
            author=sanitise(creator, MAX_AUTHOR),
            published=published,
            body=sanitise(_abstract(e.findtext("description")), MAX_BODY),
            extra={"categories": [cat], "announce_type": ann, "via": "rss"},
        ))
        if len(out) >= limit:
            break
    return out


def _from_api(endpoint: str, cat: str, cutoff, limit: int, interval: float) -> list[Item]:
    if not endpoint.startswith("https://"):
        raise ValueError("arxiv endpoint must be https; http returns an empty feed")
    params = {"search_query": f"cat:{cat}", "start": "0",
              "max_results": str(min(100, limit)),
              "sortBy": "submittedDate", "sortOrder": "descending"}
    raw = http_get(f"{endpoint}?{urllib.parse.urlencode(params)}",
                   timeout=30.0, retries=4, backoff=interval)
    root = ET.fromstring(raw)
    out = []
    for e in root.findall(f"{ATOM}entry"):
        aid = (e.findtext(f"{ATOM}id") or "").strip()
        if not aid:
            continue
        try:
            published = utc(e.findtext(f"{ATOM}published"))
        except Exception:                                   # noqa: BLE001
            continue
        if published < cutoff:
            continue
        authors = [sanitise(a.findtext(f"{ATOM}name"), 80) for a in e.findall(f"{ATOM}author")]
        cats = [c.get("term") for c in e.findall(f"{ATOM}category") if c.get("term")]
        out.append(Item(
            source=SOURCE, external_id=aid.rsplit("/", 1)[-1], url=safe_url(aid),
            title=sanitise(e.findtext(f"{ATOM}title"), MAX_TITLE),
            author=sanitise(", ".join(a for a in authors if a)[:MAX_AUTHOR], MAX_AUTHOR),
            published=published,
            body=sanitise(e.findtext(f"{ATOM}summary"), MAX_BODY),
            extra={"categories": cats[:8], "via": "api"},
        ))
    return out


def fetch(cfg: dict, now, lookback_days: int, limit: int) -> list[Item]:
    cutoff = now - timedelta(days=lookback_days)
    interval = float(cfg.get("min_interval_s", 3.0))
    cats = cfg["categories"]
    per_cat = max(1, limit // max(1, len(cats)))
    # RSS carries roughly one announcement day. Past that, the API is the
    # only way back, so it is tried as well and its failure is tolerated.
    wide = lookback_days > float(cfg.get("rss_covers_days", 2))

    out: dict[str, Item] = {}
    errors = []
    for i, cat in enumerate(cats):
        if i:
            time.sleep(interval)
        try:
            for it in _from_rss(cat, cutoff, per_cat):
                out.setdefault(it.external_id, it)
        except Exception as e:                              # noqa: BLE001
            errors.append(f"rss {cat}: {type(e).__name__}")

        if wide:
            try:
                time.sleep(interval)
                for it in _from_api(cfg["endpoint"], cat, cutoff, per_cat, interval):
                    out.setdefault(it.external_id, it)
            except Exception as e:                          # noqa: BLE001
                errors.append(f"api {cat}: {str(e)[-40:]}")

    if not out and errors:
        raise FetchError("; ".join(errors[:4]))
    return list(out.values())
