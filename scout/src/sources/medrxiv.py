"""medRxiv new preprints via the public biorxiv/medrxiv details API.

Keyless. Returns a date range, paginated 100 at a time. The API is
chatty — it returns every preprint in the window across all subjects —
so results are filtered to the configured categories before scoring.
"""

from __future__ import annotations

from datetime import timedelta

from . import Item, http_json, sanitise, safe_url, utc, MAX_TITLE, MAX_BODY, MAX_AUTHOR

SOURCE = "medrxiv"


def fetch(cfg: dict, now, lookback_days: int, limit: int) -> list[Item]:
    endpoint = cfg["endpoint"].rstrip("/")
    wanted = {c.strip().lower() for c in cfg.get("categories", [])}
    start = (now - timedelta(days=lookback_days)).date().isoformat()
    end = now.date().isoformat()

    out: dict[str, Item] = {}
    cursor = 0
    while True:
        data = http_json(f"{endpoint}/{start}/{end}/{cursor}", timeout=30.0, retries=2)
        msgs = data.get("messages") or [{}]
        if (msgs[0].get("status") or "").lower() != "ok":
            break
        batch = data.get("collection") or []
        if not batch:
            break

        for p in batch:
            doi = str(p.get("doi") or "")
            if not doi or doi in out:
                continue
            cat = (p.get("category") or "").strip().lower()
            if wanted and cat not in wanted:
                continue
            try:
                published = utc(p.get("date"))
            except Exception:
                continue
            out[doi] = Item(
                source=SOURCE,
                external_id=doi,
                url=safe_url(f"https://www.medrxiv.org/content/{doi}v{p.get('version','1')}"),
                title=sanitise(p.get("title"), MAX_TITLE),
                author=sanitise(p.get("authors"), MAX_AUTHOR),
                published=published,
                body=sanitise(p.get("abstract"), MAX_BODY),
                extra={"category": sanitise(p.get("category"), 80),
                       "doi": sanitise(doi, 120)},
            )
            if len(out) >= limit:
                return list(out.values())

        cursor += len(batch)
        if cursor >= int(msgs[0].get("total", 0) or 0) or cursor > 3000:
            break
    return list(out.values())
