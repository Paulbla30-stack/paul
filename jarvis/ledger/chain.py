"""Glass Ledger chain format v2: entries, hashing, signing, the writer.

Entry (one JSON line)::

    {"seq": 2, "ts": "2026-07-02T20:57:27Z", "kind": "action",
     "body": {"ran": "logrotate --force"},
     "prev_hash": "9ac7…", "entry_hash": "6997…", "sig": "75f8…"}

* ``seq`` starts at 0 (genesis) and climbs by exactly 1.
* ``ts`` is the writer's clock (UTC, second precision); recorded, not trusted.
* ``kind`` is one of genesis, thought, decision, gate, action, outcome, alert.
* ``body`` is the content, serialised as canonical JSON for hashing.
* ``prev_hash`` repeats the previous entry's fingerprint (64 zeros at genesis).
* ``entry_hash`` = SHA-256 over the entry's own fields, each framed as
  ``[8-byte big-endian length][bytes]`` in the order seq (decimal ASCII),
  ts, kind, canonical body, prev_hash.
* ``sig`` = Ed25519 over ``b"glass-ledger/v2:" + entry_hash`` (hex ASCII),
  hex-encoded.

Canonical JSON is ``sort_keys``, no incidental whitespace, UTF-8 (not
ASCII-escaped) and ``allow_nan=False``. The paper fixes the design; the
two byte-level choices it leaves open, the 8-byte length prefix and UTF-8
canonical output, are fixed here so an independent verifier can be
written from this docstring alone.

The writer holds an exclusive lock, fsyncs after every entry, and refuses
to append onto a torn tail (a partial final line after a power loss): the
operator repairs that explicitly with ``python -m jarvis.ledger repair``.
There is no update and no delete; a correction is a new entry.
"""

import fcntl
import hashlib
import json
import os
import struct
import threading
import time
from typing import Optional

FORMAT = "glass-ledger/v2"
SIGN_DOMAIN = b"glass-ledger/v2:"
GENESIS_PREV = "0" * 64
# The kinds of thing that can be said about the agent. Adding one is a format
# change: a verifier written against the published spec rejects an entry whose
# kind it does not know, and an older Jarvis reading a newer ledger would call
# an honest chain corrupt. So they are append-only, in order, and each one is a
# category of event rather than a convenience.
#
#   notification   a message that left the machine for the operator
#   consolidation  the agent reorganising its own memory
KINDS = ("genesis", "thought", "decision", "gate", "action", "outcome", "alert",
         "notification", "consolidation", "vigil", "verdict")
MAX_BODY_BYTES = 65536


class LedgerError(Exception):
    """Base class for ledger failures."""


class TornTail(LedgerError):
    """The file ends in a partial line; ``offset`` is where the good tail ends."""

    def __init__(self, path: str, offset: int):
        super().__init__(f"torn tail in {path}: partial final line after byte {offset}; "
                         f"repair with: python -m jarvis.ledger repair {path}")
        self.path = path
        self.offset = offset


class LedgerLocked(LedgerError):
    """Another writer holds the ledger."""


# ---- crypto -----------------------------------------------------------------

def _ed25519():
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError as e:  # pragma: no cover - environment dependent
        raise LedgerError("the cryptography package is required for the Glass Ledger "
                          "(pip install cryptography)") from e
    return ed25519


def generate_key() -> tuple:
    """Return (private_seed_hex, public_key_hex) for a fresh Ed25519 key."""
    ed = _ed25519()
    key = ed.Ed25519PrivateKey.generate()
    return key.private_bytes_raw().hex(), key.public_key().public_bytes_raw().hex()


def load_private_key(seed_hex: str):
    ed = _ed25519()
    seed = bytes.fromhex(seed_hex.strip())
    if len(seed) != 32:
        raise LedgerError("Ed25519 private key must be 32 bytes (64 hex characters)")
    return ed.Ed25519PrivateKey.from_private_bytes(seed)


def public_key_hex(private_key) -> str:
    return private_key.public_key().public_bytes_raw().hex()


def sign_entry_hash(private_key, entry_hash_hex: str) -> str:
    return private_key.sign(SIGN_DOMAIN + entry_hash_hex.encode("ascii")).hex()


def verify_signature(pubkey_hex: str, entry_hash_hex: str, sig_hex: str) -> bool:
    """True when sig_hex signs entry_hash_hex under pubkey_hex. Never raises."""
    try:
        ed = _ed25519()
        pub = ed.Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex))
        pub.verify(bytes.fromhex(sig_hex), SIGN_DOMAIN + str(entry_hash_hex).encode("ascii"))
        return True
    except Exception:
        return False


# ---- hashing ----------------------------------------------------------------

