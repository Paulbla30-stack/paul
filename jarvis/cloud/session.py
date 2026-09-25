"""Signed browser sessions for the web UI, so a login outlives a restart.

The runner token was generated fresh at every start and written under
``/run/jarvis``, which systemd deletes and recreates on each restart
(``RuntimeDirectory=jarvis``). Every deploy therefore issued a new token and
silently invalidated the one in the operator's browser. He was not being
logged out by a timeout; the credential was being replaced underneath him.

A session fixes the right half of that. The token stays the credential, and
is now persisted separately, but a successful login mints a signed cookie
that survives restarts because the key that signs it is on disk rather than
in the runtime directory. The agent restarts; the operator does not notice.

The cookie is stateless: value, expiry and an HMAC over both. Nothing is
stored server-side, so there is no session table to leak or grow. Every
session is invalidated at once by rotating the key file, which is the only
revocation this deployment needs with a single operator.
"""

import hashlib
import hmac
import logging
import os
import secrets
import time
from typing import Optional

DEFAULT_KEY_FILE = "/etc/jarvis/session.key"
DEFAULT_DAYS = 30
COOKIE_NAME = "jarvis_session"
VERSION = "v1"
MAX_DAYS = 365


def load_or_create_key(path: str, logger: Optional[logging.Logger] = None) -> Optional[str]:
    """Return the persisted signing key, creating it 0600 on first start.

    Returns None when the key cannot be persisted, which disables sessions
    rather than signing with a key that dies with the process: a cookie that
    stops working at the next restart is worse than no cookie at all.
    """
    log = logger or logging.getLogger("jarvis")
    try:
        with open(path, "r") as fh:
            key = fh.read().strip()
        if len(key) >= 32:
            return key
        log.warning("Session key at %s is too short; regenerating", path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Cannot read session key %s: %s; sessions disabled", path, exc)
        return None

    key = secrets.token_hex(32)
    try:
        parent = os.path.dirname(path) or "."
        os.makedirs(parent, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(key + "\n")
        os.chmod(path, 0o600)
    except OSError as exc:
        log.warning("Cannot write session key %s: %s; sessions disabled", path, exc)
        return None
    log.info("Generated a new UI session key at %s (0600); existing sessions are now invalid", path)
    return key


def _mac(key: str, payload: str) -> str:
    return hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()


def issue(key: str, days: int = DEFAULT_DAYS, now: Optional[float] = None) -> str:
    """Mint a signed session value valid for ``days``."""
    days = max(1, min(int(days), MAX_DAYS))
    issued = int(now if now is not None else time.time())
    expires = issued + days * 86400
    payload = f"{VERSION}.{issued}.{expires}"
    return f"{payload}.{_mac(key, payload)}"


def verify(key: Optional[str], value: Optional[str], now: Optional[float] = None) -> bool:
    """True when ``value`` is a well-formed, unexpired session signed by ``key``."""
    if not key or not value:
        return False
    parts = str(value).split(".")
    if len(parts) != 4:
        return False
    version, issued, expires, signature = parts
    if version != VERSION:
        return False
    try:
        issued_at, expires_at = int(issued), int(expires)
    except ValueError:
        return False
    moment = now if now is not None else time.time()
    if expires_at <= moment or issued_at > moment + 300:   # allow small clock skew
        return False
    expected = _mac(key, f"{version}.{issued}.{expires}")
    return hmac.compare_digest(expected, signature)


# Lax, not Strict, and the difference is the whole reason logging in felt
# like it never stuck.
#
# Under Strict a browser withholds the cookie on any navigation that arrives
# from somewhere else — a link in an email, a note, a message, a share
# sheet. Paul opens Jarvis from a link on his phone, so the session was
# withheld on the first hop every time, the page looked logged out, and he
# re-entered the token. The session was valid throughout; it was simply
# never sent.
#
# Lax sends it on top-level GET navigations, which is exactly that case, and
# still withholds it on cross-site POSTs, sub-resources and iframes, which is
# where CSRF lives. Strict is the right default for a bank, where the
# friction is the point. It is the wrong default for something its owner
# reaches by tapping a link.
DEFAULT_SAMESITE = "Lax"


def cookie_header(value: str, days: int = DEFAULT_DAYS, secure: bool = True,
                  samesite: str = DEFAULT_SAMESITE) -> str:
    """The Set-Cookie header for a freshly issued session."""
    days = max(1, min(int(days), MAX_DAYS))
    samesite = samesite if samesite in ("Lax", "Strict", "None") else DEFAULT_SAMESITE
    parts = [f"{COOKIE_NAME}={value}", "Path=/", f"Max-Age={days * 86400}",
             "HttpOnly", f"SameSite={samesite}"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def clear_header(secure: bool = True, samesite: str = DEFAULT_SAMESITE) -> str:
    # Must match the attributes the cookie was set with, or the browser keeps
    # the old one and a logout silently does nothing.
    samesite = samesite if samesite in ("Lax", "Strict", "None") else DEFAULT_SAMESITE
    parts = [f"{COOKIE_NAME}=", "Path=/", "Max-Age=0", "HttpOnly",
             f"SameSite={samesite}"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def from_cookie_header(header: Optional[str]) -> Optional[str]:
    """Pull the session value out of a raw Cookie header."""
    for chunk in str(header or "").split(";"):
        name, _, value = chunk.strip().partition("=")
        if name == COOKIE_NAME and value:
            return value
    return None
