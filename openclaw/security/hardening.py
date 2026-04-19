"""
OpenClaw System Hardening

Applies security hardening measures to the running system.
These are optional and can be applied after the security scan.
"""

import os
import subprocess
from typing import Optional

from openclaw.security.scanner import Finding


class HardeningAction:
    """A single hardening action."""

    def __init__(self, name: str, description: str, command: str,
                 verify_path: Optional[str] = None,
                 verify_value: Optional[str] = None):
        self.name = name
        self.description = description
        self.command = command
        self.verify_path = verify_path
        self.verify_value = verify_value
        self.applied = False
        self.error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "applied": self.applied,
            "error": self.error,
        }


class SystemHardener:
    """
    Applies security hardening to the system.

    All actions are reversible and logged.
    """

    def __init__(self):
        self.actions: list[HardeningAction] = []
        self._build_actions()

    def _build_actions(self):
        """Define available hardening actions."""
        self.actions = [
            HardeningAction(
                name="Enable ASLR",
                description="Set address space layout randomization to full",
                command="echo 2 > /proc/sys/kernel/randomize_va_space",
                verify_path="/proc/sys/kernel/randomize_va_space",
                verify_value="2",
            ),
            HardeningAction(
                name="Restrict dmesg",
                description="Prevent unprivileged users from reading kernel log",
                command="echo 1 > /proc/sys/kernel/dmesg_restrict",
                verify_path="/proc/sys/kernel/dmesg_restrict",
                verify_value="1",
            ),
            HardeningAction(
                name="Restrict kernel pointers",
                description="Hide kernel addresses from unprivileged users",
                command="echo 1 > /proc/sys/kernel/kptr_restrict",
                verify_path="/proc/sys/kernel/kptr_restrict",
                verify_value="1",
            ),
            HardeningAction(
                name="Enable SYN cookies",
                description="Protect against SYN flood attacks",
                command="echo 1 > /proc/sys/net/ipv4/tcp_syncookies",
                verify_path="/proc/sys/net/ipv4/tcp_syncookies",
                verify_value="1",
            ),
            HardeningAction(
                name="Disable IP forwarding",
                description="Prevent the system from routing packets",
                command="echo 0 > /proc/sys/net/ipv4/ip_forward",
                verify_path="/proc/sys/net/ipv4/ip_forward",
                verify_value="0",
            ),
            HardeningAction(
                name="Enable reverse path filtering",
                description="Drop packets with spoofed source addresses",
                command="echo 1 > /proc/sys/net/ipv4/conf/all/rp_filter",
                verify_path="/proc/sys/net/ipv4/conf/all/rp_filter",
                verify_value="1",
            ),
            HardeningAction(
                name="Disable ICMP redirects",
                description="Prevent ICMP redirect-based attacks",
                command="echo 0 > /proc/sys/net/ipv4/conf/all/accept_redirects",
                verify_path="/proc/sys/net/ipv4/conf/all/accept_redirects",
                verify_value="0",
            ),
            HardeningAction(
                name="Restrict ptrace",
                description="Limit process tracing to parent processes only",
                command="echo 1 > /proc/sys/kernel/yama/ptrace_scope",
                verify_path="/proc/sys/kernel/yama/ptrace_scope",
                verify_value="1",
            ),
        ]

    def apply_all(self) -> list[dict]:
        """Apply all hardening actions."""
        results = []
        for action in self.actions:
            result = self.apply(action)
            results.append(result)
        return results

    def apply(self, action: HardeningAction) -> dict:
        """Apply a single hardening action."""
        # Check if already at desired state
        if action.verify_path and action.verify_value:
            if os.path.exists(action.verify_path):
                try:
                    with open(action.verify_path, "r") as f:
                        current = f.read().strip()
                    if current == action.verify_value:
                        action.applied = True
                        return action.to_dict()
                except (PermissionError, OSError):
                    pass

        # Apply via sysfs write (preferred over shell command)
        if action.verify_path and action.verify_value:
            try:
                with open(action.verify_path, "w") as f:
                    f.write(action.verify_value)
                action.applied = True
            except (PermissionError, OSError) as e:
                action.error = str(e)
        else:
            try:
                result = subprocess.run(
                    action.command, shell=True,
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    action.applied = True
                else:
                    action.error = result.stderr.strip()
            except (subprocess.TimeoutExpired, OSError) as e:
                action.error = str(e)

        return action.to_dict()

    def get_status(self) -> dict:
        """Return hardening status."""
        applied = sum(1 for a in self.actions if a.applied)
        failed = sum(1 for a in self.actions if a.error)
        return {
            "total_actions": len(self.actions),
            "applied": applied,
            "failed": failed,
            "pending": len(self.actions) - applied - failed,
            "actions": [a.to_dict() for a in self.actions],
        }
