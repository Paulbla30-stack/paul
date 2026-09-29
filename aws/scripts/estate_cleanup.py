#!/usr/bin/env python3
"""Remove estate that nothing depends on any more.

Deleting cloud storage is the one operation with no undo, so this refuses to
guess. Every target is named explicitly, every target is checked against the
thing that would still need it, and the check runs immediately before the
delete rather than at review time.

Currently one target: s3://jarvis-models-485964361844, 80.78 GB of open-weight
model files (jarvis-qwen3-32b, jarvis-qwen25-7b) mirrored there so Bedrock
Custom Model Import could read them. Both imported models have been deleted,
so the weights back nothing. Re-importing would mean fetching them from
Hugging Face again, which is bandwidth and time rather than anything lost.

Deliberately NOT included, and not to be added casually:
  jarvis-ledger-*     the live Glass Ledger witness copy, Object Lock COMPLIANCE
  openclaw-ledger-*   the predecessor agent's ledger, audited INTACT before it
                      was retired. It costs nothing and it is a record; a
                      ledger you delete when it gets inconvenient was never a
                      ledger.
  *-tfstate           Terraform state. Losing it orphans every managed resource.
  jarvis-memory-*     the agent's durable memory.
  jarvis-scout-*      the scout's own storage.

Dry run is the default. Nothing is deleted without --apply.

Usage:
    estate_cleanup.py                 # show what would go
    estate_cleanup.py --apply         # delete it
"""

import argparse
import os
import sys

REGION = "us-west-2"
PROFILE = "openclaw"
SHADOWING = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")

MODELS_BUCKET = "jarvis-models-485964361844"

# A bucket may only be emptied when its guard returns True. The guard names the
# thing that would still need the data, and is re-run at delete time.
def no_imported_models(session) -> tuple:
    """True when Bedrock holds no imported model, so mirrored weights are dead."""
    br = session.client("bedrock")
    names = [m.get("modelName") for m in br.list_imported_models().get("modelSummaries", [])]
    return (not names), (f"imported models present: {names}" if names
                         else "no imported Bedrock models")

TARGETS = [(MODELS_BUCKET, no_imported_models)]


def bucket_stats(s3, bucket):
    total = count = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for o in page.get("Contents", []):
            total += o["Size"]
            count += 1
    return count, total


def empty_and_remove(s3, bucket) -> int:
    """Delete every object (and version), then the bucket. Returns objects removed."""
    removed = 0
    versioned = s3.get_bucket_versioning(Bucket=bucket).get("Status") == "Enabled"
    if versioned:
        for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket):
            batch = [{"Key": o["Key"], "VersionId": o["VersionId"]}
                     for o in page.get("Versions", []) + page.get("DeleteMarkers", [])]
            if batch:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
                removed += len(batch)
    else:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            objs = page.get("Contents", [])
            if objs:
                s3.delete_objects(Bucket=bucket,
                                  Delete={"Objects": [{"Key": o["Key"]} for o in objs],
                                          "Quiet": True})
                removed += len(objs)
    s3.delete_bucket(Bucket=bucket)
    return removed


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    args = p.parse_args()

    for name in SHADOWING:
        os.environ.pop(name, None)
    import boto3
    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    s3 = session.client("s3")

    print("APPLYING\n" if args.apply else "DRY RUN — nothing will be deleted\n")
    failed = 0
    for bucket, guard in TARGETS:
        print(f"{bucket}:")
        try:
            count, size = bucket_stats(s3, bucket)
        except Exception as exc:
            print(f"  ! cannot read bucket: {type(exc).__name__} {str(exc)[:90]}\n")
            failed += 1
            continue
        print(f"  holds {count} objects, {size / 1e9:.2f} GB")

        ok, why = guard(session)
        print(f"  guard: {why}")
        if not ok:
            print("  ! guard failed — skipping, nothing deleted\n")
            failed += 1
            continue
        if not args.apply:
            print("  would empty and remove this bucket\n")
            continue

        removed = empty_and_remove(s3, bucket)
        print(f"  removed {removed} objects and the bucket "
              f"(~{size / 1e9 * 0.023:.2f} USD/month freed)\n")

    print(f"done: {len(TARGETS) - failed} target(s) handled, {failed} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
