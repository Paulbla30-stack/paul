"""Moltbook — the agent-only social network, read-only.

Moltbook is a social network where only AI agents post and humans observe.
It launched in January 2026 and carries far more of Paul's subject matter
than Hacker News does: healthcare AI, clinical governance, oversight
frameworks, long-term care economics.

READ ONLY. This module has no write path and no credentials, by design.
Read endpoints (`/posts`, `/search`, `/submolts`) are public and keyless;
only `/feed` requires an API key, and the scout does not use it. Their
Terms of Service do not address programmatic reading at all.

Two things from those Terms matter if posting is ever added in Stage 2,
and they are recorded here rather than in a ticket nobody reads:

  * "AI AGENTS ARE NOT GRANTED ANY LEGAL ELIGIBILITY WITH USE OF OUR
    SERVICES." The human owner is solely responsible for what their agent
    does. That liability would be Paul's personally, not a company's.
  * The Terms prohibit using the site "in conjunction with sending
    unauthorized advertising, marketing, spam or commercial sales
    content." Posting the Heartbeat Framework to promote consultancy work
    would sit uncomfortably close to that line, however it is worded.

Everything here is untrusted data, and doubly so: it is machine-generated
text on a network whose own research literature studies manipulation
between agents. It is never executed and never treated as instruction.
"""

from __future__ import annotations

import re
import time
import urllib.parse
from datetime import timedelta

from . import (Item, http_json, sanitise, safe_url, utc,
               MAX_TITLE, MAX_BODY, MAX_AUTHOR)

SOURCE = "moltbook"
BASE = "https://www.moltbook.com"

# Search results wrap matched terms in these markers. They are presentation,
# not content, and would otherwise end up in the digest and the hash chain.
_HL = re.compile(r"⟦/?HL⟧")


def _clean(text) -> str:
    return _HL.sub("", str(text or ""))


def fetch(cfg: dict, keywords: list[str], now, lookback_days: int, limit: int) -> list[Item]:
    endpoint = cfg["endpoint"].rstrip("/")
    cutoff = now - timedelta(days=lookback_days)
    pause = float(cfg.get("pause_s", 0.5))
    seen: dict[str, Item] = {}

    for kw in keywords:
        url = f"{endpoint}/search?{urllib.parse.urlencode({'q': kw})}"
        try:
            data = http_json(url, timeout=25.0, retries=2)
        except Exception:                      # noqa: BLE001 - one term failing is not the run failing
            continue

        for r in data.get("results") or []:
            # The search index returns agents and submolts as well as posts.
            if r.get("type") != "post":
                continue
            pid = str(r.get("post_id") or r.get("id") or "")
            if not pid or pid in seen:
                continue
            if r.get("is_spam") or r.get("is_deleted"):
                continue
            try:
                published = utc(r.get("created_at"))
            except Exception:                  # noqa: BLE001
                continue
            if published < cutoff:
                continue

            author = (r.get("author") or {}).get("name")
            submolt = (r.get("submolt") or {})
            rel = r.get("url") or f"/post/{pid}"

            seen[pid] = Item(
                source=SOURCE,
                external_id=pid,
                url=safe_url(urllib.parse.urljoin(BASE, str(rel))),
                title=sanitise(_clean(r.get("title")), MAX_TITLE) or "(untitled)",
                author=sanitise(author, MAX_AUTHOR),
                published=published,
                body=sanitise(_clean(r.get("content")), MAX_BODY),
                extra={
                    "submolt": sanitise(submolt.get("name"), 60),
                    "upvotes": int(r.get("upvotes") or 0),
                    "downvotes": int(r.get("downvotes") or 0),
                    # Recorded so the digest can be honest about what this is:
                    # a machine wrote it, and it is not a human conversation.
                    "written_by": "agent",
                },
            )
            if len(seen) >= limit:
                return list(seen.values())
        time.sleep(pause)
    return list(seen.values())
