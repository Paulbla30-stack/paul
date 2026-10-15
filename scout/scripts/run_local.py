#!/usr/bin/env python3
"""Run the scout locally against the live APIs.

    python3 scripts/run_local.py            # fetch, score, print. Writes nothing.
    python3 scripts/run_local.py --commit   # also write to .local/ and the chain
    python3 scripts/run_local.py --show 40  # how many rows to print

Never sends email. Never touches AWS. This is the rehearsal.
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import app                                              # noqa: E402
from chain import Chain, LocalChainStore                # noqa: E402
from store import LocalHitStore                         # noqa: E402


class NullMailer:
    def send(self, *a, **k):
        print("\n[no email: this is a local run]")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="write to .local/ and the chain")
    ap.add_argument("--show", type=int, default=25)
    ap.add_argument("--config", default=os.path.join(ROOT, "config.toml"))
    a = ap.parse_args()

    local = os.path.join(ROOT, ".local")
    os.makedirs(local, exist_ok=True)
    cfg = app.load_config(a.config)

    r = app.run(
        cfg,
        hit_store=LocalHitStore(os.path.join(local, "hits.json")),
        chain_store=LocalChainStore(os.path.join(local, "chain.jsonl")),
        mailer=NullMailer(),
        dry_run=not a.commit,
    )
    s = r["stats"]
    print("\n" + "=" * 78)
    print(f"fetched {s['fetched']} | new {s['new']} | above threshold "
          f"{s['above']} (>= {s['threshold']:g}) | vetoed {s['vetoed']}")
    print(f"sources ok: {', '.join(s['sources_ok']) or 'none'}"
          + (f" | FAILED: {', '.join(s['sources_failed'])}" if s['sources_failed'] else ""))
    print("=" * 78)

    rows = sorted(r["all_new"], key=lambda x: x["score"], reverse=True)[: a.show]
    for i, rec in enumerate(rows, 1):
        flag = "*" if rec["score"] >= s["threshold"] else " "
        print(f"{flag}{i:3d}. {rec['score']:7.2f}  [{rec['source']:10s}] {rec['title'][:64]}")
        print(f"            {rec['why'][:100]}")
    if a.commit:
        v = Chain(LocalChainStore(os.path.join(local, "chain.jsonl"))).verify()
        print(f"\nchain: {v}")
    print("\n(* = would appear in the digest)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
