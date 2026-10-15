"""One bounded HTTPS GET to one checked host.

The broker never hands a URL to a library that would resolve it again on its
own. The host is resolved here, every address is checked public, and the
connection goes to the checked address with the name carried in SNI and the
Host header, so the name cannot be re-pointed between the check and the
connect. Certificates are verified against the name, as normal.

What comes back is capped while it streams, compression is refused rather
than trusted, and redirects are followed only on the same host, twice at
most, each one checked again.
"""

import http.client
import socket
import ssl
from typing import Callable, Dict, Optional
from urllib.parse import urlsplit, urljoin

from validate import Refused, check_url, resolve_public

USER_AGENT = "vigil-connect/1 (personal assistant; contact via heartbeat-framework.org)"
MAX_REDIRECTS = 2


class FetchError(RuntimeError):
    """The fetch failed. Carries a short code, never the URL or its query."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _PinnedHTTPS(http.client.HTTPSConnection):
    """HTTPS to a fixed address, verifying the certificate for the name."""

    def __init__(self, host: str, address: str, timeout: float):
        super().__init__(host, 443, timeout=timeout,
                         context=ssl.create_default_context())
        self._address = address

    def connect(self):
        sock = socket.create_connection((self._address, 443), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def get(url: str, host: str, *, max_bytes: int = 256 * 1024, timeout: float = 10.0,
        resolver: Optional[Callable] = None, opener: Optional[Callable] = None,
        headers: Optional[Dict[str, str]] = None) -> bytes:
    """The body of an https GET on ``host``, or FetchError / Refused.

    ``opener(host, address, path, headers, timeout)`` returns
    (status, response_headers, reader) and exists so tests never touch the
    network; the default is the pinned connection above.
    """
    open_one = opener or _open
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        check_url(current, host)
        parts = urlsplit(current)
        addresses = resolve_public(parts.hostname, resolver)
        path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
        sent = {"User-Agent": USER_AGENT, "Accept": "application/json",
                "Accept-Encoding": "identity"}
        sent.update(headers or {})
        status, response_headers, reader = open_one(parts.hostname, addresses[0], path,
                                                    sent, timeout)
        if status in (301, 302, 303, 307, 308):
            location = response_headers.get("location") or ""
            current = urljoin(current, location)
            continue                                   # re-checked at the top
        if status != 200:
            raise FetchError(f"http_{status}")
        encoding = (response_headers.get("content-encoding") or "identity").lower()
        if encoding not in ("identity", ""):
            raise FetchError("compressed_response_refused")
        return _read_capped(reader, max_bytes)
    raise FetchError("too_many_redirects")


def _read_capped(reader, max_bytes: int) -> bytes:
    chunks, total = [], 0
    while True:
        chunk = reader(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise FetchError("response_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _open(host: str, address: str, path: str, headers: Dict[str, str], timeout: float):
    conn = _PinnedHTTPS(host, address, timeout)
    try:
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
    except (OSError, http.client.HTTPException, ssl.SSLError):
        conn.close()
        raise FetchError("network_error")
    lowered = {k.lower(): v for k, v in response.getheaders()}
    return response.status, lowered, response.read
