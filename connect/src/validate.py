"""Where a connection may go, decided before anything is sent.

Every connection names exactly one host. This module is the one place that
decides whether that host is acceptable, first as written and then again
after DNS, because a name that looks public can resolve somewhere that is
not. It runs in the broker, off Vigil's box, and it trusts nothing the box
or a manifest says about an address.

Refused outright, whatever resolves:
  - anything but https on 443, and anything carrying userinfo
  - IP literals in any spelling (dotted, decimal, octal, hex, v4-mapped v6)
  - localhost, .local, .internal, .localdomain, cloud metadata names
  - Arkin: thearkinsystem.co.uk and every subdomain. Paul's professional
    system is firewalled from Vigil, and a connection is exactly the kind of
    thing that would quietly join the two. It is refused here, in code, so the
    firewall does not depend on anyone remembering it.
"""

import ipaddress
import re
import socket
from typing import Iterable, List, Optional

DENIED_SUFFIXES = (
    "localhost", "local", "internal", "localdomain", "home.arpa", "lan",
    # Paul's professional system. Never a Vigil connection.
    "thearkinsystem.co.uk",
)
DENIED_NAMES = (
    "metadata", "metadata.google.internal", "instance-data",
    "instance-data.ec2.internal",
)
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


class Refused(ValueError):
    """The host or URL may not be used. The message never repeats secrets."""


def normalise_host(host: str) -> str:
    """Lower-case, IDNA-encoded, no trailing dot. Refuses what is not a name."""
    raw = str(host or "").strip()
    if not raw or len(raw) > 253:
        raise Refused("host missing or too long")
    if any(c in raw for c in "/@:?#[]\\ \t\r\n%"):
        raise Refused("host contains characters a host cannot")
    try:
        ascii_host = raw.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise Refused("host is not a valid international name")
    labels = ascii_host.split(".")
    if len(labels) < 2 or not all(_LABEL.match(label) for label in labels):
        raise Refused("host is not a fully qualified name")
    if _looks_numeric(ascii_host):
        raise Refused("IP addresses are not allowed; use a name")
    return ascii_host


def _looks_numeric(host: str) -> bool:
    """Any spelling the resolver would read as an address, not a name."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    # 2130706433, 0x7f.1, 017700000001 and friends: inet_aton accepts them.
    try:
        socket.inet_aton(host)
        return True
    except OSError:
        return False


def check_host(host: str) -> str:
    """The normalised host, or Refused. No network use."""
    name = normalise_host(host)
    if name in DENIED_NAMES:
        raise Refused("that host is a cloud metadata name")
    for suffix in DENIED_SUFFIXES:
        if name == suffix or name.endswith("." + suffix):
            raise Refused(f"hosts under {suffix} are not allowed")
    return name


def is_public(address: str) -> bool:
    """True only for a globally routable unicast address."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_global) and not (ip.is_multicast or ip.is_reserved
                                       or ip.is_loopback or ip.is_link_local
                                       or ip.is_private or ip.is_unspecified)


def resolve_public(host: str, resolver=None) -> List[str]:
    """Every address the name resolves to, all public, or Refused.

    All of them, not the first: a name that returns one public and one
    private address is a name someone controls the answers for.
    """
    resolve = resolver or (lambda h: [ai[4][0] for ai in socket.getaddrinfo(
        h, 443, type=socket.SOCK_STREAM)])
    try:
        addresses = sorted(set(resolve(host)))
    except OSError:
        raise Refused("the host did not resolve")
    if not addresses:
        raise Refused("the host did not resolve")
    for address in addresses:
        if not is_public(address):
            raise Refused("the host resolves to a non-public address")
    return addresses


def check_url(url: str, expected_host: str) -> str:
    """An https URL on the one expected host, port 443, no userinfo."""
    from urllib.parse import urlsplit
    parts = urlsplit(str(url or ""))
    if parts.scheme != "https":
        raise Refused("only https is allowed")
    if parts.username or parts.password or "@" in parts.netloc:
        raise Refused("credentials in a URL are not allowed")
    if parts.port not in (None, 443):
        raise Refused("only port 443 is allowed")
    if check_host(parts.hostname or "") != check_host(expected_host):
        raise Refused("that URL is not on this connection's host")
    return url


def all_public(addresses: Iterable[str]) -> bool:
    return all(is_public(a) for a in addresses)
