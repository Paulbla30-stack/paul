"""Standing refusals. Boot gate: pytest tests/standing_refusals -m refusals -q -p no:cacheprovider"""
from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(os.environ.get("JARVIS_REPO") or pathlib.Path(__file__).resolve().parents[2])
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def pytest_configure(config):
    config.addinivalue_line("markers", "refusals: page 8 standing refusals — the boot gate")
    config.addinivalue_line("markers", "hardening: review invariants — expected to fail until built")
