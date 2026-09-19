#!/usr/bin/env python3
"""
Jarvis Agent - Main Entry Point

Initializes hardware access, runs security checks, and starts the
autonomous agentic loop.
"""

import os
import sys
import json
import signal
import logging
import argparse

from jarvis.agent.core import AgentCore
from jarvis.hardware.display import DisplayManager
from jarvis.hardware.input_devices import InputManager
from jarvis.hardware.mem import MemoryManager
from jarvis.hardware.storage import StorageManager
from jarvis.security.scanner import SecurityScanner
from jarvis.ui.console import ConsoleUI

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Jarvis Agentic Agent - Autonomous System Agent"
    )
    parser.add_argument(
        "--config", default="/etc/jarvis/config.yaml",
        help="Path to configuration file"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug logging"
    )
    parser.add_argument(
        "--scan-only", action="store_true",
        help="Run security scan and exit"
    )
    parser.add_argument(
        "--no-hardware", action="store_true",
        help="Skip hardware initialization (for testing)"
    )
    parser.add_argument(
        "--extra-config", action="append", default=[], metavar="PATH",
        help="Additional YAML overlay(s) merged after --config "
             "(e.g. /etc/jarvis/cloud.yaml written at boot)"
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="Run the agent loop without the console UI (servers, AMIs)"
    )
    parser.add_argument(
        "--cycle-interval", type=float, default=None, metavar="SECONDS",
        help="Seconds between idle cycles in headless mode"
    )
    parser.add_argument(
        "--max-cycles", type=int, default=None,
        help="Stop after N cycles in headless mode (0 = run forever)"
    )
    parser.add_argument(
        "--status-port", type=int, default=None,
        help="Loopback port for the headless status endpoint (0 = disabled)"
    )
    parser.add_argument(
        "--status-file", default=None,
        help="Where headless mode writes its JSON status snapshot"
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Print the status of a running headless agent and exit"
    )
    parser.add_argument(
        "--llm", dest="llm", action="store_true", default=None,
        help="Enable the LLM planner (overrides llm.enabled)"
    )
    parser.add_argument(
        "--no-llm", dest="llm", action="store_false",
        help="Disable the LLM planner for this run"
    )
    parser.add_argument(
        "--model", default=None,
        help="Claude model id for the LLM planner (default from config)"
    )
    parser.add_argument(
        "--ask", metavar="QUESTION", default=None,
        help="Ask the LLM brain a question about this machine and exit"
    )
    return parser.parse_args()


def setup_logging(debug=False):
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(level=level, format=LOG_FORMAT)
    return logging.getLogger("jarvis")


