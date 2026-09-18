"""
OpenClaw Cloud Bootstrap

Runs once at boot (``openclaw-bootstrap.service``) before the agent starts.
It asks the instance metadata service who we are, reads the operator's
user data, and writes a config overlay the agent merges on top of
``/etc/openclaw/config.yaml``.

User data formats understood (first match wins):

1. cloud-config or plain YAML with a top-level ``openclaw:`` key::

       #cloud-config
       openclaw:
         agent:
           name: paul-agent
         goals:
           - description: Keep root filesystem under 80% used
             priority: 3

2. A YAML/JSON document that *is* the openclaw config (has ``agent``,
   ``goals``, ``cloud`` or ``security`` at the top level).

Anything else (shell scripts, MIME multipart, binary) is ignored, so the
agent still boots with sane defaults.
"""

import argparse
import json
import os
import sys
from typing import Optional

from openclaw.cloud.imds import IMDSClient
from openclaw.brain.credentials import (DEFAULT_KEY_FILE, fetch_ssm_parameter,
                                        install_key_file)

DEFAULT_OUTPUT = "/etc/openclaw/cloud.yaml"
CONFIG_KEYS = ("agent", "goals", "cloud", "security", "hardware", "llm")


def _load_yaml_or_json(text: str) -> Optional[dict]:
    """Parse text as YAML (preferred) or JSON. Returns a dict or None."""
    text = text.strip()
    if not text:
        return None
    try:
        import yaml
        doc = yaml.safe_load(text)
    except ImportError:
        try:
            doc = json.loads(text)
        except ValueError:
            return None
    except Exception:
        return None
    return doc if isinstance(doc, dict) else None


def parse_user_data(text: Optional[str]) -> dict:
    """Extract the openclaw overrides from raw user data.

    Returns ``{}`` when the user data carries nothing for us.
    """
    if not text:
        return {}
    doc = _load_yaml_or_json(text)
    if not doc:
        return {}
    if isinstance(doc.get("openclaw"), dict):
        return dict(doc["openclaw"])
    if any(key in doc for key in CONFIG_KEYS):
        return {k: v for k, v in doc.items() if k in CONFIG_KEYS}
    return {}


def normalise_goals(goals) -> list:
    """Accept ``["text", {"description": .., "priority": ..}]`` forms."""
    result = []
    if not isinstance(goals, list):
        return result
    for goal in goals:
        if isinstance(goal, str) and goal.strip():
            result.append({"description": goal.strip(), "priority": 5})
        elif isinstance(goal, dict) and goal.get("description"):
            try:
                priority = int(goal.get("priority", 5))
            except (TypeError, ValueError):
                priority = 5
            result.append({
                "description": str(goal["description"]).strip(),
                "priority": max(0, min(priority, 10)),
            })
    return result


def build_cloud_config(imds: IMDSClient) -> dict:
    """Assemble the overlay: instance facts + operator overrides + goals."""
    overrides = parse_user_data(imds.user_data())
    goals = normalise_goals(overrides.pop("goals", []))

    instance = imds.summary()
    tags = imds.tags() if instance else {}

    # Tags are a second, lighter way to steer the agent without user data.
    if tags.get("openclaw:goal"):
        goals.append({"description": tags["openclaw:goal"], "priority": 5})

    cloud = {
        "enabled": bool(instance),
        "provider": "aws" if instance else "none",
        "instance": instance,
        "tags": tags,
    }
    # operator's cloud overrides (cycle_interval, status_port, ...) win
    user_cloud = overrides.pop("cloud", {})
    if isinstance(user_cloud, dict):
        cloud.update(user_cloud)

    config = {"cloud": cloud, "goals": goals}
    for key, value in overrides.items():
        if key in CONFIG_KEYS and isinstance(value, dict):
            config[key] = value

    if tags.get("openclaw:name"):
        config.setdefault("agent", {})["name"] = tags["openclaw:name"]
    if tags.get("openclaw:llm-key-parameter"):
        config.setdefault("llm", {})["api_key_ssm_parameter"] = tags["openclaw:llm-key-parameter"]
    if instance:
        config["agent"] = config.get("agent", {})
        config["agent"].setdefault("profile", "cloud")
    return config


def provision_llm_key(config: dict, key_file: str = DEFAULT_KEY_FILE,
                      fetch=fetch_ssm_parameter) -> str:
    """Move any LLM API key out of the overlay and into a 0600 key file.

    Sources, in order: an inline ``llm.api_key`` from user data (removed
    from the overlay so it never lands in the world-readable cloud.yaml),
    then ``llm.api_key_ssm_parameter`` fetched with the instance role.
    Returns a short description of what happened for the boot log.
    """
    llm = config.get("llm")
    if not isinstance(llm, dict):
        return "no llm section"
    key_file = llm.get("api_key_file") or key_file
    inline = llm.pop("api_key", None)
    if isinstance(inline, str) and inline.strip():
        install_key_file(inline, key_file)
        llm["api_key_file"] = key_file
        llm.setdefault("enabled", True)
        return f"inline key moved to {key_file}"
    param = llm.get("api_key_ssm_parameter")
    if param:
        region = (config.get("cloud", {}).get("instance") or {}).get("region")
        value = fetch(param, region)
        if value:
            install_key_file(value, key_file)
            llm["api_key_file"] = key_file
            llm.setdefault("enabled", True)
            return f"key fetched from SSM {param} -> {key_file}"
        return f"SSM parameter {param} not readable (role permissions? awscli?)"
    return "no key configured"


def dump_config(config: dict) -> str:
    try:
        import yaml
        return yaml.safe_dump(config, default_flow_style=False, sort_keys=False)
    except ImportError:
        # JSON is valid YAML, so the agent can still load it.
        return json.dumps(config, indent=2)


def write_cloud_config(config: dict, path: str) -> str:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("# Generated by openclaw.cloud.bootstrap - do not edit; "
                "set user data or tags instead.\n")
        f.write(dump_config(config))
    os.replace(tmp, path)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Bootstrap OpenClaw configuration from EC2 metadata")
    parser.add_argument("--output", default=DEFAULT_OUTPUT,
                        help="Where to write the config overlay")
    parser.add_argument("--imds-url", default=None,
                        help="Override the IMDS endpoint (testing)")
    parser.add_argument("--print", action="store_true", dest="print_only",
                        help="Print the overlay instead of writing it")
    parser.add_argument("--key-file", default=DEFAULT_KEY_FILE,
                        help="Where to store an LLM API key found in user data / SSM")
    parser.add_argument("--skip-llm-key", action="store_true",
                        help="Do not resolve or write the LLM API key")
    args = parser.parse_args(argv)

    imds = IMDSClient(base_url=args.imds_url)
    config = build_cloud_config(imds)

    key_note = "skipped"
    if not args.skip_llm_key and not args.print_only:
        key_note = provision_llm_key(config, args.key_file)
    elif args.print_only and isinstance(config.get("llm"), dict):
        config["llm"].pop("api_key", None)  # never print secrets

    if args.print_only:
        sys.stdout.write(dump_config(config))
        return 0

    path = write_cloud_config(config, args.output)
    instance = config["cloud"].get("instance", {})
    if instance:
        print(f"[bootstrap] {instance.get('instance_id', '?')} "
              f"({instance.get('instance_type', '?')}) in "
              f"{instance.get('region', '?')} -> {path}")
    else:
        print(f"[bootstrap] IMDS not reachable; wrote defaults -> {path}")
    print(f"[bootstrap] goals: {len(config['goals'])}")
    print(f"[bootstrap] llm key: {key_note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
