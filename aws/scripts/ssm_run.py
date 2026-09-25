#!/usr/bin/env python3
"""Run a command on the Jarvis instance over SSM and print what it said.

There is no inbound SSH to the box, so SSM Run Command is the only way in.
Doing it inline with boto3 each time meant a fresh throwaway script per
deploy, which is neither reviewable nor repeatable; this is the one path.

Stale AWS_* variables in the environment shadow AWS_PROFILE and silently
authenticate as the wrong principal, so they are cleared here rather than
left to a caller who remembers the `env -u` incantation.

Usage:
    ssm_run.py --command "systemctl is-active jarvis"
    ssm_run.py --commands-file batch.json          # a JSON list of strings
    ssm_run.py --command "..." --instance i-0123 --region eu-west-2

Exits non-zero when the invocation fails, so a caller can gate on it.
"""

import argparse
import json
import os
import sys
import time

DEFAULT_INSTANCE = "i-016f9f37fe6ca2ba8"
DEFAULT_REGION = "us-west-2"
DEFAULT_PROFILE = "openclaw"
SHADOWING = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
TERMINAL = ("Success", "Failed", "Cancelled", "TimedOut", "Undeliverable", "Terminated")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--command", action="append", default=[],
                   help="a shell command to run; repeat for several, in order")
    p.add_argument("--commands-file", help="JSON file holding a list of command strings")
    p.add_argument("--instance", default=DEFAULT_INSTANCE)
    p.add_argument("--region", default=DEFAULT_REGION)
    p.add_argument("--profile", default=DEFAULT_PROFILE)
    p.add_argument("--timeout", type=int, default=600, help="seconds to wait (default 600)")
    args = p.parse_args()

    commands = list(args.command)
    if args.commands_file:
        with open(args.commands_file) as fh:
            loaded = json.load(fh)
        if not isinstance(loaded, list) or not all(isinstance(c, str) for c in loaded):
            print("--commands-file must hold a JSON list of strings", file=sys.stderr)
            return 2
        commands.extend(loaded)
    if not commands:
        print("nothing to run: pass --command or --commands-file", file=sys.stderr)
        return 2

    for name in SHADOWING:
        os.environ.pop(name, None)

    import boto3                                    # imported late, after the scrub
    ssm = boto3.Session(profile_name=args.profile, region_name=args.region).client("ssm")

    sent = ssm.send_command(InstanceIds=[args.instance],
                            DocumentName="AWS-RunShellScript",
                            Parameters={"commands": commands},
                            TimeoutSeconds=args.timeout)
    command_id = sent["Command"]["CommandId"]
    print(f"[ssm] {command_id} -> {args.instance} ({len(commands)} command(s))",
          file=sys.stderr)

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        time.sleep(3)
        try:
            inv = ssm.get_command_invocation(CommandId=command_id, InstanceId=args.instance)
        except ssm.exceptions.InvocationDoesNotExist:
            continue                                # not registered yet
        if inv["Status"] in TERMINAL:
            print(f"[ssm] status: {inv['Status']}", file=sys.stderr)
            if inv["StandardOutputContent"]:
                print(inv["StandardOutputContent"], end="")
            if inv["StandardErrorContent"].strip():
                print("--- stderr ---", file=sys.stderr)
                print(inv["StandardErrorContent"], end="", file=sys.stderr)
            return 0 if inv["Status"] == "Success" else 1

    print(f"[ssm] timed out after {args.timeout}s; command {command_id} may still be running",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