def load_config(path, extra_paths=()):
    """Load configuration from YAML file(s) or return defaults.

    ``extra_paths`` are overlays merged in order after ``path``; a missing
    overlay is skipped silently so the same command line works on the ISO
    and on a cloud instance.
    """
    config = {
        "agent": {
            "name": "Jarvis",
            "max_tasks": 100,
            "memory_limit_mb": 512,
            "auto_plan": True,
            "profile": "bare-metal",
            # The permission spine, above the command deny-list's floor:
            # observer (look and report), proposer (a change becomes a card
            # for the operator), actor (may change the machine). Anything
            # unrecognised falls back to proposer, never to actor.
            "rung": "proposer",
        },
        "hardware": {
            "display": {"enabled": True, "framebuffer": "/dev/fb0"},
            "input": {"enabled": True, "grab_exclusive": False},
            "memory": {"enabled": True, "max_map_mb": 256},
            "storage": {"enabled": True, "readonly": False},
        },
        "security": {
            "scan_on_boot": True,
            "enforce_hardening": False,
            "report_path": "/var/log/jarvis-security.log",
        },
        "cloud": {
            "enabled": False,
            "provider": "none",
            "headless": False,
            "cycle_interval": 10,
            "max_cycles": 0,
            "status_port": 8471,
            "status_host": "127.0.0.1",
            "status_file": "/run/jarvis/status.json",
            "status_token": None,
            "status_token_file": "/run/jarvis/token",
            # The runtime copy above dies with every restart; this one does not,
            # so a redeploy stops replacing the operator's credential.
            "status_token_persist_file": "/etc/jarvis/token",
            "session_key_file": "/etc/jarvis/session.key",
            "ui": {
                "enabled": False,          # web UI (chat, agent panel, uploads)
                "host": "0.0.0.0",
                "port": 8443,
                "tls": True,               # self-signed cert generated at first start
                "tls_dir": "/etc/jarvis/tls",
                "upload_dir": "/var/lib/jarvis/uploads",
                "max_upload_mb": 50,
                "session_days": 30,       # how long a browser login lasts
            },
        },
        "goals": [],
        "ledger": {                        # the Glass Ledger (signed, hash-chained journal)
            "enabled": False,
            "path": "/var/lib/jarvis/ledger.jsonl",
            "key_file": "/etc/jarvis/ledger/ed25519.key",
            "pubkey_file": "/etc/jarvis/ledger/ed25519.pub",
            "fail_closed": True,           # no record, no action, no answer
            "anchor": {                    # off-box witness copy (S3 Object Lock bucket)
                "bucket": None,            # set by the jarvis:ledger-bucket tag or here
                "prefix": "ledger",
                "every_s": 300,
                "copy": True,
            },
        },
        "memory": {                        # the agent's own durable operational memory
            "enabled": True,               # what it learned about THIS machine, across restarts
            "path": "/var/lib/jarvis/memory.db",
            "max_rows": 2000,              # oldest unpinned entries drop past this
        },
        "llm": {
            "enabled": False,
            "provider": "anthropic",       # anthropic | bedrock
            "model": "claude-opus-5",      # bedrock: a model id, inference profile or imported-model ARN
            "region": None,                # bedrock: defaults to the instance region / AWS_REGION
            "bedrock": {"temperature": 0.2, "json_retries": 1, "not_ready_backoff": 45,
                        "thinking": "auto"},  # imported reasoning models: auto | on | off
            "effort": "medium",
            "thinking": "adaptive",
            "max_tokens": 4096,
            "fallbacks": True,
            "api_key": None,
            "api_key_file": "/etc/jarvis/anthropic.key",
            "api_key_secret": None,
            "api_key_ssm_parameter": None,
            "base_url": None,
            "timeout": 120,
            "max_retries": 2,
            "max_calls_per_hour": 60,
            "plan_every_n_cycles": 1,
            "history_window": 10,
            "shell": {
                "enabled": False,
                "timeout": 60,
                "max_output": 4000,
                "cwd": "/",
                "deny_patterns": None,          # extra patterns, added to the defaults (the list never shrinks)
            },
        },
    }

    for cfg_path in (path, *extra_paths):
        _merge_config_file(config, cfg_path)

    # Override from environment
    if os.environ.get("JARVIS_FULLACCESS") == "1":
        for hw in config["hardware"].values():
            if isinstance(hw, dict):
                hw["enabled"] = True
    if os.environ.get("JARVIS_SAFEMODE") == "1":
        for hw in config["hardware"].values():
            if isinstance(hw, dict):
                hw["enabled"] = False
    if os.environ.get("JARVIS_DEBUG") == "1":
        config["agent"]["debug"] = True
    if os.environ.get("JARVIS_HEADLESS") == "1":
        config["cloud"]["headless"] = True
    if os.environ.get("JARVIS_LLM") in ("0", "1"):
        config["llm"]["enabled"] = os.environ["JARVIS_LLM"] == "1"
    if os.environ.get("JARVIS_MODEL"):
        config["llm"]["model"] = os.environ["JARVIS_MODEL"]

    return config


def _merge_config_file(config, path):
    """Merge one YAML/JSON file into config; missing or bad files are skipped."""
    if not path or not os.path.exists(path):
        return False
    try:
        with open(path, "r") as f:
            text = f.read()
    except OSError:
        return False
    user_config = None
    try:
        import yaml
        user_config = yaml.safe_load(text)
    except ImportError:
        try:
            user_config = json.loads(text)
        except ValueError:
            user_config = None
    except Exception:
        user_config = None
    if isinstance(user_config, dict):
        _deep_merge(config, user_config)
        return True
    return False


