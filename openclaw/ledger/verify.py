"""Glass Ledger verifier: read-only, streaming, never a traceback.

For every entry, four checks: the sequence climbs by exactly 1, prev_hash
links to the previous entry's hash, the stored hash matches a fresh
recomputation, and the signature verifies under the *pinned* public key
(genesis must commit that same key, so a wholesale rewrite under a fresh
key dies at seq 0). A checkpoint pin, ``(seq, entry_hash)`` remembered
somewhere the writer cannot reach, closes the rollback blind spot: a
chain that never reaches the pin, or carries a different hash at the
pinned seq, is BROKEN. A partial final line is a torn tail: reported as
INTACT (torn tail) with a repair instruction, never as an attack, unless
a pin lies at or past it.

Verdicts, never exceptions: hostile input comes back as
``BROKEN at seq N: reason``.
"""

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Iterable, Optional

from openclaw.ledger.chain import (FORMAT, GENESIS_PREV, KINDS, entry_hash, parse_line,
                                   verify_signature, utc_now)


@dataclass
class Report:
    verdict: str = "BROKEN"          # INTACT | BROKEN
    entries: int = 0                 # intact prefix length (entries that verified)
    head_seq: Optional[int] = None
    head_hash: Optional[str] = None
    reason: str = ""
    broken_seq: Optional[int] = None
    torn_tail: bool = False
    torn_offset: Optional[int] = None  # byte offset where the good tail ends
    pubkey: Optional[str] = None       # key the chain was verified under
    pinned_key: bool = False           # pubkey came from the caller, not the file
    pin: Optional[dict] = None
    pin_ok: Optional[bool] = None
    writer: Optional[str] = None
    kinds: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.verdict == "INTACT"

    def summary(self) -> str:
        if self.ok:
            head = f"{self.entries} entries, head {self.head_seq} {(self.head_hash or '')[:12]}…"
            text = f"INTACT (torn tail; {head})" if self.torn_tail else f"INTACT ({head})"
            if self.pin_ok:
                text += f"; pin seq {self.pin['seq']} confirmed"
            if not self.pinned_key:
                text += "; key taken from the file, not pinned"
            return text
        where = f" at seq {self.broken_seq}" if self.broken_seq is not None else ""
        return f"BROKEN{where}: {self.reason} (intact prefix: {self.entries} entries)"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ok"] = self.ok
        d["summary"] = self.summary()
        return d


