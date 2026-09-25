"""Cloudflare Tunnel: how the operator reaches the UI without an open port.

The UI listens on 8443 with a self-signed certificate, and the only way to
reach it from a phone was to open that port in the security group. Narrowing
it to one address does not survive a carrier moving you; leaving it at
``0.0.0.0/0`` puts the login page in front of every scanner on the internet.

A tunnel inverts it. ``cloudflared`` dials *out* to Cloudflare on 7844 and
holds the connection open; requests arrive back down it. The security group
then needs no inbound rule at all, the certificate warning goes away because
Cloudflare terminates TLS with a real certificate, and Cloudflare Access can
ask who you are before a request ever reaches the box.

This module does one job: get the tunnel's credential out of AWS and onto
disk where the ``cloudflared`` unit can read it, and nowhere else. The token
is the tunnel: anyone holding it can re-point the hostname at their own
machine, so it is written 0600 to a file owned by the ``cloudflared`` user,
never into the world-readable overlay, and the agent's shell deny-list
refuses the path the same way it refuses the ledger's signing key.

The ingress rules (which hostname maps to which local service) live in
Cloudflare, not here, because a token-run tunnel is configured remotely.
That is deliberate: the box cannot rewrite what it is allowed to publish.
"""

import os
from typing import Optional

from jarvis.brain.credentials import (fetch_ssm_parameter,
                                      fetch_secretsmanager_secret)

DEFAULT_TOKEN_FILE = "/etc/jarvis/cloudflared.env"
DEFAULT_OWNER = "cloudflared"
# What the systemd unit reads. cloudflared picks the token up from the
# environment, so it never appears in the process list.
TOKEN_ENV = "TUNNEL_TOKEN"


def install_token_file(token: str, path: str = DEFAULT_TOKEN_FILE,
                       owner: str = DEFAULT_OWNER) -> str:
    """Write ``token`` as an EnvironmentFile readable only by ``owner``."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    # Create it unreadable, then fill it: never a window where the token
    # sits in a file the rest of the box can open.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, f"{TOKEN_ENV}={token.strip()}\n".encode())
    finally:
        os.close(fd)
    _chown(tmp, owner)
    os.replace(tmp, path)
    return path


def _chown(path: str, owner: str) -> None:
    """Hand the file to the tunnel's own user, if that user exists."""
    try:
        import pwd
        entry = pwd.getpwnam(owner)
    except (ImportError, KeyError):
        return  # not provisioned (a test box, or a build host); 0600 root still holds
    try:
        os.chown(path, entry.pw_uid, entry.pw_gid)
    except OSError:
        pass


def configured(config: dict) -> bool:
    """True when user data or a tag asked for a tunnel at all."""
    section = (config or {}).get("tunnel")
    if not isinstance(section, dict):
        return False
    if section.get("enabled") is False:
        return False
    return bool(section.get("token") or section.get("token_secret")
                or section.get("token_ssm_parameter"))


def provision_token(config: dict, token_file: str = DEFAULT_TOKEN_FILE,
                    fetch=fetch_ssm_parameter,
                    fetch_secret=fetch_secretsmanager_secret) -> str:
    """Move the tunnel token out of the overlay and into a 0600 file.

    Sources, in order: an inline ``tunnel.token`` from user data (removed
    from the overlay so it never lands in cloud.yaml), then
    ``tunnel.token_secret`` from Secrets Manager, then
    ``tunnel.token_ssm_parameter`` from SSM Parameter Store, both fetched
    with the instance role. Returns a short description for the boot log.

    Never raises: a box that cannot fetch its tunnel token should still come
    up and be reachable over SSM, which is how you would go and fix it.
    """
    section = config.get("tunnel")
    if not isinstance(section, dict):
        return "no tunnel section"
    if section.get("enabled") is False:
        section.pop("token", None)
        return "tunnel disabled"
    token_file = section.get("token_file") or token_file
    inline = section.pop("token", None)
    if isinstance(inline, str) and inline.strip():
        install_token_file(inline, token_file)
        section["token_file"] = token_file
        return f"inline tunnel token moved to {token_file}"
    region = (config.get("cloud", {}).get("instance") or {}).get("region")
    notes = []
    secret = section.get("token_secret")
    if secret:
        value = fetch_secret(secret, region)
        if value:
            install_token_file(value, token_file)
            section["token_file"] = token_file
            return f"tunnel token fetched from Secrets Manager {secret} -> {token_file}"
        notes.append(f"Secrets Manager secret {secret} not readable (role permissions? awscli?)")
    param = section.get("token_ssm_parameter")
    if param:
        value = fetch(param, region)
        if value:
            install_token_file(value, token_file)
            section["token_file"] = token_file
            return f"tunnel token fetched from SSM {param} -> {token_file}"
        notes.append(f"SSM parameter {param} not readable (role permissions? awscli?)")
    return "; ".join(notes) if notes else "no tunnel token configured"


def strip_secrets(config: dict) -> None:
    """The overlay is world-readable; the token never belongs in it."""
    section = config.get("tunnel")
    if isinstance(section, dict):
        section.pop("token", None)


def status(token_file: str = DEFAULT_TOKEN_FILE) -> dict:
    """What a health check can say about the tunnel without reading it."""
    try:
        st = os.stat(token_file)
    except OSError:
        return {"token_installed": False, "token_file": token_file}
    return {"token_installed": True, "token_file": token_file,
            "mode": oct(st.st_mode & 0o777), "size": st.st_size}


def token_present(token_file: str = DEFAULT_TOKEN_FILE) -> bool:
    return status(token_file)["token_installed"]
