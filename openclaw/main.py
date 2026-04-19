#!/usr/bin/env python3
"""
OpenClaw Agent - Main Entry Point

Initializes hardware access, runs security checks, and starts the
autonomous agentic loop.
"""

import os
import sys
import signal
import logging
import argparse

from openclaw.agent.core import AgentCore
from openclaw.hardware.display import DisplayManager
from openclaw.hardware.input_devices import InputManager
from openclaw.hardware.mem import MemoryManager
from openclaw.hardware.storage import StorageManager
from openclaw.security.scanner import SecurityScanner
from openclaw.ui.console import ConsoleUI

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def parse_args():
    parser = argparse.ArgumentParser(
        description="OpenClaw Agentic Agent - Autonomous System Agent"
    )
    parser.add_argument(
        "--config", default="/etc/openclaw/config.yaml",
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
    return parser.parse_args()


def setup_logging(debug=False):
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(level=level, format=LOG_FORMAT)
    return logging.getLogger("openclaw")


def load_config(path):
    """Load configuration from YAML file or return defaults."""
    config = {
        "agent": {
            "name": "OpenClaw",
            "max_tasks": 100,
            "memory_limit_mb": 512,
            "auto_plan": True,
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
            "report_path": "/var/log/openclaw-security.log",
        },
    }

    if os.path.exists(path):
        try:
            import yaml
            with open(path, "r") as f:
                user_config = yaml.safe_load(f)
            if user_config:
                _deep_merge(config, user_config)
        except ImportError:
            # YAML not available, try simple parsing
            pass
        except Exception:
            pass

    # Override from environment
    if os.environ.get("OPENCLAW_FULLACCESS") == "1":
        for hw in config["hardware"].values():
            if isinstance(hw, dict):
                hw["enabled"] = True
    if os.environ.get("OPENCLAW_SAFEMODE") == "1":
        for hw in config["hardware"].values():
            if isinstance(hw, dict):
                hw["enabled"] = False
    if os.environ.get("OPENCLAW_DEBUG") == "1":
        config["agent"]["debug"] = True

    return config


def _deep_merge(base, override):
    """Recursively merge override dict into base dict."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


class OpenClawSystem:
    """Top-level system that manages all OpenClaw components."""

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

    def start_agent(self):
        """Start the agentic core loop."""
        self.log.info("Starting OpenClaw Agent Core...")

        hardware = {
            "display": self.display,
            "input": self.input_mgr,
            "memory": self.memory,
            "storage": self.storage,
        }

        self.agent = AgentCore(
            config=self.config["agent"],
            hardware=hardware,
            logger=self.log,
        )
        self.ui = ConsoleUI(self.agent)

        self.running = True
        self.ui.run()

    def shutdown(self):
        """Clean shutdown of all components."""
        self.log.info("Shutting down OpenClaw...")
        self.running = False

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


def main():
    args = parse_args()
    debug = args.debug or os.environ.get("OPENCLAW_DEBUG") == "1"
    logger = setup_logging(debug)

    logger.info("OpenClaw Agent v%s starting...", "1.0.0")

    config = load_config(args.config)
    system = OpenClawSystem(config, logger)

    # Handle signals for clean shutdown
    def signal_handler(signum, frame):
        logger.info("Received signal %d, shutting down...", signum)
        system.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Security scan
    scan_on_boot = config["security"].get("scan_on_boot", True)
    scan_only = args.scan_only or os.environ.get("OPENCLAW_SCANONLY") == "1"

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
        system.start_agent()
    except KeyboardInterrupt:
        pass
    finally:
        system.shutdown()


if __name__ == "__main__":
    main()