def _deep_merge(base, override):
    """Recursively merge override dict into base dict."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


class JarvisSystem:
    """Top-level system that manages all Jarvis components."""

    def __init__(self, config, logger):
        self.config = config
        self.log = logger
        self.running = False

        # Components (initialized lazily)
        self.display = None
        self.input_mgr = None
        self.memory = None
        self.storage = None
        self.scanner = None
        self.agent = None
        self.ui = None
        self.runner = None

    def initialize_hardware(self):
        """Initialize all hardware access layers."""
        hw_cfg = self.config["hardware"]

        self.log.info("Initializing hardware access layers...")

        if hw_cfg["display"]["enabled"]:
            self.display = DisplayManager(hw_cfg["display"])
            self.display.initialize()
            self.log.info("Display manager initialized")

        if hw_cfg["input"]["enabled"]:
            self.input_mgr = InputManager(hw_cfg["input"])
            self.input_mgr.initialize()
            self.log.info("Input manager initialized (keyboard + mouse)")

        if hw_cfg["memory"]["enabled"]:
            self.memory = MemoryManager(hw_cfg["memory"])
            self.memory.initialize()
            self.log.info("Memory manager initialized")

        if hw_cfg["storage"]["enabled"]:
            self.storage = StorageManager(hw_cfg["storage"])
            self.storage.initialize()
            self.log.info("Storage manager initialized")

    def run_security_scan(self):
        """Run the security vulnerability scanner."""
        self.log.info("Running security vulnerability scan...")
        self.scanner = SecurityScanner()
        report = self.scanner.full_scan()
        self.log.info(
            "Security scan complete: %d issues found "
            "(%d critical, %d warning, %d info)",
            report["total"],
            report["critical"],
            report["warning"],
            report["info"],
        )
        return report

    def build_brain(self):
        """Construct the LLM planner when enabled; None when it cannot run."""
        llm_cfg = self.config.get("llm") or {}
        if not llm_cfg.get("enabled"):
            self.log.info("LLM brain disabled (llm.enabled=false); rule planner only")
            return None
        cloud = self.config.get("cloud") or {}
        region = (cloud.get("instance") or {}).get("region") if isinstance(cloud.get("instance"), dict) else None
        try:
            from jarvis.brain.llm import build_brain
            brain = build_brain(llm_cfg, self.log, region=region,
                                cycle_interval=(cloud.get("cycle_interval")
                                                if cloud.get("headless") else None))
        except Exception as e:
            self.log.warning("LLM brain unavailable: %s", e)
            return None
        if brain.client is None:
            self.log.warning("LLM brain configured but not usable; rule planner only")
            return None
        self.log.info("LLM brain ready: provider=%s model=%s effort=%s auth=%s shell=%s",
                      brain.provider, brain.model, brain.effort, brain.key_source,
                      "on" if (llm_cfg.get("shell") or {}).get("enabled") else "off")
        return brain

    def build_ledger(self):
        """Open the Glass Ledger when enabled; None keeps the agent ungated."""
        cfg = self.config.get("ledger") or {}
        if not cfg.get("enabled"):
            return None
        cloud = self.config.get("cloud") or {}
        inst = cloud.get("instance") if isinstance(cloud.get("instance"), dict) else {}
        try:
            from jarvis.ledger import AgentLedger
            return AgentLedger(cfg, self.log, writer=self.config["agent"].get("name", "jarvis"),
                               region=inst.get("region"), instance_id=inst.get("instance_id"))
        except Exception as e:  # AgentLedger reports its own failures; this is a bug guard
            self.log.error("Glass Ledger could not be set up: %s", e)
            return None

    def build_store(self):
        """Open the agent's durable memory; a NullStore when it cannot."""
        from jarvis.agent.store import build_store
        return build_store(self.config.get("memory"), self.log)

    def build_agent(self):
        """Construct the AgentCore and seed it with configured goals."""
        hardware = {
            "display": self.display,
            "input": self.input_mgr,
            "memory": self.memory,
            "storage": self.storage,
        }

        llm_cfg = self.config.get("llm") or {}
        self.ledger = self.build_ledger()
        self.store = self.build_store()
        self.agent = AgentCore(
            config=self.config["agent"],
            hardware=hardware,
            logger=self.log,
            brain=self.build_brain(),
            shell_policy=llm_cfg.get("shell"),
            ledger=self.ledger,
            store=self.store,
        )
        if self.ledger is not None:
            brain = self.agent.brain
            self.ledger.record("action", {
                "cycle": 0, "actor": "system", "action": "agent_start",
                "name": self.agent.name, "profile": self.agent.profile,
                "brain": {"provider": getattr(brain, "provider", None),
                          "model": getattr(brain, "model", None)} if brain else None,
                "shell": bool((llm_cfg.get("shell") or {}).get("enabled")),
                "instance_id": ((self.config.get("cloud") or {}).get("instance") or {}).get("instance_id")
                if isinstance((self.config.get("cloud") or {}).get("instance"), dict) else None,
            })
        for goal in self.config.get("goals") or []:
            if isinstance(goal, str):
                self.agent.add_goal(goal)
            elif isinstance(goal, dict) and goal.get("description"):
                self.agent.add_goal(goal["description"],
                                    int(goal.get("priority", 5)))
        cloud = self.config.get("cloud", {})
        if cloud.get("instance"):
            self.agent.memory.store(category="cloud_instance",
                                    data=cloud["instance"])
        return self.agent

    def start_agent(self, headless=None):
        """Start the agentic core loop (console UI or headless)."""
        self.log.info("Starting Jarvis Agent Core...")
        self.build_agent()

        cloud = self.config.get("cloud", {})
        if headless is None:
            headless = bool(cloud.get("headless"))

        self.running = True
        if headless:
            from jarvis.cloud.headless import HeadlessRunner
            port = cloud.get("status_port")
            self.runner = HeadlessRunner(
                self.agent, self.log,
                interval=cloud.get("cycle_interval", 10),
                max_cycles=cloud.get("max_cycles", 0),
                status_port=(int(port) if port else None),
                status_host=cloud.get("status_host", "127.0.0.1"),
                status_file=cloud.get("status_file"),
                token=cloud.get("status_token") or None,
                token_file=cloud.get("status_token_file"),
                token_persist_file=cloud.get("status_token_persist_file"),
                session_key_file=cloud.get("session_key_file"),
                session_days=int((cloud.get("ui") or {}).get("session_days") or 30),
                max_idle_wait=cloud.get("max_idle_wait"),
                ui=cloud.get("ui"),
            )
            self.runner.run()
        else:
            self.ui = ConsoleUI(self.agent)
            self.ui.run()

    def shutdown(self):
        """Clean shutdown of all components."""
        self.log.info("Shutting down Jarvis...")
        self.running = False

        if self.runner:
            self.runner.stop()
        if self.agent:
            self.agent.shutdown()
        if self.display:
            self.display.cleanup()
        if self.input_mgr:
            self.input_mgr.cleanup()
        if self.memory:
            self.memory.cleanup()
        if self.storage:
            self.storage.cleanup()

        self.log.info("Shutdown complete.")


