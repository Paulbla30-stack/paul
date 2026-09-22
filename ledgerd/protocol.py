"""Wire protocol shared by ledgerd and its clients. Stdlib only.

One JSON object per line, UTF-8, newline-terminated. Exactly one operation
exists: append. There is deliberately no read, tail, head, get or list — a
writer the agent can query is a record the agent can consult.

Request:   {"v": 1, "op": "append", "kind": "decision", "payload": {...}, "model": "..."}
Response:  {"ok": true, "seq": 42, "hash": "<64 hex>"}
       or  {"ok": false, "error": "<code>"}
"""
from __future__ import annotations

import json
import re
from typing import NamedTuple

PROTOCOL_VERSION = 1
MAX_LINE = 64 * 1024
DEFAULT_SOCKET = "/run/jarvis-ledger/append.sock"

# The six entry kinds named in the architecture note, plus deploy records.
DEFAULT_KINDS = frozenset({"decision", "action", "outcome", "gate", "alert", "thought", "deploy"})
# Entries whose content depends on which model produced them. Without the model
# id, faults / timesense / probes cannot be partitioned when the model changes.
DEFAULT_MODEL_REQUIRED = frozenset({"decision", "thought"})

_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_MODEL_RE = re.compile(r"^[\x21-\x7e]{1,200}$")  # printable ASCII, no whitespace
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_RE = re.compile(r"^ledger:([1-9][0-9]{0,18}):([0-9a-f]{64})$")
_FIELDS = frozenset({"v", "op", "kind", "payload", "model"})


class Receipt(NamedTuple):
    """Proof that an entry reached the chain: its sequence number and hash."""

    seq: int
    hash: str

    def as_source(self) -> str:
        """Provenance string for the memory store: points at a chain entry, not at a claim."""
        return f"ledger:{self.seq}:{self.hash}"


def parse_source(source: object) -> "Receipt | None":
    """Return the Receipt a provenance string refers to, or None if it is not one.

    This checks shape only. The store cannot read the chain (and must not), so
    whether the receipt exists is checked off-box by the verifier.
    """
    if not isinstance(source, str):
        return None
    m = _SOURCE_RE.match(source)
    return Receipt(int(m.group(1)), m.group(2)) if m else None


class ProtocolError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def canonical(obj) -> bytes:
    """Deterministic encoding, used for hashing and on the wire."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def encode(obj) -> bytes:
    return canonical(obj) + b"\n"


def _reject_constant(name):
    raise ProtocolError("bad_request", f"{name} is not permitted")


def decode_line(line: bytes) -> dict:
    try:
        obj = json.loads(line.decode("utf-8"), parse_constant=_reject_constant)
        canonical(obj)  # rejects lone surrogates and anything that cannot be hashed stably
    except ProtocolError:
        raise
    except Exception:
        raise ProtocolError("bad_request", "not valid JSON") from None
    if not isinstance(obj, dict):
        raise ProtocolError("bad_request", "request must be a JSON object")
    return obj


def validate_append(obj: dict, kinds, model_required) -> "tuple[str, dict, str | None]":
    op = obj.get("op")
    if op != "append":
        raise ProtocolError("op_refused", repr(op)[:64])
    if obj.get("v") != PROTOCOL_VERSION:
        raise ProtocolError("bad_version")
    unknown = set(obj) - _FIELDS
    if unknown:
        raise ProtocolError("bad_request", f"unknown fields {sorted(unknown)[:5]}")
    kind = obj.get("kind")
    if not isinstance(kind, str) or not _KIND_RE.match(kind):
        raise ProtocolError("bad_kind")
    if kind not in kinds:
        raise ProtocolError("kind_refused", kind)
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        raise ProtocolError("bad_payload", "payload must be a JSON object")
    model = obj.get("model")
    if model is not None and (not isinstance(model, str) or not _MODEL_RE.match(model)):
        raise ProtocolError("bad_model")
    if kind in model_required and model is None:
        raise ProtocolError("model_required", kind)
    return kind, payload, model


def is_hash(value: object) -> bool:
    return isinstance(value, str) and bool(_HASH_RE.match(value))
