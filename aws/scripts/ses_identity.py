#!/usr/bin/env python3
"""Set up a domain to send mail through SES, and print the DNS it needs.

The scout's digest currently sends as a hotmail.com address. hotmail.com's
SPF record does not list Amazon SES, so the mail is unauthenticated and
arrives only because consumer DMARC is permissive. The fix is a domain the
operator controls the DNS for, verified with Easy DKIM.

This creates the SES identity and prints the records. **It changes no DNS.**
AWS has no access to the domain's nameservers; the CNAMEs and the SPF change
have to be made in Cloudflare by hand, and verification completes only once
they resolve. Nothing here can publish anything on the operator's behalf.

Also worth knowing before running it: SES on this account is in sandbox
(ProductionAccessEnabled false), so a verified domain lets you *send as* any
address on it, and the *recipient* must still be a verified identity. That is
already true of the hotmail address the digest goes to.

Dry run is the default. Nothing is created without --apply.

Usage:
    ses_identity.py --domain heartbeat-framework.org
    ses_identity.py --domain heartbeat-framework.org --apply
    ses_identity.py --domain heartbeat-framework.org --show     # existing records
"""

import argparse
import os
import sys

REGION = "us-west-2"
PROFILE = "openclaw"
SHADOWING = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")


def records(domain: str, tokens) -> list:
    """The CNAMEs Easy DKIM needs. Three of them, one per signing key."""
    return [(f"{t}._domainkey.{domain}", f"{t}.dkim.amazonses.com") for t in tokens]


def report(domain: str, identity: dict, spf: str) -> None:
    dk = identity.get("DkimAttributes") or {}
    print(f"\n{domain}")
    print(f"  verified for sending : {identity.get('VerifiedForSendingStatus')}")
    print(f"  verification status  : {identity.get('VerificationStatus')}")
    print(f"  dkim status          : {dk.get('Status')}  (PENDING until the CNAMEs resolve)")

    rows = records(domain, dk.get("Tokens") or [])
    if not rows:
        print("\n  no DKIM tokens yet — nothing to add")
        return
    print("\n  Add these in Cloudflare as CNAME, DNS only (grey cloud, NOT proxied):\n")
    for name, target in rows:
        print(f"    {name}")
        print(f"      -> {target}\n")
    print("  And change the SPF TXT record on the apex to authorise SES:\n")
    print(f"    current : {spf or '(none found)'}")
    if spf and "amazonses.com" not in spf:
        print(f"    becomes : {spf.replace(' ~all', ' include:amazonses.com ~all')}")
    elif not spf:
        print("    becomes : v=spf1 include:amazonses.com ~all")
    else:
        print("    becomes : (already lists amazonses.com — leave it)")
    print("\n  Verification completes on its own once those resolve, usually minutes.")
    print("  Then set sender in scout/config.toml to an address on this domain.")


def current_spf(domain: str) -> str:
    """Read the apex SPF over DNS-over-HTTPS. Read-only; changes nothing."""
    import json
    import urllib.request
    try:
        req = urllib.request.Request(
            f"https://dns.google/resolve?name={domain}&type=TXT",
            headers={"Accept": "application/dns-json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
        for a in data.get("Answer", []):
            txt = (a.get("data") or "").strip('"')
            if txt.startswith("v=spf1"):
                return txt
    except Exception as exc:                       # noqa: BLE001
        print(f"  (could not read SPF: {type(exc).__name__} {exc})", file=sys.stderr)
    return ""


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--domain", required=True)
    p.add_argument("--apply", action="store_true",
                   help="actually create the identity (default: dry run)")
    p.add_argument("--show", action="store_true",
                   help="only report an identity that already exists")
    p.add_argument("--region", default=REGION)
    p.add_argument("--profile", default=PROFILE)
    args = p.parse_args()

    for name in SHADOWING:
        os.environ.pop(name, None)
    import boto3
    ses = boto3.Session(profile_name=args.profile,
                        region_name=args.region).client("sesv2")

    try:
        existing = ses.get_email_identity(EmailIdentity=args.domain)
    except ses.exceptions.NotFoundException:
        existing = None

    if existing:
        print(f"identity already exists for {args.domain}")
        report(args.domain, existing, current_spf(args.domain))
        return 0
    if args.show:
        print(f"no SES identity for {args.domain}")
        return 1
    if not args.apply:
        print(f"DRY RUN — would create a DKIM-signing domain identity for {args.domain}")
        print("  creates no DNS records and sends no mail; re-run with --apply")
        return 0

    ses.create_email_identity(
        EmailIdentity=args.domain,
        DkimSigningAttributes={"NextSigningKeyLength": "RSA_2048_BIT"})
    print(f"created domain identity: {args.domain}")
    report(args.domain, ses.get_email_identity(EmailIdentity=args.domain),
           current_spf(args.domain))
    return 0


if __name__ == "__main__":
    sys.exit(main())
