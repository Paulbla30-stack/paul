"""
Jarvis Cloud Bootstrap

Runs once at boot (``jarvis-bootstrap.service``) before the agent starts.
It asks the instance metadata service who we are, reads the operator's
user data, and writes a config overlay the agent merges on top of
``/etc/jarvis/config.yaml``.

User data formats understood (first match wins):

1. cloud-config or plain YAML with a top-level ``jarvis:`` key::

       #cloud-config
       jarvis:
         agent:
           name: paul-agent
         goals:
           - description: Keep root filesystem under 80% used
             priority: 3

2. A YAML/JSON document that *is* the jarvis config (has ``agent``,
   ``goals``, ``cloud`` or ``security`` at the top level).

Anything else (shell scripts, MIME multipart, binary) is ignored, so the
agent still boots with sane defaults.
"""

import argparse
import json
import os
import sys
from typing import Optional

from jarvis.cloud.imds import IMDSClient
from jarvis.brain.credentials import (DEFAULT_KEY_FILE, fetch_ssm_parameter,
                                        fetch_secretsmanager_secret, install_key_file)
from jarvis.cloud import tunnel as _tunnel
from jarvis.agent import notify as _notify

DEFAULT_OUTPUT = "/etc/jarvis/cloud.yaml"
DEFAULT_BASE_CONFIG = "/etc/jarvis/config.yaml"
CONFIG_KEYS = ("agent", "goals", "cloud", "security", "hardware", "llm", "tunnel")
LLM_DEFAULT_KEYS = ("api_key_secret", "api_key_ssm_parameter", "api_key_file")


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
    """Extract the jarvis overrides from raw user data.

    Returns ``{}`` when the user data carries nothing for us.
    """
    if not text:
        return {}
    doc = _load_yaml_or_json(text)
    if not doc:
        return {}
    if isinstance(doc.get("jarvis"), dict):
        return dict(doc["jarvis"])
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
    if tags.get("jarvis:goal"):
        goals.append({"description": tags["jarvis:goal"], "priority": 5})

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

    if tags.get("jarvis:name"):
        config.setdefault("agent", {})["name"] = tags["jarvis:name"]
    if tags.get("jarvis:llm-key-secret"):
        config.setdefault("llm", {})["api_key_secret"] = tags["jarvis:llm-key-secret"]
    if tags.get("jarvis:llm-key-parameter"):
        config.setdefault("llm", {})["api_key_ssm_parameter"] = tags["jarvis:llm-key-parameter"]
    if tags.get("jarvis:ledger-bucket"):
        config.setdefault("ledger", {}).setdefault("anchor", {})["bucket"] = tags["jarvis:ledger-bucket"]
    if tags.get("jarvis:notify-destination-secret"):
        config.setdefault("cloud", {}).setdefault("notify", {})["destination_secret"] = \
            tags["jarvis:notify-destination-secret"]
    if tags.get("jarvis:tunnel-token-secret"):
        config.setdefault("tunnel", {})["token_secret"] = tags["jarvis:tunnel-token-secret"]
    if tags.get("jarvis:tunnel-token-parameter"):
        config.setdefault("tunnel", {})["token_ssm_parameter"] = tags["jarvis:tunnel-token-parameter"]
    if instance:
        config["agent"] = config.get("agent", {})
        config["agent"].setdefault("profile", "cloud")
    return config


def apply_base_llm_defaults(config: dict, base_config_path: Optional[str]) -> None:
    """Take llm.api_key_ssm_parameter / api_key_file defaults from the
    installed config.yaml when the overlay does not set them, so the knob
    in config-aws.yaml is honoured as documented."""
    doc = _load_yaml_or_json(_read(base_config_path)) if base_config_path else None
    base_llm = (doc or {}).get("llm") if isinstance(doc, dict) else None
    if not isinstance(base_llm, dict):
        return
    llm = config.setdefault("llm", {})
    if not isinstance(llm, dict):
        return
    provider = str(llm.get("provider") or base_llm.get("provider") or "anthropic").lower()
    if provider not in ("anthropic", "claude"):
        return  # Bedrock and friends use the instance role; no key to source
    for key in LLM_DEFAULT_KEYS:
        if not llm.get(key) and base_llm.get(key):
            llm[key] = base_llm[key]


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def strip_secrets(config: dict) -> None:
    """Never let an inline API key or tunnel token reach the world-readable
    overlay. The tunnel token is the tunnel: whoever holds it can re-point
    the hostname at their own machine."""
    llm = config.get("llm")
    if isinstance(llm, dict):
        llm.pop("api_key", None)
    _tunnel.strip_secrets(config)
    notify = (config.get("cloud") or {}).get("notify")
    if isinstance(notify, dict):
        notify.pop("destination", None)


