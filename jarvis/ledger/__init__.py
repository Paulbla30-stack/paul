"""The Glass Ledger: a tamper-evident decision journal for the agent.

Implements chain format v2 from Blatherwick P., *The Glass Ledger v2*
(DOI 10.5281/zenodo.21515861, CC BY 4.0; reference code MIT): one JSON
line per entry, each carrying the SHA-256 fingerprint of the entry
before it and an Ed25519 signature over its own fingerprint, hardened by
length-prefix framing, canonical JSON, domain-separated signing and a
strict append-only rule. ``chain`` is the format and writer, ``verify``
the read-only auditor, ``anchor`` the off-box witness copy, and
``agent_ledger`` the facade the agent records through.
"""

from jarvis.ledger.chain import (FORMAT, GENESIS_PREV, KINDS, LedgerError, LedgerLocked,
                                   TornTail, canonical_json, entry_hash, frame, generate_key,
                                   LedgerWriter)
from jarvis.ledger.verify import Report, verify_file, verify_lines, load_pin, save_pin
from jarvis.ledger.agent_ledger import AgentLedger

__all__ = ["FORMAT", "GENESIS_PREV", "KINDS", "LedgerError", "LedgerLocked", "TornTail",
           "canonical_json", "entry_hash", "frame", "generate_key", "LedgerWriter",
           "Report", "verify_file", "verify_lines", "load_pin", "save_pin", "AgentLedger"]