def verify_lines(lines: Iterable[bytes], pubkey: Optional[str] = None,
                 pin: Optional[dict] = None, torn_offset: Optional[int] = None,
                 last_line_complete: bool = True) -> Report:
    """Verify an iterable of raw lines (each without its trailing newline).

    ``torn_offset`` / ``last_line_complete`` describe the file's tail as
    seen by the caller: a final line without its newline is a candidate torn
    tail and is judged leniently (INTACT if it verifies, torn if it does not).
    """
    report = Report(pubkey=pubkey, pinned_key=bool(pubkey), pin=pin)
    expected_seq = 0
    prev = GENESIS_PREV
    pin_seq = None
    if pin:
        try:
            pin_seq = int(pin["seq"])
            pin_hash = str(pin["entry_hash"])
        except (KeyError, TypeError, ValueError):
            report.reason = "pin file is not {seq, entry_hash}"
            return report
    materialised = [ln for ln in lines]
    if materialised and materialised[-1] == b"" and last_line_complete:
        materialised.pop()
    total = len(materialised)
    if total == 0 or (total == 1 and materialised[0] == b""):
        report.reason = "empty ledger: no genesis entry"
        return report
    for index, raw in enumerate(materialised):
        is_last = index == total - 1
        candidate_torn = is_last and not last_line_complete
        try:
            entry = parse_line(raw)
            if entry["seq"] != expected_seq:
                raise ValueError(f"sequence gap: expected {expected_seq}, found {entry['seq']}")
            if entry["prev_hash"] != prev:
                raise ValueError("prev_hash does not link to the previous entry")
            if entry["kind"] not in KINDS:
                raise ValueError(f"unknown kind {entry['kind']!r}")
            digest = entry_hash(entry["seq"], entry["ts"], entry["kind"], entry["body"],
                                entry["prev_hash"])
            if digest != entry["entry_hash"]:
                raise ValueError("entry_hash does not match a fresh recomputation")
            if expected_seq == 0:
                genesis_key = entry["body"].get("pubkey")
                if entry["body"].get("format") != FORMAT:
                    raise ValueError(f"genesis format is not {FORMAT}")
                if not isinstance(genesis_key, str) or len(genesis_key) != 64:
                    raise ValueError("genesis does not commit a public key")
                if pubkey is None:
                    pubkey = genesis_key
                    report.pubkey = pubkey
                elif genesis_key.lower() != pubkey.lower():
                    raise ValueError("genesis commits a different public key than the one pinned")
                report.writer = entry["body"].get("writer")
            if not verify_signature(pubkey, digest, entry["sig"]):
                raise ValueError("signature does not verify under the pinned key")
        except (ValueError, TypeError, KeyError, UnicodeDecodeError) as e:
            if candidate_torn:
                report.torn_tail = True
                report.torn_offset = torn_offset
                break
            report.broken_seq = expected_seq
            report.reason = str(e) if isinstance(e, ValueError) else f"malformed entry ({e})"
            return _finish(report, pin_seq, pin if pin else None)
        except Exception as e:  # hostile input of any other shape
            report.broken_seq = expected_seq
            report.reason = f"unreadable entry ({type(e).__name__})"
            return _finish(report, pin_seq, pin if pin else None)
        if pin_seq is not None and entry["seq"] == pin_seq:
            if entry["entry_hash"] != pin_hash:
                report.broken_seq = expected_seq
                report.reason = "regenerated: the entry at the pinned seq carries a different hash"
                report.pin_ok = False
                return report
            report.pin_ok = True
        report.entries += 1
        report.head_seq = entry["seq"]
        report.head_hash = entry["entry_hash"]
        report.kinds[entry["kind"]] = report.kinds.get(entry["kind"], 0) + 1
        expected_seq += 1
        prev = entry["entry_hash"]
    if report.entries == 0:
        report.reason = "no genesis entry" + (" (torn first line)" if report.torn_tail else "")
        report.broken_seq = 0
        return report
    report.verdict = "INTACT"
    return _finish(report, pin_seq, pin if pin else None)


def _finish(report: Report, pin_seq, pin) -> Report:
    if pin_seq is not None and report.pin_ok is None:
        report.pin_ok = False
        report.verdict = "BROKEN"
        report.reason = (f"truncated or rolled back: chain ends at seq {report.head_seq} "
                         f"before the pinned seq {pin_seq}")
        if report.broken_seq is None:
            report.broken_seq = (report.head_seq + 1) if report.head_seq is not None else 0
    return report


def verify_file(path: str, pubkey: Optional[str] = None, pin: Optional[dict] = None) -> Report:
    """Stream a ledger file and return its Report. Never raises."""
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return Report(reason=f"cannot read ledger: {e}", pubkey=pubkey, pinned_key=bool(pubkey))
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as e:
        return Report(reason=f"cannot read ledger: {e}", pubkey=pubkey, pinned_key=bool(pubkey))
    complete = data.endswith(b"\n")
    lines = data.split(b"\n")
    if complete:
        lines = lines[:-1]
    torn_offset = None
    if not complete and size:
        torn_offset = size - len(lines[-1]) if lines else 0
    try:
        return verify_lines(lines, pubkey=pubkey, pin=pin, torn_offset=torn_offset,
                            last_line_complete=complete)
    except Exception as e:  # pragma: no cover - the verifier must not crash
        return Report(reason=f"verifier error ({type(e).__name__}: {e})", pubkey=pubkey,
                      pinned_key=bool(pubkey))


def load_pin(path: str) -> Optional[dict]:
    """Read a checkpoint pin file; None when it does not exist yet."""
    if not path or not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        pin = json.load(fh)
    if not isinstance(pin, dict) or "seq" not in pin or "entry_hash" not in pin:
        raise ValueError(f"{path} is not a pin file")
    return pin


def save_pin(path: str, seq: int, entry_hash_hex: str) -> dict:
    """Advance the pin atomically after a good run."""
    pin = {"seq": int(seq), "entry_hash": entry_hash_hex, "verified_at": utc_now()}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(pin, fh, indent=1)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return pin
