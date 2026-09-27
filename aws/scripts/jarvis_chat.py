#!/usr/bin/env python3
"""Say something to Jarvis over its /chat endpoint, without a shell reading it.

CLAUDE.md asks a coding agent to tell Jarvis what is changing before it lands,
and the only way in is SSM Run Command, which hands its commands to a shell.
On 27 September 2026 a message was built into a curl command line inside
double quotes. Every command name written in backticks in that message --
`xargs`, `awk`, `sort -o`, `journalctl --vacuum-*` -- was run as root by the
box's shell instead of reaching Jarvis as text. None of them changed anything,
by luck: the wildcard matched no file and the rest had no input.

A message is prose, and prose is full of the characters a shell acts on. So
the text never touches a command line here. It is JSON-encoded, then base64,
which is an alphabet with nothing in it a shell interprets; on the box it is
decoded to a file and posted with --data-binary @file.

Usage:
    jarvis_chat.py --file message.txt
    echo "..." | jarvis_chat.py
    jarvis_chat.py --file message.txt --dry-run     # print the SSM command only

Prints Jarvis's answer. Exits non-zero if the SSM invocation fails.
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = "/run/jarvis/token"
PORT = 8471


def build_command(message: str) -> str:
    """The one shell line the box runs. The message is only ever base64 in it."""
    payload = json.dumps({"messages": [{"role": "user", "content": message}]})
    blob = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    # base64's alphabet is A-Z a-z 0-9 + / =, none of which a shell acts on
    # inside single quotes -- or outside them. Checked, not assumed.
    assert all(c.isalnum() or c in "+/=" for c in blob)
    return (
        "set -e; umask 077; f=$(mktemp); trap 'rm -f \"$f\"' EXIT; "
        f"echo '{blob}' | base64 -d > \"$f\"; "
        f"T=$(cat {TOKEN}); "
        "curl -s -m 200 -H \"Authorization: Bearer $T\" "
        "-H 'Content-Type: application/json' "
        f"-X POST http://127.0.0.1:{PORT}/chat --data-binary @\"$f\" "
        "| python3.11 -c 'import sys,json; d=json.load(sys.stdin); "
        "print(d.get(\"answer\") or json.dumps(d))'"
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--file", help="read the message from this file (default: stdin)")
    p.add_argument("--timeout", type=int, default=260)
    p.add_argument("--dry-run", action="store_true",
                   help="print the command that would be sent and stop")
    args = p.parse_args()

    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            message = fh.read()
    else:
        message = sys.stdin.read()
    message = message.strip()
    if not message:
        print("nothing to say: the message is empty", file=sys.stderr)
        return 2

    command = build_command(message)
    if args.dry_run:
        print(command)
        return 0

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump([command], fh)
        batch = fh.name
    try:
        return subprocess.call([sys.executable, os.path.join(HERE, "ssm_run.py"),
                                "--commands-file", batch,
                                "--timeout", str(args.timeout)])
    finally:
        os.unlink(batch)


if __name__ == "__main__":
    sys.exit(main())
