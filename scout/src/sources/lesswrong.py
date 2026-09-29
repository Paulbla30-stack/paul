"""LessWrong via its public GraphQL API.

No key required. Recent posts are pulled and filtered locally rather than
searched, because the volume is low enough that a sweep is cheaper and more
complete than a per-keyword query.
"""

from __future__ import annotations

import json
from datetime import timedelta

from . import Item, http_get, FetchError, sanitise, safe_url, utc, MAX_TITLE, MAX_BODY, MAX_AUTHOR

SOURCE = "lesswrong"

QUERY = """
{
  posts(input: {terms: {view: "new", limit: %d}}) {
    results {
      _id
      title
      postedAt
      pageUrl
      baseScore
      user { username displayName }
      contents { plaintextDescription }
    }
  }
}
"""


def fetch(cfg: dict, now, lookback_days: int, limit: int) -> list[Item]:
    endpoint = cfg["endpoint"]
    body = json.dumps({"query": QUERY % min(limit, 200)}).encode("utf-8")
    raw = http_get(
        endpoint,
        timeout=float(cfg.get("timeout_s", 30.0)),
        retries=int(cfg.get("retries", 2)),
        headers={"Content-Type": "application/json"},
        data=body,
    )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise FetchError(f"lesswrong: response was not JSON ({e})")
    if data.get("errors"):
        raise FetchError(f"lesswrong graphql errors: {str(data['errors'])[:200]}")

    cutoff = now - timedelta(days=lookback_days)
    out = []
    for p in (data.get("data", {}).get("posts", {}) or {}).get("results", []) or []:
        try:
            published = utc(p.get("postedAt"))
        except Exception:
            continue
        if published < cutoff:
            continue
        user = p.get("user") or {}
        contents = p.get("contents") or {}
        out.append(Item(
            source=SOURCE,
            external_id=str(p.get("_id") or ""),
            url=safe_url(p.get("pageUrl")),
            title=sanitise(p.get("title"), MAX_TITLE),
            author=sanitise(user.get("displayName") or user.get("username"), MAX_AUTHOR),
            published=published,
            body=sanitise(contents.get("plaintextDescription"), MAX_BODY),
            extra={"base_score": int(p.get("baseScore") or 0)},
        ))
        if len(out) >= limit:
            break
    return out
