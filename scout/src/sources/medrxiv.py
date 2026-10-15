"""medRxiv new preprints via the public biorxiv/medrxiv details API.

Keyless. Returns a date range, paginated 100 at a time. The API is
chatty — it returns every preprint in the window across all subjects —
so results are filtered to the configured categories before scoring.
"""

from __future__ import annotations

import logging

from datetime import timedelta

from . import (BudgetExpired, FetchError, Item, MAX_AUTHOR, MAX_BODY, MAX_TITLE,
               http_json, note_partial, safe_url, sanitise, utc)

log = logging.getLogger("scout.medrxiv")

SOURCE = "medrxiv"


def fetch(cfg: dict, now, lookback_days: int, limit: int) -> list[Item]:
    endpoint = cfg["endpoint"].rstrip("/")
    wanted = {c.strip().lower() for c in cfg.get("categories", [])}
    start = (now - timedelta(days=lookback_days)).date().isoformat()
    end = now.date().isoformat()

    out: dict[str, Item] = {}
    cursor = 0
    page = 0
    while True:
        page += 1
        try:
            data = http_json(f"{endpoint}/{start}/{end}/{cursor}",
                             timeout=30.0, retries=2)
        except BudgetExpired as why:
            if page == 1:
                raise                   # nothing was read: a failure, not a truncation
            # Stop with what has been gathered rather than losing the lot.
            # collect() reads budget.tripped and reports the source as
            # truncated, because a partial sweep presented as a complete one
            # is the failure this codebase keeps coming back to.
            log.warning("medrxiv: %s; returning %d of an unknown total",
                        why, len(out))
            break
        except Exception as e:          # noqa: BLE001
            if page == 1:
                raise
            # A later page failing keeps the earlier pages, and says so.
            note_partial(f"page {page} failed: {type(e).__name__}: {str(e)[-60:]}")
            log.warning("medrxiv: page %d failed, keeping %d items: %s",
                        page, len(out), e)
            break
        msgs = (data.get("messages") if isinstance(data, dict) else None) or [{}]
        status = str((msgs[0] or {}).get("status") or "").strip().lower()
        if status == "no posts found" and page == 1:
            # The API's own answer for an empty window. Not an error.
            break
        if status != "ok":
            # This used to break silently: a refusal, or an empty {}, came
            # back as zero items and the source was listed as ok.
            if page == 1:
                raise FetchError(f"medrxiv: status {status or 'missing'!r} on the first page")
            note_partial(f"page {page} answered status {status or 'missing'!r}")
            log.warning("medrxiv: page %d answered status %r, keeping %d items",
                        page, status, len(out))
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
