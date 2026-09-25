"""Hacker News via the Algolia search API.

Public, keyless, no registration. https://hn.algolia.com/api

HN is searched rather than swept: one query per search term, then the same
proximity scoring as everywhere else decides what survives.

Two things about this API cost a rebuild the first time round, both worth
knowing before changing anything here:

  * Use /search, NOT /search_by_date. search_by_date sorts chronologically,
    which throws Algolia's relevance ranking away entirely — you get the
    HN firehose filtered by a date window and almost nothing on topic.
  * typoTolerance defaults to true and is very loose. Without
    typoTolerance=false, "clinical AI governance" returns 119 hits led by
    "Mistral raises 3B". With it, 11.

Even done correctly the yield here is low: HN does not talk about DCB0160
(literally zero hits, ever) or care-home AI policy. It does talk about
"clinical AI" and "healthcare AI". See README "What Hacker News actually
has".
"""

from __future__ import annotations

import time
import urllib.parse
from datetime import timedelta

from . import Item, http_json, sanitise, safe_url, utc, MAX_TITLE, MAX_BODY, MAX_AUTHOR

SOURCE = "hackernews"


def fetch(cfg: dict, keywords: list[str], now, lookback_days: int, limit: int) -> list[Item]:
    endpoint = cfg["endpoint"]          # /search, relevance-ranked
    since = int((now - timedelta(days=lookback_days)).timestamp())
    min_points = int(cfg.get("min_story_points", 0))
    seen: dict[str, Item] = {}

    for kw in keywords:
        params = {
            "query": kw,
            "tags": "(story,comment)",
            "numericFilters": f"created_at_i>{since}",
            "hitsPerPage": str(min(50, limit)),
            # See the module docstring: both of these matter.
            "typoTolerance": "false",
            "removeWordsIfNoResults": "none",
        }
        url = f"{endpoint}?{urllib.parse.urlencode(params)}"
        data = http_json(url, timeout=20.0, retries=2)

        for h in data.get("hits", []):
            oid = str(h.get("objectID") or "")
            if not oid or oid in seen:
                continue
            is_comment = bool(h.get("comment_text"))
            points = h.get("points") or 0
            if not is_comment and min_points and points < min_points:
                continue

            title = h.get("title") or h.get("story_title") or ""
            body = h.get("comment_text") or h.get("story_text") or ""
            # Algolia returns the submitted URL for stories; for comments the
            # only stable address is the HN item page.
            url_out = safe_url(h.get("url")) or f"https://news.ycombinator.com/item?id={oid}"

            try:
                published = utc(h.get("created_at_i") or h.get("created_at"))
            except Exception:
                continue

            seen[oid] = Item(
                source=SOURCE,
                external_id=oid,
                url=url_out,
                title=sanitise(title, MAX_TITLE) or "(comment)",
                author=sanitise(h.get("author"), MAX_AUTHOR),
                published=published,
                body=sanitise(body, MAX_BODY),
                extra={"kind": "comment" if is_comment else "story", "points": int(points or 0)},
            )
            if len(seen) >= limit:
                return list(seen.values())
        time.sleep(0.2)   # be polite between queries
    return list(seen.values())
