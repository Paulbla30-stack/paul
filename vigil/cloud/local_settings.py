"""Paul's own settings, in a file the bootstrap never writes.

27 September 2026: the move to Vigil re-ran the bootstrap, which rebuilds
cloud.yaml from the instance's launch user data. That data still named the
agent "paul-life-agent" and pointed at a Bedrock model deleted weeks before.
The agent's working name, its operator's name and the model it had been
running on all day lived only as hand edits in cloud.yaml, so they were
overwritten, and the agent came up nameless and without a planner. Any
reboot would have done the same.

So the settings an operator chooses live here, in /etc/vigil/local.yaml,
loaded after cloud.yaml (``--extra-config`` order) and never written by the
bootstrap. Launch data can go stale; this file is what the operator last
decided.

    python3 -m vigil.cloud.local_settings derive OLD_CLOUD.yaml LOCAL.yaml
    python3 -m vigil.cloud.local_settings effective CONFIG CLOUD LOCAL
"""

import argparse
import json
import os
import sys
from typing import Optional

HEADER = ("# The operator's own settings. Loaded after cloud.yaml and never written\n"
          "# by the bootstrap, so a reboot cannot undo them. See\n"
          "# vigil/cloud/local_settings.py for why this file exists.\n")


def derive(old_cloud: dict, name: str = "Vigil") -> dict:
    """The operator's settings, carried from a hand-edited cloud.yaml.

    Carried: the operator's name and the model the agent was running on.
    Set: the agent's name, and the ledger's writer, which keeps the name the
    chain began with (the old agent name) so the off-box anchor does not move.
    """
    old_agent = old_cloud.get("agent") if isinstance(old_cloud.get("agent"), dict) else {}
    old_llm = old_cloud.get("llm") if isinstance(old_cloud.get("llm"), dict) else {}
    old_ledger = old_cloud.get("ledger") if isinstance(old_cloud.get("ledger"), dict) else {}
    out = {"agent": {"name": name}}
    if old_agent.get("operator_name"):
        out["agent"]["operator_name"] = str(old_agent["operator_name"])
    llm = {k: old_llm[k] for k in ("provider", "model") if old_llm.get(k)}
    if llm:
        out["llm"] = llm
    writer = old_ledger.get("writer") or old_agent.get("name")
    if writer:
        out["ledger"] = {"writer": str(writer)}
    return out


def effective(config: str, cloud: str, local: Optional[str]) -> dict:
    """What the agent will actually run with, from the same loader it uses."""
    from vigil.main import load_config
    merged = load_config(config, [p for p in (cloud, local) if p])
    return {"name": merged["agent"].get("name"),
            "operator_name": merged["agent"].get("operator_name"),
            "model": (merged.get("llm") or {}).get("model"),
            "provider": (merged.get("llm") or {}).get("provider"),
            "ledger_writer": (merged.get("ledger") or {}).get("writer") or merged["agent"].get("name"),
            "anchor_bucket": ((merged.get("ledger") or {}).get("anchor") or {}).get("bucket")}


def _load(path: str) -> dict:
    import yaml
    with open(path, encoding="utf-8") as fh:
        got = yaml.safe_load(fh) or {}
    return got if isinstance(got, dict) else {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("derive")
    d.add_argument("old_cloud")
    d.add_argument("out")
    d.add_argument("--name", default="Vigil")
    e = sub.add_parser("effective")
    e.add_argument("config")
    e.add_argument("cloud")
    e.add_argument("local", nargs="?")
    args = ap.parse_args(argv)
    if args.cmd == "derive":
        import yaml
        settings = derive(_load(args.old_cloud), args.name)
        tmp = args.out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(HEADER + yaml.safe_dump(settings, sort_keys=False))
        os.chmod(tmp, 0o644)
        os.replace(tmp, args.out)
        print(json.dumps(settings))
        return 0
    print(json.dumps(effective(args.config, args.cloud, args.local)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
