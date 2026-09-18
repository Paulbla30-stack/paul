"""Command-line auditor for a Glass Ledger.

    python -m openclaw.ledger verify LEDGER --pubkey HEX [--pin FILE] [--json]
    python -m openclaw.ledger LEDGER --pubkey HEX --pin FILE      (same thing)
    python -m openclaw.ledger tail LEDGER [-n 10]
    python -m openclaw.ledger keygen DIR
    python -m openclaw.ledger pubkey KEYFILE
    python -m openclaw.ledger repair LEDGER

Run ``verify`` from a machine the writer does not administer, against a
copy of the file, with the public key you know and the pin you keep. A
good run advances the pin. Exit status 0 for INTACT, 1 for BROKEN.
"""

import argparse
import json
import os
import sys

from openclaw.ledger.chain import (generate_key, load_private_key, public_key_hex,
                                   repair_torn_tail, parse_line, scan_tail)
from openclaw.ledger.verify import verify_file, load_pin, save_pin

COMMANDS = ("verify", "tail", "keygen", "pubkey", "repair")


def cmd_verify(args) -> int:
    pin = None
    if args.pin:
        try:
            pin = load_pin(args.pin)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print(f"BROKEN: cannot read pin {args.pin}: {e}")
            return 1
    report = verify_file(args.ledger, pubkey=args.pubkey, pin=pin)
    if args.json:
        print(json.dumps(report.to_dict(), indent=1))
    else:
        print(report.summary())
        if report.torn_tail:
            print(f"repair: python -m openclaw.ledger repair {args.ledger}   "
                  f"(truncates to byte {report.torn_offset})")
    if report.ok and args.pin and report.head_seq is not None:
        save_pin(args.pin, report.head_seq, report.head_hash)
        if not args.json:
            print(f"pin advanced to seq {report.head_seq}")
    return 0 if report.ok else 1


def cmd_tail(args) -> int:
    if not os.path.exists(args.ledger):
        print(f"no ledger at {args.ledger}")
        return 1
    with open(args.ledger, "rb") as fh:
        lines = [ln for ln in fh.read().split(b"\n") if ln][-args.n:]
    for raw in lines:
        try:
            entry = parse_line(raw)
            print(f"{entry['seq']:>7} {entry['ts']} {entry['kind']:<9} "
                  f"{json.dumps(entry['body'], ensure_ascii=False)[:160]}")
        except ValueError as e:
            print(f"   ???? unparseable line ({e})")
    return 0


def cmd_keygen(args) -> int:
    os.makedirs(args.dir, mode=0o700, exist_ok=True)
    key_path = os.path.join(args.dir, "ed25519.key")
    pub_path = os.path.join(args.dir, "ed25519.pub")
    if os.path.exists(key_path):
        print(f"refusing to overwrite {key_path}")
        return 1
    seed, pub = generate_key()
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(seed + "\n")
    with open(pub_path, "w") as fh:
        fh.write(pub + "\n")
    print(f"private key: {key_path}\npublic key:  {pub}")
    return 0


def cmd_pubkey(args) -> int:
    with open(args.keyfile) as fh:
        print(public_key_hex(load_private_key(fh.read())))
    return 0


def cmd_repair(args) -> int:
    _, torn, size = scan_tail(args.ledger)
    if torn is None:
        print("no torn tail; nothing to repair")
        return 0
    removed = repair_torn_tail(args.ledger)
    print(f"removed {removed} bytes of partial final line; ledger now ends at byte {torn}")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in COMMANDS and not argv[0].startswith("-"):
        argv.insert(0, "verify")  # the paper's form: <ledger> --pubkey … --pin …
    ap = argparse.ArgumentParser(prog="python -m openclaw.ledger", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="verify a ledger file")
    v.add_argument("ledger")
    v.add_argument("--pubkey", help="64-hex Ed25519 public key you trust (pin it; do not take it from the file)")
    v.add_argument("--pin", help="checkpoint pin file; checked, then advanced on a good run")
    v.add_argument("--json", action="store_true")
    t = sub.add_parser("tail", help="print the last entries")
    t.add_argument("ledger")
    t.add_argument("-n", type=int, default=10)
    k = sub.add_parser("keygen", help="create ed25519.key / ed25519.pub in a directory")
    k.add_argument("dir")
    p = sub.add_parser("pubkey", help="print the public key of a private key file")
    p.add_argument("keyfile")
    r = sub.add_parser("repair", help="truncate a torn tail (operator action)")
    r.add_argument("ledger")
    args = ap.parse_args(argv)
    try:
        return {"verify": cmd_verify, "tail": cmd_tail, "keygen": cmd_keygen,
                "pubkey": cmd_pubkey, "repair": cmd_repair}[args.cmd](args)
    except Exception as e:  # the auditor prints verdicts, not tracebacks
        print(f"BROKEN: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