def query_status(config):
    """Fetch the status of a running headless agent (endpoint, then file)."""
    cloud = config.get("cloud", {})
    port = cloud.get("status_port")
    if port:
        import urllib.request
        url = f"http://{cloud.get('status_host', '127.0.0.1')}:{port}/status"
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                return json.loads(resp.read().decode()), url
        except Exception:
            pass
    status_file = cloud.get("status_file")
    if status_file and os.path.exists(status_file):
        try:
            with open(status_file) as f:
                return json.load(f), status_file
        except (OSError, ValueError):
            pass
    return None, None


def print_status(status, source):
    print(f"Jarvis agent status (from {source})")
    print(f"  Name:            {status.get('name')}")
    print(f"  Profile:         {status.get('profile')}")
    print(f"  Running:         {status.get('running')}")
    print(f"  Cycles:          {status.get('cycle_count')}")
    print(f"  Pending tasks:   {status.get('pending_tasks')}")
    print(f"  Completed tasks: {status.get('completed_tasks')}")
    print(f"  Memory entries:  {status.get('memory_entries')}")
    runner = status.get("runner") or {}
    if runner:
        print(f"  Uptime:          {runner.get('uptime_seconds')}s")
        print(f"  Last action:     {runner.get('last_action')}")
    goals = status.get("goals") or {}
    if goals:
        print(f"  Goals:           {goals.get('open')} open / {goals.get('total')} total")
    brain = status.get("brain")
    if brain:
        state = "available" if brain.get("available") else (brain.get("disabled_reason")
                                                            or "backing off")
        provider = f"{brain['provider']}:" if brain.get("provider") else ""
        print(f"  Brain:           {provider}{brain.get('model')} ({state}), "
              f"{brain.get('calls_last_hour')}/{brain.get('max_calls_per_hour')} calls this hour")
        if not brain.get("available") and brain.get("unavailable_reason"):
            print(f"  Brain reason:    {brain['unavailable_reason']}")
        if brain.get("last_reasoning"):
            print(f"  Last reasoning:  {brain['last_reasoning'][:200]}")
    else:
        print("  Brain:           off (rule planner)")
    current = status.get("current_task")
    if current:
        print(f"  Current task:    {current.get('description')}")


