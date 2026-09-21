"""Source fetchers.

EVERYTHING RETURNED FROM THESE MODULES IS UNTRUSTED DATA.

It is written by strangers on the public internet. It is never executed,
never interpolated into a query or a command, and in Stage 2 it must only
ever reach a model inside an explicitly delimited block marked as data.
`sanitise()` below is the single choke point: every field of every item
passes through it before anything else in the scout sees it.

What sanitise() does and does not do:
  - strips C0/C1 control characters, which is what would let fetched text
    forge line structure in the digest email or the log
  - collapses runs of whitespace, including the zero-width and directional
    marks used to hide text inside an innocuous-looking string
  - caps length, so one item cannot flood the table or the email
It does NOT attempt to detect prompt injection. That is not a solvable
filtering problem, and pretending otherwise would be worse than useless.
The defence is structural: Stage 1 has no model at all, and Stage 2 puts
this text in a data block and tells the model it is data.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

USER_AGENT = (
    "jarvis-scout/0.1 (personal research tool; "
    "contact paulblatherwick@heartbeat-framework.org)"
)

MAX_TITLE = 300
MAX_BODY = 4000
MAX_AUTHOR = 120
MAX_URL = 500

# C0 and C1 control characters, except tab/newline which we fold to space.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# Zero-width and bidirectional formatting characters.
_INVISIBLE = re.compile(r"[​-‏‪-‮⁠-⁤﻿]")
_WS = re.compile(r"\s+")


def sanitise(value, limit: int = MAX_BODY) -> str:
    if value is None:
        return ""
    s = str(value)
    s = unicodedata.normalize("NFC", s)
    s = _CONTROL.sub(" ", s)
    s = _INVISIBLE.sub("", s)
    s = _WS.sub(" ", s).strip()
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def safe_url(value) -> str:
    """Only http(s) survives. Anything else becomes empty.

    A javascript: or data: URL in a digest email is a link Paul might click.
    """
    s = sanitise(value, MAX_URL)
    if not s:
        return ""
    low = s.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        return ""
    return s


@dataclass
class Item:
    source: str
    external_id: str
    url: str
    title: str
    author: str
    published: datetime
    body: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"HIT#{self.source}#{self.external_id}"


class FetchError(Exception):
    pass


# Codes that mean "you are going too fast", not "this is broken".
# arXiv answers 406 when throttling, which is unusual but real: observed
# 21 Sep 2026, then 12/12 identical requests succeeded once spaced out.
THROTTLE_CODES = {406, 408, 429, 500, 502, 503, 504}


def http_get(url: str, *, timeout: float = 25.0, retries: int = 2,
             headers: dict | None = None, data: bytes | None = None,
             backoff: float = 1.5) -> bytes:
    """GET (or POST when data is given), backing off on throttling.

    Two sources need this for different reasons. LessWrong's endpoint times
    out on a cold first call and then answers in under a second. arXiv
    returns 406 under load; spacing the requests out clears it.
    """
    hdrs = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs, data=data)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in THROTTLE_CODES or attempt >= retries:
                raise FetchError(f"{url}: HTTP {e.code}")
            time.sleep(backoff * (2 ** attempt))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
    raise FetchError(f"{url}: {last}")


def http_json(url: str, **kw) -> dict:
    raw = http_get(url, **kw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise FetchError(f"{url}: response was not JSON ({e})")


def utc(ts) -> datetime:
    """Parse the several date shapes these APIs return, always to aware UTC."""
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    s = str(ts).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
            try:
                d = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        else:
            raise FetchError(f"unparseable timestamp: {ts!r}")
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