def canonical_json(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def frame(*fields: bytes) -> bytes:
    """Length-prefix each field so no boundary can be forged."""
    out = bytearray()
    for f in fields:
        out += struct.pack(">Q", len(f)) + f
    return bytes(out)


def entry_hash(seq: int, ts: str, kind: str, body, prev_hash: str) -> str:
    preimage = frame(str(int(seq)).encode("ascii"), ts.encode("utf-8"), kind.encode("utf-8"),
                     canonical_json(body), prev_hash.encode("ascii"))
    return hashlib.sha256(preimage).hexdigest()


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def make_entry(seq: int, kind: str, body: dict, prev_hash: str, private_key,
               ts: Optional[str] = None) -> dict:
    """Build a complete, signed entry (does not write it)."""
    if kind not in KINDS:
        raise LedgerError(f"unknown entry kind {kind!r}")
    if not isinstance(body, dict):
        raise LedgerError("entry body must be a JSON object")
    encoded = canonical_json(body)
    if len(encoded) > MAX_BODY_BYTES:
        raise LedgerError(f"entry body of {len(encoded)} bytes exceeds {MAX_BODY_BYTES}")
    ts = ts or utc_now()
    digest = entry_hash(seq, ts, kind, body, prev_hash)
    return {"seq": int(seq), "ts": ts, "kind": kind, "body": body, "prev_hash": prev_hash,
            "entry_hash": digest, "sig": sign_entry_hash(private_key, digest)}


def encode_line(entry: dict) -> bytes:
    return (json.dumps(entry, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            + "\n").encode("utf-8")


def parse_line(raw: bytes) -> dict:
    """Parse one ledger line. Raises ValueError on anything that is not an entry."""
    entry = json.loads(raw.decode("utf-8"))
    if not isinstance(entry, dict):
        raise ValueError("entry is not a JSON object")
    for key in ("seq", "ts", "kind", "body", "prev_hash", "entry_hash", "sig"):
        if key not in entry:
            raise ValueError(f"missing field {key}")
    if not isinstance(entry["seq"], int) or isinstance(entry["seq"], bool):
        raise ValueError("seq is not an integer")
    if not isinstance(entry["body"], dict):
        raise ValueError("body is not an object")
    for key in ("ts", "kind", "prev_hash", "entry_hash", "sig"):
        if not isinstance(entry[key], str):
            raise ValueError(f"{key} is not a string")
    return entry


# ---- writer -------------------------------------------------------------------

def scan_tail(path: str) -> tuple:
    """Return (last_entry or None, torn_offset or None, size) for an existing file.

    Reads only the tail, so it costs the same for a ledger of any length.
    A file that does not end in a newline, or whose final line is not an
    entry, has a torn tail; the offset is where the last complete line ends.
    """
    size = os.path.getsize(path)
    if size == 0:
        return None, None, 0
    with open(path, "rb") as fh:
        window = min(size, 1 << 20)
        fh.seek(size - window)
        chunk = fh.read(window)
    if not chunk.endswith(b"\n"):
        cut = chunk.rfind(b"\n")
        good_end = size - window + cut + 1 if cut >= 0 else 0
        return _last_entry_before(chunk[:cut + 1] if cut >= 0 else b""), good_end, size
    body = chunk[:-1]
    cut = body.rfind(b"\n")
    last = body[cut + 1:]
    try:
        return parse_line(last), None, size
    except ValueError:
        return _last_entry_before(body[:cut + 1] if cut >= 0 else b""), size - len(last) - 1, size


def _last_entry_before(chunk: bytes):
    lines = [ln for ln in chunk.split(b"\n") if ln]
    for raw in reversed(lines):
        try:
            return parse_line(raw)
        except ValueError:
            continue
    return None


class LedgerWriter:
    """One scribe, exclusive lock, fsync after every entry, append-only."""

    def __init__(self, path: str, private_key, writer: str = "jarvis",
                 lock: bool = True):
        self.path = path
        self.key = private_key
        self.pubkey = public_key_hex(private_key)
        self.writer = writer
        self._lock = threading.Lock()
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o640)
        if lock:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                os.close(self._fd)
                raise LedgerLocked(f"{path} is held by another writer") from e
        try:
            last, torn, size = scan_tail(path)
            if torn is not None:
                raise TornTail(path, torn)
            if last is None:
                self.seq = -1
                self.head_hash = GENESIS_PREV
                self._append("genesis", {"format": FORMAT, "pubkey": self.pubkey,
                                         "writer": writer, "created": utc_now()})
            else:
                self.seq = last["seq"]
                self.head_hash = last["entry_hash"]
                genesis_pub = self._genesis_pubkey()
                if genesis_pub and genesis_pub != self.pubkey:
                    raise LedgerError(f"{path} was started under key {genesis_pub[:12]}…, "
                                      f"not this writer's key {self.pubkey[:12]}…")
        except Exception:
            os.close(self._fd)
            raise

    def _genesis_pubkey(self) -> Optional[str]:
        with open(self.path, "rb") as fh:
            first = fh.readline()
        try:
            entry = parse_line(first)
        except ValueError:
            return None
        return entry["body"].get("pubkey") if entry["seq"] == 0 else None

    @property
    def head(self) -> dict:
        return {"seq": self.seq, "entry_hash": self.head_hash}

    def append(self, kind: str, body: dict) -> dict:
        if kind == "genesis":
            raise LedgerError("genesis is written once, by the writer itself")
        return self._append(kind, body)

    def _append(self, kind: str, body: dict) -> dict:
        with self._lock:
            entry = make_entry(self.seq + 1, kind, body, self.head_hash, self.key)
            line = encode_line(entry)
            written = os.write(self._fd, line)
            if written != len(line):  # pragma: no cover - partial write on a regular file
                raise LedgerError(f"short write to {self.path}")
            os.fsync(self._fd)
            self.seq = entry["seq"]
            self.head_hash = entry["entry_hash"]
            return entry

    def close(self):
        try:
            os.close(self._fd)
        except OSError:
            pass


def repair_torn_tail(path: str) -> Optional[int]:
    """Truncate a partial final line. Returns the bytes removed, or None if intact.

    This is the one documented repair; it is an operator action, never the
    agent's. Nothing before the torn line is touched.
    """
    _, torn, size = scan_tail(path)
    if torn is None:
        return None
    with open(path, "r+b") as fh:
        fh.truncate(torn)
        fh.flush()
        os.fsync(fh.fileno())
    return size - torn
