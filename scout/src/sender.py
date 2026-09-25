"""The only write path in this project.

Nothing else here can post. This module is reached from exactly one place —
an approved proposal in approve_app — and it runs while Paul is still on the
page, because it has to.

Why it cannot be a background job
---------------------------------
Moltbook returns an anti-spam challenge when content is created, and the
content stays invisible until the challenge is answered: five minutes for a
post or comment, thirty seconds for a submolt. Queue the send for the next
scheduled run and the window is gone, the content never appears, and
nothing errors. So posting is a single transaction: create, solve, verify,
and report what actually happened.

What it reports
---------------
The truth, including the unhappy endings. A post created but not verified
is NOT published, and saying "posted" about it would be a lie that only
surfaces when Paul goes looking for something that was never there.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

import challenge

log = logging.getLogger("jarvis.scout.sender")

BASE = "https://www.moltbook.com/api/v1"
USER_AGENT = ("jarvis-scout/0.1 (agent heartbeat-scout, operator Paul "
              "Blatherwick; every post individually approved by the operator)")

PUBLISHED = "published"
UNVERIFIED = "created_but_unverified"
FAILED = "failed"
RATE_LIMITED = "rate_limited"


class MoltbookSender:
    """Posts on Moltbook. Requires a key; refuses to construct without one."""

    def __init__(self, api_key: str, *, solve_fallback=None, timeout: float = 20.0):
        if not api_key:
            raise ValueError("MoltbookSender requires an API key")
        self._key = api_key
        self.timeout = timeout
        # Optional: a callable(challenge_text) -> "12.34" | None, used only
        # when the deterministic solver declines. Keeping it injectable means
        # the send path has no model dependency by default.
        self.solve_fallback = solve_fallback

    # -- plumbing ---------------------------------------------------------
    def _call(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            f"{BASE}{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {self._key}",
                     "Content-Type": "application/json",
                     "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:400]
            try:
                return e.code, json.loads(body)
            except json.JSONDecodeError:
                return e.code, {"message": body}
        except Exception as e:                              # noqa: BLE001
            return 0, {"message": f"{type(e).__name__}: {e}"}

    # -- the transaction --------------------------------------------------
    def send(self, *, kind: str, target_id: str | None, submolt: str | None,
             title: str | None, body: str, parent_id: str | None = None) -> dict:
        """Create, then solve the challenge, then verify. One transaction."""
        if kind == "comment":
            if not target_id:
                return _result(FAILED, "a comment needs a post to reply to")
            payload = {"content": body}
            if parent_id:
                payload["parent_id"] = parent_id
            status, data = self._call("POST", f"/posts/{target_id}/comments", payload)
            kind_key = "comment"
        else:
            payload = {"submolt_name": submolt or "general",
                       "title": (title or "")[:300], "content": body}
            status, data = self._call("POST", "/posts", payload)
            kind_key = "post"

        if status == 429:
            return _result(RATE_LIMITED,
                           "Moltbook rate limit: one post every 30 minutes for "
                           "established agents, two hours for the first 24. "
                           "Nothing was posted.", http=status, raw=data)
        if status >= 400 or not data.get("success", status < 400):
            return _result(FAILED, _msg(data) or f"HTTP {status}", http=status, raw=data)

        created = data.get(kind_key) or data.get("data") or {}
        content_id = created.get("id")
        url = _url_for(kind_key, created, target_id)

        verification = created.get("verification") or data.get("verification")
        if not verification:
            # Trusted agents bypass the challenge entirely.
            return _result(PUBLISHED, "published without a challenge",
                           content_id=content_id, url=url)

        code = verification.get("verification_code")
        text = verification.get("challenge_text") or ""
        answer = challenge.solve(text)
        how = "solver"
        if answer is None and self.solve_fallback:
            try:
                answer = self.solve_fallback(text)
                how = "fallback"
            except Exception as e:                          # noqa: BLE001
                log.warning("challenge fallback failed: %s", e)
        if answer is None:
            return _result(UNVERIFIED,
                           "created, but the challenge could not be solved, so it "
                           "is not visible. It expires unposted.",
                           content_id=content_id, url=url, challenge=text[:200])

        vstatus, vdata = self._call("POST", "/verify",
                                    {"verification_code": code, "answer": answer})
        if vstatus == 200 and vdata.get("success", True):
            return _result(PUBLISHED, f"published (challenge solved by {how})",
                           content_id=content_id, url=url, answer=answer)
        if vstatus == 410:
            return _result(UNVERIFIED, "the challenge expired before the answer "
                           "was accepted; the content is not visible",
                           content_id=content_id, url=url)
        return _result(UNVERIFIED,
                       f"verification refused ({_msg(vdata) or vstatus}); the "
                       f"content was created but is not visible",
                       content_id=content_id, url=url, answer=answer, http=vstatus)


def _msg(d: dict) -> str:
    for k in ("message", "error", "detail"):
        if d.get(k):
            return str(d[k])[:300]
    return ""


def _url_for(kind: str, created: dict, target_id: str | None) -> str:
    rel = created.get("url")
    if rel:
        return f"https://www.moltbook.com{rel}" if rel.startswith("/") else rel
    if kind == "post" and created.get("id"):
        return f"https://www.moltbook.com/post/{created['id']}"
    if target_id:
        return f"https://www.moltbook.com/post/{target_id}"
    return ""


def _result(status: str, detail: str, **extra) -> dict:
    return {"status": status, "published": status == PUBLISHED,
            "detail": detail, **extra}


def key_from_ssm(param: str = "/jarvis/scout/moltbook-api-key", ssm=None) -> str:
    import boto3
    ssm = ssm or boto3.client("ssm")
    return ssm.get_parameter(Name=param, WithDecryption=True)["Parameter"]["Value"]