def apply_cli_overrides(config, args):
    """Command-line flags win over every config file."""
    cloud = config["cloud"]
    llm = config["llm"]
    if getattr(args, "llm", None) is not None:
        llm["enabled"] = bool(args.llm)
    if getattr(args, "model", None):
        llm["model"] = args.model
    if getattr(args, "ask", None):
        llm["enabled"] = True
    if args.headless:
        cloud["headless"] = True
    if args.cycle_interval is not None:
        cloud["cycle_interval"] = args.cycle_interval
    if args.max_cycles is not None:
        cloud["max_cycles"] = args.max_cycles
    if args.status_port is not None:
        cloud["status_port"] = args.status_port or None
    if args.status_file is not None:
        cloud["status_file"] = args.status_file or None
    return config


def main():
    args = parse_args()
    debug = args.debug or os.environ.get("JARVIS_DEBUG") == "1"
    logger = setup_logging(debug)

    config = load_config(args.config, args.extra_config)
    apply_cli_overrides(config, args)

    if args.status:
        status, source = query_status(config)
        if status is None:
            print("Jarvis agent is not running (no status endpoint or file).")
            sys.exit(3)
        print_status(status, source)
        return

    if args.ask:
        # One-shot question: build the agent (hardware optional), ask, exit.
        system = JarvisSystem(config, logger)
        if not args.no_hardware:
            try:
                system.initialize_hardware()
            except Exception as e:
                logger.warning("Hardware initialization partial failure: %s", e)
        system.build_agent()
        try:
            print(system.agent.ask(args.ask))
        finally:
            system.shutdown()
        return

    logger.info("Jarvis Agent v%s starting...", "1.0.0")
    system = JarvisSystem(config, logger)

    # Handle signals for clean shutdown
    def signal_handler(signum, frame):
        logger.info("Received signal %d, shutting down...", signum)
        system.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Security scan
    scan_on_boot = config["security"].get("scan_on_boot", True)
    scan_only = args.scan_only or os.environ.get("JARVIS_SCANONLY") == "1"

    if scan_on_boot or scan_only:
        report = system.run_security_scan()
        if scan_only:
            print("\n=== Security Scan Report ===")
            for finding in report.get("findings", []):
                severity = finding.get("severity", "INFO").upper()
                print(f"  [{severity}] {finding.get('title', 'Unknown')}")
                print(f"         {finding.get('description', '')}")
            print(f"\nTotal: {report['total']} issues")
            return

    # Initialize hardware (unless testing)
    if not args.no_hardware:
        try:
            system.initialize_hardware()
        except Exception as e:
            logger.warning("Hardware initialization partial failure: %s", e)
            logger.info("Continuing with available hardware...")

    # Start the agent
    try:
        system.start_agent(headless=config["cloud"].get("headless"))
    except KeyboardInterrupt:
        pass
    finally:
        system.shutdown()


if __name__ == "__main__":
    main()
