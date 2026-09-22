"""Agent-side client for ledgerd. Stdlib only.

    from ledgerd.client import LedgerClient, LedgerError

    ledger = LedgerClient()
    receipt = ledger.append("action", {"task": "df -h", "rung": "proposer"}, model=MODEL_ID)
    run_the_task()          # only reached if the entry is on disk

Fail-closed contract: append() either returns a Receipt — the entry is signed,
chained and fsynced — or raises LedgerError. Callers must treat any exception
as "no record", which means no action and no answer.

The client can only append. There is no read method because there is no read
operation to call.
"""
from __future__ import annotations

import json
import os
import socket

from .protocol import DEFAULT_SOCKET, MAX_LINE, PROTOCOL_VERSION, Receipt, encode, is_hash

__all__ = ["LedgerClient", "LedgerError", "LedgerUnavailable", "LedgerRefused", "Receipt"]


class LedgerError(RuntimeError):
    """No record was made. Do not act, do not answer."""


class LedgerUnavailable(LedgerError):
    """ledgerd could not be reached, or did not confirm the write."""


class LedgerRefused(LedgerError):
    """ledgerd refused the request. `code` says why (forbidden, kind_refused, model_required, ...)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class LedgerClient:
    def __init__(self, socket_path: "str | None" = None, timeout: float = 5.0):
        self.socket_path = socket_path or os.environ.get("JARVIS_LEDGER_SOCKET", DEFAULT_SOCKET)
        self.timeout = timeout

    def append(self, kind: str, payload: dict, model: "str | None" = None) -> Receipt:
        request = {"v": PROTOCOL_VERSION, "op": "append", "kind": kind, "payload": payload}
        if model is not None:
            request["model"] = model
        response = self.request(request)
        if response.get("ok") is True:
            seq, digest = response.get("seq"), response.get("hash")
            if type(seq) is int and seq > 0 and is_hash(digest):
                return Receipt(seq, digest)
            raise LedgerUnavailable("malformed receipt")
        raise LedgerRefused(str(response.get("error", "unknown")))

    def request(self, obj: dict) -> dict:
        """Send one raw request and return the raw response. Transport errors raise LedgerUnavailable."""
        line = encode(obj)  # ValueError / TypeError here is a caller bug and propagates
        if len(line) > MAX_LINE:
            raise ValueError(f"request is {len(line)} bytes; the limit is {MAX_LINE}")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(self.timeout)
                s.connect(self.socket_path)
                s.sendall(line)
                with s.makefile("rb") as f:
                    raw = f.readline(MAX_LINE + 1)
        except OSError as e:
            raise LedgerUnavailable(f"{self.socket_path}: {e}") from None
        if not raw.endswith(b"\n"):
            raise LedgerUnavailable("no complete response")
        try:
            response = json.loads(raw)
        except ValueError:
            raise LedgerUnavailable("unparseable response") from None
        if not isinstance(response, dict):
            raise LedgerUnavailable("unexpected response")
        return response
