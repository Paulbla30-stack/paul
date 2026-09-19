#!/usr/bin/env python3
"""Audit a Jarvis agent's Glass Ledger from a machine the agent does not administer.

    python3 aws/scripts/ledger_audit.py --bucket jarvis-ledger-<acct>-<name> \
        --writer <agent name> --region us-west-2 --pubkey <64 hex> --pin build/ledger.pin

Fetches the witness copy the instance anchored into the Object Lock bucket
(``ledger/<writer>/ledger.jsonl``) and the latest checkpoint the instance
wrote, verifies the copy under the public key you pinned (never the key
the file offers), checks that the chain still extends your local pin,
compares the instance's own checkpoint against the chain, and advances
the pin on a good run. ``--file`` audits a local copy instead (for
example one pulled over SSM). Exit status 0 for INTACT, 1 for BROKEN.

Keep the pin file and the public key where the agent cannot reach them:
this machine, not the instance. That separation is the instrument.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from jarvis.ledger.verify import verify_file, load_pin, save_pin  # noqa: E402


def fetch(bucket: str, writer: str, region: str, profile: str, out_dir: str) -> tuple:
    import boto3
    session = boto3.Session(profile_name=profile, region_name=region) if profile else boto3.Session(region_name=region)
    s3 = session.client("s3")
    os.makedirs(out_dir, exist_ok=True)
    copy_path = os.path.join(out_dir, f"{writer}-ledger.jsonl")
    s3.download_file(bucket, f"ledger/{writer}/ledger.jsonl", copy_path)
    checkpoint = None
    try:
        obj = s3.get_object(Bucket=bucket, Key=f"ledger/{writer}/checkpoint.json")
        checkpoint = json.loads(obj["Body"].read())
    except Exception as e:  # a missing checkpoint is reported, not fatal
        print(f"note: no checkpoint.json in the bucket ({type(e).__name__})")
    versions = s3.list_object_versions(Bucket=bucket, Prefix=f"ledger/{writer}/ledger.jsonl")
    n_versions = len(versions.get("Versions", []))
    return copy_path, checkpoint, n_versions


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--bucket", help="the Object Lock witness bucket (terraform output ledger_bucket)")
    src.add_argument("--file", help="audit a local copy of the ledger instead")
    ap.add_argument("--writer", help="agent name used as the key prefix (terraform var name)")
    ap.add_argument("--region", default=None)
    ap.add_argument("--profile", default=None, help="AWS CLI profile for the auditor (not the agent's role)")
    ap.add_argument("--pubkey", required=True, help="64-hex Ed25519 public key you recorded when the agent first started")
    ap.add_argument("--pin", default="build/ledger.pin", help="checkpoint pin file kept on this machine")
    ap.add_argument("--out-dir", default="build/ledger-audit")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    checkpoint = None
    n_versions = None
    if args.bucket:
        if not args.writer:
            ap.error("--writer is required with --bucket")
        try:
            path, checkpoint, n_versions = fetch(args.bucket, args.writer, args.region, args.profile, args.out_dir)
        except Exception as e:
            print(f"BROKEN: could not fetch the witness copy: {type(e).__name__}: {e}")
            return 1
    else:
        path = args.file

    try:
        pin = load_pin(args.pin)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"BROKEN: cannot read pin {args.pin}: {e}")
        return 1
    report = verify_file(path, pubkey=args.pubkey, pin=pin)
    findings = []
    if checkpoint:
        if str(checkpoint.get("pubkey", "")).lower() != args.pubkey.lower():
            findings.append("the instance's checkpoint names a different public key than the one pinned")
        cp_seq = checkpoint.get("seq")
        if report.ok and isinstance(cp_seq, int):
            if cp_seq > (report.head_seq or -1):
                findings.append(f"the instance's checkpoint (seq {cp_seq}) is ahead of the copy (seq {report.head_seq}): copy is stale or rolled back")
            else:
                # the entry at the checkpoint seq must carry the checkpoint hash
                sub = verify_file(path, pubkey=args.pubkey, pin={"seq": cp_seq, "entry_hash": checkpoint.get("entry_hash")})
                if not sub.ok:
                    findings.append(f"the instance's checkpoint does not match the chain: {sub.reason}")
    verdict = report.ok and not findings
    out = report.to_dict()
    out["source"] = f"s3://{args.bucket}/ledger/{args.writer}/ledger.jsonl" if args.bucket else path
    out["copy_versions_in_bucket"] = n_versions
    out["instance_checkpoint"] = checkpoint
    out["findings"] = findings
    out["ok"] = verdict
    if args.json:
        print(json.dumps(out, indent=1))
    else:
        print(f"source: {out['source']}" + (f"  ({n_versions} retained versions)" if n_versions is not None else ""))
        print(report.summary())
        if checkpoint:
            print(f"instance checkpoint: seq {checkpoint.get('seq')} {str(checkpoint.get('entry_hash'))[:12]}… at {checkpoint.get('anchored_at')}")
        for f in findings:
            print(f"FINDING: {f}")
        if report.torn_tail:
            print("torn tail in the copy: the instance lost power mid-write; repair on the instance, never here")
    if verdict and report.head_seq is not None:
        save_pin(args.pin, report.head_seq, report.head_hash)
        if not args.json:
            print(f"pin advanced to seq {report.head_seq} ({args.pin})")
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
