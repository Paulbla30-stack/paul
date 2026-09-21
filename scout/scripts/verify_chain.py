#!/usr/bin/env python3
"""Verify the scout's hash chain.

Walks every entry from the first, recomputes each hash from the stored
content, and checks each entry's prev_hash against the actual hash of the
entry before it. Reports the first entry that fails and stops there.

    python3 scripts/verify_chain.py --table jarvis-scout --profile openclaw
    python3 scripts/verify_chain.py --file .local/chain.jsonl

Exit code 0 means the chain verifies, 1 means it does not.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from chain import Chain, DynamoChainStore, LocalChainStore  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--table", help="DynamoDB table name")
    g.add_argument("--file", help="local chain .jsonl")
    ap.add_argument("--profile", help="AWS profile (with --table)")
    ap.add_argument("--region", default="us-west-2")
    a = ap.parse_args()

    if a.table:
        import boto3
        s = boto3.Session(profile_name=a.profile) if a.profile else boto3.Session()
        store = DynamoChainStore(
            a.table,
            dynamodb=s.resource("dynamodb", region_name=a.region),
            client=s.client("dynamodb", region_name=a.region),
        )
        where = f"table {a.table} ({a.region})"
    else:
        store = LocalChainStore(a.file)
        where = a.file

    r = Chain(store).verify()
    print(f"chain: {where}")
    if r["ok"]:
        print(f"  VERIFIED  {r['entries']} entries")
        print(f"  head hash {r['head_hash']}")
        print("\n  Each entry's stored hash was recomputed from its own content and")
        print("  matched, and each prev_hash matched the entry before it.")
        return 0

    print("  FAILED")
    print(f"  first bad entry : seq {r['first_bad_seq']}")
    print(f"  kind            : {r['kind']}")
    print(f"  detail          : {r['detail']}")
    print(f"  verified before : {r['entries_verified_before_failure']} entries")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
