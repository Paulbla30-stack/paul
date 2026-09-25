#!/usr/bin/env python3
"""Apply reviewed metadata to published Zenodo records.

The estate's discoverability lives in its metadata, not its text: keywords
and related_identifiers are what the academic aggregators index and what a
topic search can match. One record went out with neither, and it is the one
sitting on the standards revision that is live right now.

What goes on each record is DATA, not code: it lives in a plan file so it can
be read and argued with before anything is published. This script only
applies it.

Metadata on a published Zenodo record is editable without minting a new
version (edit -> update -> publish). Files are not, and this script never
touches them. It adds only the fields named in the plan and leaves every
other field exactly as it found it, because the legacy update is a wholesale
replace and a partial payload silently drops whatever it omits.

Dry run is the default. Nothing reaches Zenodo without --apply.

Usage:
    zenodo_meta.py --plan zenodo_plan.json                 # show the diff
    zenodo_meta.py --plan zenodo_plan.json --apply         # publish it
    zenodo_meta.py --plan zenodo_plan.json --record 22771015 --apply
"""

import argparse
import copy
import json
import os
import sys
import urllib.error
import urllib.request

TOKEN_PARAM = "/hbf/zenodo-token"
REGION = "us-west-2"
PROFILE = "openclaw"
API = "https://zenodo.org/api/deposit/depositions"
SHADOWING = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")

# Only these may be written. Anything else in a plan entry is refused, so a
# stray key cannot quietly rewrite a title, a licence or an author list.
# `version` is here because one record carries a whole sentence where the
# other nineteen carry a version string, and aggregators read that field.
WRITABLE = {"keywords", "related_identifiers", "language", "subjects", "version"}


def read_token() -> str:
    for name in SHADOWING:
        os.environ.pop(name, None)
    import boto3
    ssm = boto3.Session(profile_name=PROFILE, region_name=REGION).client("ssm")
    return ssm.get_parameter(Name=TOKEN_PARAM, WithDecryption=True)["Parameter"]["Value"].strip()


def api(token, url, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            body = r.read()
            return r.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:500].decode("utf-8", "replace")


def describe(md: dict) -> str:
    ver = str(md.get("version") or "")
    if len(ver) > 18:
        ver = ver[:15] + "..."
    return (f"keywords={len(md.get('keywords') or [])} "
            f"related={len(md.get('related_identifiers') or [])} "
            f"subjects={len(md.get('subjects') or [])} "
            f"language={md.get('language')!r} "
            f"version={ver!r}")


def apply_one(token, record_id: str, changes: dict, apply: bool) -> bool:
    base = f"{API}/{record_id}"
    code, dep = api(token, base)
    if code != 200:
        print(f"  ! fetch failed: HTTP {code} {str(dep)[:120]}")
        return False

    before = dep.get("metadata") or {}
    print(f"  before: {describe(before)}")
    after_preview = dict(before, **changes)
    print(f"  after : {describe(after_preview)}")
    if not apply:
        print("  (dry run — nothing sent)")
        return True

    code, edited = api(token, f"{base}/actions/edit", "POST")
    if code not in (200, 201):
        print(f"  ! edit failed: HTTP {code} {str(edited)[:200]}")
        return False

    md = copy.deepcopy((edited or {}).get("metadata") or before)
    md.update(changes)
    # Zenodo rejects this on update; it is only meaningful at creation.
    md.pop("prereserve_doi", None)

    code, res = api(token, base, "PUT", {"metadata": md})
    if code != 200:
        print(f"  ! update failed: HTTP {code} {str(res)[:200]}")
        discard = api(token, f"{base}/actions/discard", "POST")[0]
        print(f"  ! edit discarded (HTTP {discard}); record left as it was")
        return False

    code, _ = api(token, f"{base}/actions/publish", "POST")
    if code not in (200, 202):
        print(f"  ! PUBLISH FAILED (HTTP {code}) — record is UNLOCKED, not published")
        return False

    code, now = api(token, base)
    print(f"  after : {describe(now.get('metadata') or {})}  [published, doi {now.get('doi')}]")
    return True


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--plan", required=True, help="JSON: {record_id: {field: value}}")
    p.add_argument("--record", help="apply only this record id from the plan")
    p.add_argument("--apply", action="store_true", help="actually publish (default: dry run)")
    args = p.parse_args()

    with open(args.plan) as fh:
        plan = json.load(fh)
    if args.record:
        if args.record not in plan:
            print(f"{args.record} is not in the plan", file=sys.stderr)
            return 2
        plan = {args.record: plan[args.record]}

    for rid, changes in plan.items():
        stray = set(changes) - WRITABLE
        if stray:
            print(f"{rid}: refusing, plan names non-writable field(s): {sorted(stray)}",
                  file=sys.stderr)
            return 2

    token = read_token()
    print(f"{'APPLYING' if args.apply else 'DRY RUN'} — {len(plan)} record(s)\n")
    failed = 0
    for rid, changes in plan.items():
        print(f"{rid}:")
        if not apply_one(token, rid, changes, args.apply):
            failed += 1
        print()
    print(f"done: {len(plan) - failed} ok, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
