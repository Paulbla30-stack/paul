"""Which sites the agent may act on, once it can stay logged in.

This is Jarvis's design, not mine. Asked what it would want structurally
given that Paul had decided to let it act without asking, it answered:

    origin-level permission control ... Track which origins the browser has
    active sessions on. Require explicit, one-time operator approval the first
    time any action is attempted from a new origin while a session exists.
    After approval, allow subsequent actions on that origin only if no new
    sensitive fields are present ... This adds memory of trust per origin, not
    blanket trust, and keeps the operator in the loop where risk concentrates.

That is a better answer than the one I had, which was to trust everything once
the grant was on, and it maps exactly onto the risk Paul accepted. Acting and
staying logged in are each survivable alone. Together they mean an instruction
injected into any page can act as him on any site he is signed into, and no
amount of care about the *text* prevents it -- we established that this
morning and dropped the keyword filtering that pretended otherwise.

So the fence goes on the pairing rather than on either half:

* **A clean profile is free.** Nothing is signed in, so the agent clicking a
  button is a stranger clicking a button. There is no identity to borrow and
  nothing to approve.
* **A persistent profile is per-origin.** The first action on an origin waits
  for Paul. After that the origin is his decision, recorded, and revocable.

The asymmetry is the point. Widening one capability re-narrows the other, and
the narrowing is automatic rather than something anyone has to remember.
"""

from urllib.parse import urlsplit

# What the caller gets back. Strings rather than booleans because "no" has two
# very different meanings here and a caller that cannot tell them apart will
# report the wrong one to Paul.
ALLOW = "allow"
NEEDS_APPROVAL = "needs-approval"
REFUSE = "refuse"


def origin(url: str) -> str:
    """scheme://host[:port], lowercased. The unit trust is granted in.

    Not the hostname: http://example.com and https://example.com are different
    trust decisions, and a port is part of a site's identity. Approving a
    site should not silently approve a plaintext version of it.
    """
    parts = urlsplit((url or "").strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    if not scheme or not host:
        return ""
    port = parts.port
    default = {"http": 80, "https": 443}.get(scheme)
    return f"{scheme}://{host}" + (f":{port}" if port and port != default else "")


def normalise_approved(value) -> frozenset:
    """The origins an operator has actually approved. Fails closed.

    Each entry is put through origin() rather than trusted as written, so
    "https://Example.COM/some/path" and "https://example.com" cannot end up as
    two different rules for one site. Anything that does not parse is dropped.
    """
    if isinstance(value, str) or not hasattr(value, "__iter__"):
        return frozenset()
    out = set()
    for item in value:
        if not isinstance(item, str):
            continue
        got = origin(item)
        if got:
            out.add(got)
    return frozenset(out)


def decide(url: str, persistent: bool, approved=(), *, has_secret: bool = False,
           secrets_unlocked: bool = False) -> tuple:
    """(verdict, reason) for acting on this page.

    ``persistent`` is whether the profile keeps cookies between sessions --
    that is, whether there is any identity here to borrow.
    """
    site = origin(url)
    if not site:
        return REFUSE, "no origin: nothing is open"

    if has_secret and not secrets_unlocked:
        # The default that survives the grant. Lifting it is a separate
        # decision from letting the agent act, because typing into a password
        # box is not the same kind of act as pressing "next page".
        return REFUSE, (f"{site} has a password or payment field and filling "
                        "those is not switched on")

    if not persistent:
        # Nothing is signed in anywhere, so there is no identity to misuse.
        return ALLOW, f"{site}: nothing is signed in, so there is nobody to be"

    if site in normalise_approved(approved):
        return ALLOW, f"{site} is approved for acting"

    return NEEDS_APPROVAL, (
        f"the browser stays signed in, and {site} has not been approved for "
        "acting yet. Acting here could act as Paul. He approves the site once "
        "and then it is open.")


def describe(persistent: bool, approved=()) -> str:
    """One line for the model, so it knows the shape before it is refused."""
    if not persistent:
        return ("The browser forgets everything between sessions, so nothing is "
                "signed in and you may act on any page you can reach.")
    sites = sorted(normalise_approved(approved))
    listed = ", ".join(sites) if sites else "none yet"
    return ("The browser stays signed in, so acting on a site can act as Paul. "
            f"You may act on sites he has approved ({listed}). On any other "
            "site, acting is recorded as a proposal for him instead.")
