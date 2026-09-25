"""Fence self-test: prove the OS-level fence holds, from inside the fenced process.

    python -m fence.selftest \
        --deny-read /var/lib/jarvis-ledger/ledger.jsonl \
        --deny-read /etc/jarvis-ledger/signing.key \
        --deny-write /proc/sys/vm/swappiness \
        --deny-write /etc/sysctl.d \
        --probe-ledger-socket /run/jarvis-ledger/append.sock

Run as ExecStartPre in the agent's unit. systemd runs ExecStartPre under the
same user and sandbox as ExecStart, so this tests the fence the agent will
actually have — not the one the unit file claims. Exit 0 only if every check
passes; the agent does not start otherwise.

The deny-list is a string filter and a second layer. This is the first layer:
the kernel refusing the operation, whatever string was used to ask for it.

Non-destructive: write checks open a file for writing and close it without
writing, or create a uniquely named probe file and remove it at once.
A path that does not exist FAILS — a fence cannot be demonstrated on a path
that is not there, and a wrong path is how a fence quietly stops covering
anything.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import secrets
import sys
from typing import NamedTuple

_REFUSED = {errno.EACCES, errno.EPERM, errno.EROFS}


class Check(NamedTuple):
    name: str
    ok: bool
    detail: str


def _status_fields() -> dict:
    out = {}
    try:
        with open("/proc/self/status") as f:
            for line in f:
                key, _, value = line.partition(":")
                out[key.strip()] = value.strip()
    except OSError:
        pass
    return out


def check_not_root() -> Check:
    euid = os.geteuid()
    return Check("not_root", euid != 0, f"euid={euid}")


def check_no_capabilities(status: "dict | None" = None) -> Check:
    status = _status_fields() if status is None else status
    held = {k: status.get(k) for k in ("CapEff", "CapPrm", "CapAmb")}
    if any(v is None for v in held.values()):
        return Check("no_capabilities", False, "cannot read capabilities from /proc/self/status")
    nonzero = {k: v for k, v in held.items() if int(v, 16) != 0}
    return Check("no_capabilities", not nonzero, f"nonzero: {nonzero}" if nonzero else "all zero")


def check_no_new_privs(status: "dict | None" = None) -> Check:
    status = _status_fields() if status is None else status
    value = status.get("NoNewPrivs")
    return Check("no_new_privs", value == "1", f"NoNewPrivs={value}")


def check_deny_read(path: str) -> Check:
    name = f"deny_read:{path}"
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        return Check(name, False, "path not found — the fence cannot be shown to cover it")
    except OSError as e:
        if e.errno in _REFUSED:
            return Check(name, True, f"refused ({errno.errorcode.get(e.errno, e.errno)})")
        return Check(name, False, f"unexpected error {e.errno}: {e.strerror}")
    os.close(fd)
    return Check(name, False, "READABLE by this process")


def check_deny_write(path: str) -> Check:
    name = f"deny_write:{path}"
    if os.path.isdir(path):
        probe = os.path.join(path, f".fence-probe-{os.getpid()}-{secrets.token_hex(4)}")
        try:
            fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileNotFoundError:
            return Check(name, False, "directory not found")
        except OSError as e:
            if e.errno in _REFUSED:
                return Check(name, True, f"refused ({errno.errorcode.get(e.errno, e.errno)})")
            return Check(name, False, f"unexpected error {e.errno}: {e.strerror}")
        os.close(fd)
        os.unlink(probe)
        return Check(name, False, "WRITABLE: created and removed a probe file")
    try:
        fd = os.open(path, os.O_WRONLY | getattr(os, "O_CLOEXEC", 0))  # no O_TRUNC, no O_CREAT
    except FileNotFoundError:
        return Check(name, False, "path not found — the fence cannot be shown to cover it")
    except OSError as e:
        if e.errno in _REFUSED:
            return Check(name, True, f"refused ({errno.errorcode.get(e.errno, e.errno)})")
        return Check(name, False, f"unexpected error {e.errno}: {e.strerror}")
    os.close(fd)  # opened for write and closed without writing
    return Check(name, False, "WRITABLE by this process (opened, nothing written)")


def check_ledger_socket(path: str) -> Check:
    """The socket must accept us, and must refuse a read. (The refusal is itself recorded.)"""
    name = f"ledger_socket:{path}"
    try:
        from ledgerd.client import LedgerClient, LedgerError
    except ImportError:
        return Check(name, False, "ledgerd package not importable")
    try:
        response = LedgerClient(path, timeout=5).request({"v": 1, "op": "read"})
    except LedgerError as e:
        return Check(name, False, f"unreachable: {e}")
    if response == {"ok": False, "error": "op_refused"}:
        return Check(name, True, "reachable; read refused")
    return Check(name, False, f"unexpected response to a read: {response}")


def run(deny_read=(), deny_write=(), ledger_socket=None) -> "list[Check]":
    status = _status_fields()
    checks = [check_not_root(), check_no_capabilities(status), check_no_new_privs(status)]
    checks += [check_deny_read(p) for p in deny_read]
    checks += [check_deny_write(p) for p in deny_write]
    if ledger_socket:
        checks.append(check_ledger_socket(ledger_socket))
    return checks


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m fence.selftest")
    p.add_argument("--deny-read", action="append", default=[])
    p.add_argument("--deny-write", action="append", default=[])
    p.add_argument("--probe-ledger-socket")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    if not args.deny_read or not args.deny_write:
        print("fence.selftest: give at least one --deny-read and one --deny-write", file=sys.stderr)
        return 2
    checks = run(args.deny_read, args.deny_write, args.probe_ledger_socket)
    failed = [c for c in checks if not c.ok]
    if args.json:
        print(json.dumps({"ok": not failed, "checks": [c._asdict() for c in checks]}))
    else:
        for c in checks:
            print(f"{'PASS' if c.ok else 'FAIL'}  {c.name}  {c.detail}")
        print(f"fence: {'HOLDS' if not failed else f'{len(failed)} check(s) FAILED — refusing to start'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
