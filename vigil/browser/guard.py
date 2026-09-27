"""Where the browser is not allowed to go.

This is the fence I rated most important in the design note, and nothing else
in the browser matters if it fails. A headless browser on an EC2 instance that
can reach 169.254.169.254 is a headless browser that can be told, by any page
it loads, to fetch the instance role's credentials and post them somewhere.
The agent's whole permission spine sits above the role; a page that gets the
role does not need the spine's permission for anything.

So the rule is not "do not browse to the metadata service". It is: **every
request the browser makes, including ones the page makes on its own -- images,
XHR, fetch, redirects, iframes -- resolves to a public address or does not
happen.** A page that cannot be stopped from asking can still be stopped from
reaching.

Three layers, because one is a single point of failure:

  1. this module, checked before navigation and again on every intercepted
     request, so a redirect to a private address is caught even though the URL
     the agent asked for was public;
  2. the browser process runs under its own uid in a systemd unit with
     `IPAddressDeny=link-local` and the private ranges, so the kernel refuses
     what this module misses;
  3. the browser holds no AWS credentials of its own and cannot read the
     instance profile from disk, so a request that somehow lands has nothing
     to sign with.

DNS rebinding is the case this has to get right and the naive version gets
wrong. Checking the hostname is useless: `evil.example` can resolve to a
public address when it is checked and 169.254.169.254 when it is fetched.
So this resolves the name and checks **every address it resolves to**, and the
systemd layer is what actually holds if the resolution changes between the
check and the connection.
"""

import ipaddress
import socket
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})

# Named for the journal line, so a refusal says which rule caught it rather
# than "blocked".
REASON_SCHEME = "scheme is not http or https"
REASON_NO_HOST = "no host in the URL"
REASON_UNRESOLVABLE = "host does not resolve"
REASON_PRIVATE = "resolves to an address that is not public"
REASON_METADATA = "the cloud metadata service"

# The one that would end the account. Listed separately from the general
# private-range rule so the refusal names it: a journal line saying "blocked:
# resolves to a private address" and one saying "blocked: the cloud metadata
# service" are the same event and very different news.
METADATA_ADDRESSES = frozenset({
    "169.254.169.254",       # AWS, Azure, DigitalOcean, OpenStack
    "fd00:ec2::254",         # AWS IMDS over IPv6
    "100.100.100.200",       # Alibaba
})
METADATA_NAMES = frozenset({
    "metadata.google.internal", "metadata.goog", "metadata",
    "instance-data",         # the AWS alias that still resolves inside a VPC
})


class Refused(Exception):
    """The browser was asked to reach somewhere it may not."""

    def __init__(self, reason: str, url: str = "", detail: str = ""):
        self.reason, self.url, self.detail = reason, url, detail
        super().__init__(f"{reason}: {url}" + (f" ({detail})" if detail else ""))


def _is_public(addr: str) -> bool:
    """True only for an address that is routable on the public internet.

    `is_global` is the property that matters and it is the one to trust here:
    it already excludes loopback, link-local, the RFC1918 ranges, carrier-grade
    NAT, multicast, reserved and unspecified. Writing the ranges out by hand
    would be a longer way to be wrong about one of them.
    """
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_link_local or ip.is_private:
        return False
    if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False
    return bool(getattr(ip, "is_global", True))


def _resolve(host: str) -> list:
    """Every address a host resolves to, v4 and v6."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    out = []
    for info in infos:
        addr = info[4][0]
        if addr not in out:
            out.append(addr)
    return out


def check(url: str, resolver=_resolve) -> str:
    """Return the URL if the browser may fetch it, or raise ``Refused``.

    ``resolver`` is injected so the tests can describe a host that resolves
    somewhere hostile without needing DNS that does.
    """
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise Refused(REASON_SCHEME, url, parts.scheme or "none")
    host = (parts.hostname or "").strip().rstrip(".").lower()
    if not host:
        raise Refused(REASON_NO_HOST, url)
    if host in METADATA_NAMES:
        raise Refused(REASON_METADATA, url, host)

    # A literal address is checked as itself; resolution would only launder it.
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        addresses = resolver(host)
        if not addresses:
            raise Refused(REASON_UNRESOLVABLE, url, host)

    for addr in addresses:
        if addr in METADATA_ADDRESSES:
            raise Refused(REASON_METADATA, url, addr)
        if not _is_public(addr):
            raise Refused(REASON_PRIVATE, url, addr)
    return url


def allowed(url: str, resolver=_resolve) -> bool:
    """The same question, asked where an exception would be noise."""
    try:
        check(url, resolver=resolver)
        return True
    except Refused:
        return False
