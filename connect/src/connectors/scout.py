"""The scout's finds, read from its own table (Paul, 27 September: option 1).

The scout runs on its own every morning and emails Paul a digest. Until now
Vigil saw none of what it found: his only window onto the scout looked at
draft posts, a stage that is not switched on, so it showed empty lists while
the table held 46 finds. This is the read side of that link, built but not
yet wired to Vigil.

It reads; it never writes. The scout's hash chain stays the scout's own
record, separate from Vigil's ledger, which is the safer way round: the
scout finds things and Vigil thinks about them.

What comes back is typed. The source is one of the scout's known sources,
the link must be on that source's own host, and the keywords are the scout's
own configured ones. The title is someone else's words, so it is returned
under ``ui_only``: for Paul's screen, never for the planner. Authors, text and
everything else in the item stay behind.
"""

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

KIND = "aws_table"

SOURCE_HOSTS = {
    "arxiv": "arxiv.org",
    "hackernews": "news.ycombinator.com",
    "lesswrong": "www.lesswrong.com",
    "medrxiv": "www.medrxiv.org",
    "moltbook": "www.moltbook.com",
}
_ID = re.compile(r"^[A-Za-z0-9._:/-]{1,80}$")
_WORD = re.compile(r"^[\w .,'&+/-]{1,40}$")
MAX_SCAN = 1000        # the whole table today is under 100 items


def read(operation: str, params: dict, scan, now=None) -> list:
    """Finds first seen in the last ``days``, best score first.

    ``scan()`` yields the scout table's HIT items; the broker supplies it.
    """
    moment = now or datetime.now(timezone.utc)
    cutoff = moment - timedelta(days=params["days"])
    out = []
    for item in scan():
        record = _typed(item, cutoff)
        if record:
            out.append(record)
    out.sort(key=lambda r: (-(r["score"] or 0), r["first_seen"] or ""))
    return out[: params["limit"]]


def _when(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _typed(item: dict, cutoff):
    source = item.get("source")
    if source not in SOURCE_HOSTS:
        return None
    seen = _when(item.get("first_seen"))
    if seen is None or seen.tzinfo is None or seen < cutoff:
        return None
    link = _link(item.get("url"), SOURCE_HOSTS[source])
    ext = item.get("external_id")
    score = item.get("score")
    try:
        score = round(float(score), 3)
    except (TypeError, ValueError):
        score = None
    keywords = [k for k in (item.get("matched_keywords") or [])
                if isinstance(k, str) and _WORD.match(k)][:10]
    published = _when(item.get("published"))
    title = item.get("title") if isinstance(item.get("title"), str) else ""
    chain_seq = item.get("chain_seq")
    return {
        "source": source,
        "id": ext if isinstance(ext, str) and _ID.match(ext) else None,
        "score": score,
        "published": published.date().isoformat() if published else None,
        "first_seen": seen.isoformat(timespec="seconds"),
        "keywords": keywords,
        "link": link,
        "chain_seq": int(chain_seq) if chain_seq is not None else None,
        "ui_only": {"title": " ".join(title.split())[:160]},
    }


def _link(url, host):
    """The find's own page, on its source's host, as https; else None."""
    if not isinstance(url, str) or len(url) > 300:
        return None
    parts = urlsplit(url)
    if parts.hostname != host or parts.scheme not in ("http", "https") \
            or parts.username or parts.password or parts.port not in (None, 80, 443):
        return None
    return urlunsplit(("https", host, parts.path, parts.query, ""))