def provision_llm_key(config: dict, key_file: str = DEFAULT_KEY_FILE,
                      fetch=fetch_ssm_parameter,
                      fetch_secret=fetch_secretsmanager_secret) -> str:
    """Move any LLM API key out of the overlay and into a 0600 key file.

    Sources, in order: an inline ``llm.api_key`` from user data (removed
    from the overlay so it never lands in the world-readable cloud.yaml),
    then ``llm.api_key_secret`` from AWS Secrets Manager, then
    ``llm.api_key_ssm_parameter`` from SSM Parameter Store, both fetched
    with the instance role. Returns a short description for the boot log.
    """
    llm = config.get("llm")
    if not isinstance(llm, dict):
        return "no llm section"
    if str(llm.get("provider") or "anthropic").lower() not in ("anthropic", "claude"):
        llm.pop("api_key", None)
        return f"not needed for provider {llm.get('provider')} (instance role)"
    key_file = llm.get("api_key_file") or key_file
    inline = llm.pop("api_key", None)
    if isinstance(inline, str) and inline.strip():
        install_key_file(inline, key_file)
        llm["api_key_file"] = key_file
        llm.setdefault("enabled", True)
        return f"inline key moved to {key_file}"
    region = (config.get("cloud", {}).get("instance") or {}).get("region")
    notes = []
    secret = llm.get("api_key_secret")
    if secret:
        value = fetch_secret(secret, region)
        if value:
            install_key_file(value, key_file)
            llm["api_key_file"] = key_file
            llm.setdefault("enabled", True)
            return f"key fetched from Secrets Manager {secret} -> {key_file}"
        notes.append(f"Secrets Manager secret {secret} not readable (role permissions? awscli?)")
    param = llm.get("api_key_ssm_parameter")
    if param:
        value = fetch(param, region)
        if value:
            install_key_file(value, key_file)
            llm["api_key_file"] = key_file
            llm.setdefault("enabled", True)
            return f"key fetched from SSM {param} -> {key_file}"
        notes.append(f"SSM parameter {param} not readable (role permissions? awscli?)")
    return "; ".join(notes) if notes else "no key configured"


def provision_notify_destination(config: dict,
                                 dest_file: str = _notify.DEFAULT_DESTINATION_FILE,
                                 fetch=fetch_ssm_parameter,
                                 fetch_secret=fetch_secretsmanager_secret) -> str:
    """Put the operator's address on disk 0600, out of the overlay.

    Where the agent may speak to is the operator's decision, not the model's,
    so it arrives the same way the LLM key and the tunnel token do: fetched
    with the instance role from a secret, written root-only, and never left
    in the world-readable cloud.yaml. Returns a note for the boot log; never
    raises, because a box that cannot fetch it should still come up.
    """
    section = (config.get("cloud") or {}).get("notify")
    if not isinstance(section, dict):
        return "no notify section"
    if section.get("enabled") is False:
        section.pop("destination", None)
        return "notifications disabled"
    dest_file = section.get("destination_file") or dest_file
    inline = section.pop("destination", None)
    if isinstance(inline, str) and inline.strip():
        install_key_file(inline.strip(), dest_file)
        section["destination_file"] = dest_file
        return f"inline destination moved to {dest_file}"
    region = (config.get("cloud", {}).get("instance") or {}).get("region")
    notes = []
    secret = section.get("destination_secret")
    if secret:
        value = fetch_secret(secret, region)
        if value:
            install_key_file(value.strip(), dest_file)
            section["destination_file"] = dest_file
            return f"destination fetched from Secrets Manager {secret} -> {dest_file}"
        notes.append(f"Secrets Manager secret {secret} not readable")
    param = section.get("destination_ssm_parameter")
    if param:
        value = fetch(param, region)
        if value:
            install_key_file(value.strip(), dest_file)
            section["destination_file"] = dest_file
            return f"destination fetched from SSM {param} -> {dest_file}"
        notes.append(f"SSM parameter {param} not readable")
    return "; ".join(notes) if notes else "no destination configured"


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
        f.write("# Generated by jarvis.cloud.bootstrap - do not edit; "
                "set user data or tags instead.\n")
        f.write(dump_config(config))
    os.replace(tmp, path)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Bootstrap Jarvis configuration from EC2 metadata")
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
    parser.add_argument("--config", default=DEFAULT_BASE_CONFIG,
                        help="Installed config.yaml; its llm.api_key_ssm_parameter / "
                             "api_key_file are used when user data does not set them")
    args = parser.parse_args(argv)

    imds = IMDSClient(base_url=args.imds_url)
    config = build_cloud_config(imds)
    apply_base_llm_defaults(config, args.config)

    key_note = "skipped"
    tunnel_note = "skipped"
    notify_note = "skipped"
    if not args.skip_llm_key and not args.print_only:
        key_note = provision_llm_key(config, args.key_file)
    if not args.print_only:
        tunnel_note = _tunnel.provision_token(config)
        notify_note = provision_notify_destination(config)
    # Whatever happened above, the overlay never carries a credential.
    strip_secrets(config)

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
    print(f"[bootstrap] tunnel: {tunnel_note}")
    print(f"[bootstrap] notify: {notify_note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
