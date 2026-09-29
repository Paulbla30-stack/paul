"""Signed approval tokens.

A token in a digest email is a capability: whoever holds it can act. So it
is HMAC-signed, scoped to one proposal and one action, and it expires.

The signing key lives in SSM Parameter Store as a SecureString. It is the
first secret this project has needed — Stage 1 used none, because every
source is a public keyless API.

Two properties matter more than the cryptography:

  * **A GET never changes anything.** Mail clients and security scanners
    follow links in email to check them. Outlook and Gmail both do. A
    one-click GET approve URL would be fired by the scanner before Paul
    ever saw the message, and the post would go out on its own. So GET
    renders a page showing the draft, and approving is a POST from that
    page. This is not belt-and-braces; without it the approval gate is
    decorative.
  * **A token is not the decision.** Holding a valid token only lets you
    ask. The proposal's own status is checked at decision time, so a
    replayed or forwarded token cannot approve something twice, and cannot
    approve something already rejected or expired.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

DEFAULT_TTL_S = 7 * 86_400          # a week to decide, then it lapses
# "decide" is what the digest link carries: it authorises Paul to make a
# decision on this proposal, and the decision itself (approve or reject)
# comes from the form he submits. One link per proposal, not two, and the
# email never contains a URL that means "approve".
_ACTIONS = ("decide", "approve", "reject")


class BadToken(Exception):
    pass


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def mint(secret: str, proposal_id: str, action: str = "decide", *, ttl_s: int = DEFAULT_TTL_S,
         now: float | None = None) -> str:
    if action not in _ACTIONS:
        raise ValueError(f"action must be one of {_ACTIONS}")
    payload = {"p": proposal_id, "a": action,
               "x": int((now or time.time()) + ttl_s)}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).digest()
    return f"{_b64e(raw)}.{_b64e(sig)}"


def verify(secret: str, token: str, *, now: float | None = None) -> dict:
    """Return the payload, or raise BadToken. Never returns a partial result."""
    if not token or "." not in token:
        raise BadToken("malformed")
    body, sig = token.rsplit(".", 1)
    try:
        raw = _b64d(body)
        given = _b64d(sig)
    except Exception:                                    # noqa: BLE001
        raise BadToken("malformed")

    expected = hmac.new(secret.encode(), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(given, expected):         # constant time
        raise BadToken("signature")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise BadToken("malformed")
    if payload.get("a") not in _ACTIONS:
        raise BadToken("action")
    if float(payload.get("x", 0)) < (now or time.time()):
        raise BadToken("expired")
    return payload


def load_secret(param_name: str | None = None, ssm=None) -> str:
    """Fetch the signing key from SSM. Cached for the life of the container."""
    global _CACHED
    if _CACHED:
        return _CACHED
    name = param_name or os.environ["SCOUT_APPROVAL_PARAM"]
    if ssm is None:
        import boto3
        ssm = boto3.client("ssm")
    r = ssm.get_parameter(Name=name, WithDecryption=True)
    _CACHED = r["Parameter"]["Value"]
    return _CACHED


_CACHED: str | None = None
