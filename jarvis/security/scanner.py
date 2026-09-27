#!/usr/bin/env python3
"""
Jarvis Security Vulnerability Scanner

Scans the system for security vulnerabilities including:
- Open ports and network services
- File permission issues
- Kernel security configuration
- Boot chain integrity
- Memory protection settings
- Known vulnerable patterns

A check that cannot be performed -- no root, a sysctl this kernel does not
expose, a tool that is not installed -- is recorded as unchecked, with the
reason, rather than passing silently. An absence of findings is not a
finding of none.
"""

import os
import re
import sys
import stat
import glob
import subprocess
import argparse
from typing import Optional


class Finding:
    """A single security finding."""

    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"

    def __init__(self, title: str, description: str, severity: str,
                 category: str, remediation: str = ""):
        self.title = title
        self.description = description
        self.severity = severity
        self.category = category
        self.remediation = remediation

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "description": self.description,
            "severity": self.severity,
            "category": self.category,
            "remediation": self.remediation,
        }


def _why(error: BaseException) -> str:
    """A short reason for a failed read, e.g. 'PermissionError: Permission denied'."""
    detail = getattr(error, "strerror", None) or str(error) or "no detail"
    return f"{error.__class__.__name__}: {detail}"


class SecurityScanner:
    """
    Comprehensive system security vulnerability scanner.

    Runs multiple scan modules and aggregates findings into a report.
    """

    # (path, most permissive allowed mode, description)
    SENSITIVE_FILES = [
        ("/etc/shadow", 0o640, "Shadow password file"),
        ("/etc/gshadow", 0o640, "Group shadow file"),
        ("/etc/passwd", 0o644, "Password file"),
        ("/etc/ssh/sshd_config", 0o600, "SSH server config"),
        ("/root/.ssh", 0o700, "Root SSH directory"),
        ("/boot/grub/grub.cfg", 0o600, "GRUB config"),
    ]

    SSHD_CONFIG = "/etc/ssh/sshd_config"
    # Bounds Include recursion so a file that includes itself cannot loop.
    SSHD_MAX_INCLUDE_DEPTH = 16
    # keyword (lower case) -> (display name, dangerous value, compiled-in
    # default, description, severity, remediation)
    SSHD_RISKS = {
        "permitrootlogin": (
            "PermitRootLogin", "yes", "prohibit-password",
            "Root login via SSH is enabled", Finding.CRITICAL,
            "Set PermitRootLogin to 'no' or 'prohibit-password'",
        ),
        "passwordauthentication": (
            "PasswordAuthentication", "yes", "yes",
            "Password authentication is enabled", Finding.WARNING,
            "Set PasswordAuthentication no and use key-based authentication",
        ),
        "permitemptypasswords": (
            "PermitEmptyPasswords", "yes", "no",
            "Empty passwords allowed for SSH", Finding.CRITICAL,
            "Set PermitEmptyPasswords to 'no'",
        ),
    }

    def __init__(self):
        self.findings: list[Finding] = []
        self.checked: list[str] = []
        self.unchecked: list[dict] = []

    def full_scan(self) -> dict:
        """Run all security scans and return a report."""
        self.findings.clear()
        self.checked.clear()
        self.unchecked.clear()

        self._scan_kernel_security()
        self._scan_memory_protections()
        self._scan_file_permissions()
        self._scan_network_services()
        self._scan_boot_integrity()
        self._scan_suid_binaries()
        self._scan_world_writable()
        self._scan_password_files()
        self._scan_ssh_config()
        self._scan_firewall()

        return self._generate_report()

    def _checked(self, name: str):
        self.checked.append(name)

    def _unchecked(self, name: str, reason: str):
        """Record a check that could not be performed, and say so in the findings."""
        self.unchecked.append({"check": name, "reason": reason})
        self.findings.append(Finding(
            title=f"Not checked: {name}",
            description=reason,
            severity=Finding.INFO,
            category="unchecked",
        ))

    def _scan_kernel_security(self):
        """Check kernel security configuration."""
        checks = {
            "/proc/sys/kernel/randomize_va_space": {
                "name": "ASLR",
                "expected": "2",
                "severity": Finding.CRITICAL,
                "remediation": "echo 2 > /proc/sys/kernel/randomize_va_space",
            },
            "/proc/sys/kernel/dmesg_restrict": {
                "name": "dmesg restriction",
                "expected": "1",
                "severity": Finding.WARNING,
                "remediation": "echo 1 > /proc/sys/kernel/dmesg_restrict",
            },
            "/proc/sys/kernel/kptr_restrict": {
                "name": "kernel pointer restriction",
                "expected": "1",
                "severity": Finding.WARNING,
                "remediation": "echo 1 > /proc/sys/kernel/kptr_restrict",
            },
            "/proc/sys/kernel/yama/ptrace_scope": {
                "name": "ptrace scope",
                "expected": "1",
                "severity": Finding.WARNING,
                "remediation": "echo 1 > /proc/sys/kernel/yama/ptrace_scope",
            },
            "/proc/sys/net/ipv4/icmp_ignore_bogus_error_responses": {
                "name": "ICMP bogus error response ignore",
                "expected": "1",
                "severity": Finding.INFO,
                "remediation": "echo 1 > /proc/sys/net/ipv4/icmp_ignore_bogus_error_responses",
            },
        }

        for path, check in checks.items():
            try:
                with open(path, "r") as f:
                    value = f.read().strip()
            except FileNotFoundError:
                self._unchecked(check["name"], f"{path} does not exist on this kernel")
                continue
            except OSError as e:
                self._unchecked(check["name"], f"{path} could not be read ({_why(e)})")
                continue
            self._checked(check["name"])
            if value != check["expected"]:
                self.findings.append(Finding(
                    title=f"{check['name']} not properly configured",
                    description=(
                        f"{path} = {value} "
                        f"(expected {check['expected']})"
                    ),
                    severity=check["severity"],
                    category="kernel",
                    remediation=check["remediation"],
                ))
            else:
                self.findings.append(Finding(
                    title=f"{check['name']} properly configured",
                    description=f"{path} = {value}",
                    severity=Finding.INFO,
                    category="kernel",
                ))

        if self._check_rp_filter():
            self._checked("reverse path filtering")
        else:
            self._unchecked("reverse path filtering",
                            "rp_filter could not be read for conf.all or for any interface")

        # Check for kernel lockdown
        lockdown_path = "/sys/kernel/security/lockdown"
        try:
            with open(lockdown_path, "r") as f:
                lockdown = f.read().strip()
        except FileNotFoundError:
            self._unchecked("kernel lockdown",
                            f"{lockdown_path} does not exist (securityfs not mounted, "
                            f"or no lockdown support in this kernel)")
        except OSError as e:
            self._unchecked("kernel lockdown", f"{lockdown_path} could not be read ({_why(e)})")
        else:
            self._checked("kernel lockdown")
            if "none" in lockdown.lower():
                self.findings.append(Finding(
                    title="Kernel lockdown disabled",
                    description=f"Lockdown status: {lockdown}",
                    severity=Finding.WARNING,
                    category="kernel",
                    remediation="Boot with lockdown=integrity or lockdown=confidentiality",
                ))

    def _scan_memory_protections(self):
        """Check memory protection features."""
        try:
            with open("/proc/cpuinfo", "r") as f:
                cpuinfo = f.read()
        except OSError as e:
            self._unchecked("CPU memory protections (NX, SMEP, SMAP)",
                            f"/proc/cpuinfo could not be read ({_why(e)})")
        else:
            self._checked("CPU memory protections (NX, SMEP, SMAP)")
            # Check NX bit support
            if "nx" not in cpuinfo.lower():
                self.findings.append(Finding(
                    title="NX (No-Execute) bit not detected",
                    description="CPU may not support NX bit for memory protection",
                    severity=Finding.CRITICAL,
                    category="memory",
                    remediation="Ensure NX/XD bit is enabled in BIOS",
                ))
            else:
                self.findings.append(Finding(
                    title="NX (No-Execute) bit enabled",
                    description="CPU supports NX bit memory protection",
                    severity=Finding.INFO,
                    category="memory",
                ))

            # Check SMEP/SMAP
            lowered = cpuinfo.lower()
            for feature, name in [("smep", "SMEP"), ("smap", "SMAP")]:
                if feature not in lowered:
                    self.findings.append(Finding(
                        title=f"{name} not detected",
                        description=f"CPU may not support {name}",
                        severity=Finding.WARNING,
                        category="memory",
                    ))

        # Check for swap encryption
        try:
            with open("/proc/swaps", "r") as f:
                swaps = f.read()
        except OSError as e:
            self._unchecked("swap", f"/proc/swaps could not be read ({_why(e)})")
        else:
            self._checked("swap")
            if len(swaps.strip().split("\n")) > 1:  # Has swap
                # Check if swap is encrypted (dm-crypt)
                self.findings.append(Finding(
                    title="Swap space detected",
                    description="Verify swap is encrypted to prevent data leakage",
                    severity=Finding.WARNING,
                    category="memory",
                    remediation="Use encrypted swap or disable swap entirely",
                ))

    def _scan_file_permissions(self):
        """Check for common file permission issues."""
        for path, max_mode, description in self.SENSITIVE_FILES:
            try:
                file_stat = os.stat(path)
            except FileNotFoundError:
                # Nothing there to be exposed. A parent the scanner may not
                # enter raises PermissionError instead, and lands below.
                continue
            except OSError as e:
                self._unchecked(f"permissions of {path}", f"could not stat ({_why(e)})")
                continue
            self._checked(f"permissions of {path}")
            mode = stat.S_IMODE(file_stat.st_mode)
            # Any bit beyond the allowed ones is excessive. Comparing the
            # numbers passed 0o604 (world-readable) against 0o640.
            excess = mode & ~max_mode
            if excess:
                self.findings.append(Finding(
                    title=f"Excessive permissions on {path}",
                    description=(
                        f"{description}: mode {oct(mode)} "
                        f"(should be {oct(max_mode)} or stricter; "
                        f"{oct(excess)} is beyond that)"
                    ),
                    severity=Finding.WARNING,
                    category="permissions",
                    remediation=f"chmod {mode & max_mode:o} {path}",
                ))

    def _scan_network_services(self):
        """Scan for open ports and network services."""
        # Check /proc/net/tcp for listening sockets
        for proto, path in [("tcp", "/proc/net/tcp"), ("tcp6", "/proc/net/tcp6")]:
            try:
                with open(path, "r") as f:
                    lines = f.readlines()[1:]  # Skip header
            except OSError as e:
                self._unchecked(f"listening {proto} ports", f"{path} could not be read ({_why(e)})")
                continue
            self._checked(f"listening {proto} ports")
            for line in lines:
                fields = line.strip().split()
                if len(fields) < 4:
                    continue
                # State 0A = LISTEN
                state = fields[3]
                if state == "0A":
                    local_addr = fields[1]
                    addr_parts = local_addr.split(":")
                    port = int(addr_parts[1], 16)
                    self.findings.append(Finding(
                        title=f"Listening {proto} port: {port}",
                        description=f"Service listening on {proto} port {port}",
                        severity=Finding.INFO,
                        category="network",
                    ))

        # Check for commonly exploited services
        name = "insecure services (telnet, FTP)"
        try:
            result = subprocess.run(
                ["ss", "-tlnp"],
                capture_output=True, text=True, timeout=5
            )
        except FileNotFoundError:
            self._unchecked(name, "ss is not installed")
            return
        except subprocess.TimeoutExpired:
            self._unchecked(name, "ss -tlnp timed out")
            return
        except OSError as e:
            self._unchecked(name, f"ss could not be run ({_why(e)})")
            return
        if result.returncode != 0:
            self._unchecked(name, f"ss -tlnp exited {result.returncode}: "
                                  f"{(result.stderr or '').strip()[:200]}")
            return
        self._checked(name)
        for line in result.stdout.split("\n"):
            for risky in ("telnet", ":23 ", ":21 ", "ftp"):
                if risky.lower() in line.lower():
                    self.findings.append(Finding(
                        title="Potentially insecure service detected",
                        description=f"Found: {line.strip()[:100]}",
                        severity=Finding.CRITICAL,
                        category="network",
                        remediation="Disable insecure services (telnet, FTP) and use SSH/SFTP",
                    ))

    def _check_rp_filter(self, base: str = "/proc/sys/net/ipv4/conf") -> bool:
        """Reverse path filtering, judged the way the kernel judges it.

        The effective setting for an interface is max(conf.all, conf.<iface>),
        so conf.all on its own says nothing. Reading only conf.all reported
        this machine as unprotected while every interface was set to 2, and
        the planner then proposed a fix for a problem that did not exist.

        Both 1 (strict) and 2 (loose) filter. Loose is the correct choice
        where routing can be asymmetric, which is common on cloud instances,
        so neither is a finding.

        Returns False when nothing could be read, so the caller can record
        the check as not performed.
        """
        def read(path):
            try:
                with open(path, "r") as fh:
                    return int(fh.read().strip())
            except (OSError, ValueError):
                return None

        all_value = read(os.path.join(base, "all", "rp_filter"))
        if all_value is None:
            return False
        try:
            names = sorted(n for n in os.listdir(base) if n not in ("all", "default", "lo"))
        except OSError:
            names = []

        effective = {}
        for name in names:
            value = read(os.path.join(base, name, "rp_filter"))
            if value is not None:
                effective[name] = max(all_value, value)
        if not effective:
            return False

        unprotected = sorted(n for n, v in effective.items() if v < 1)
        detail = ", ".join(f"{n}={v}" for n, v in sorted(effective.items()))
        if unprotected:
            self.findings.append(Finding(
                title="reverse path filtering not properly configured",
                description=(f"effective rp_filter (max of conf.all={all_value} and each "
                             f"interface): {detail}; no filtering on {', '.join(unprotected)}"),
                severity=Finding.WARNING,
                category="kernel",
                remediation=("set net.ipv4.conf.default.rp_filter=2 (loose) or 1 (strict) "
                             "in a sysctl drop-in and rebuild the image"),
            ))
        else:
            self.findings.append(Finding(
                title="reverse path filtering properly configured",
                description=f"effective rp_filter per interface: {detail}",
                severity=Finding.INFO,
                category="kernel",
            ))
        return True

    @staticmethod
    def _secure_boot_state(pattern: str = "/sys/firmware/efi/efivars/SecureBoot-*"):
        """True, False, or None when the variable cannot be read.

        The efivar is four attribute bytes followed by the one-byte state.
        """
        for path in glob.glob(pattern):
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            if len(data) >= 5:
                return bool(data[4])
            if len(data) == 1:
                return bool(data[0])
        return None

    def _scan_boot_integrity(self):
        """Check boot chain integrity."""
        # Secure Boot: the variable exists on every UEFI system, so its
        # presence says only that the firmware is UEFI. The state is in the
        # value. Reporting presence as "Secure Boot detected" claimed it was
        # on for a machine where it is off, next to a lockdown finding that
        # said the opposite.
        if os.path.isdir("/sys/firmware/efi"):
            state = self._secure_boot_state()
            if state is True:
                self._checked("Secure Boot")
                self.findings.append(Finding(
                    title="Secure Boot enabled",
                    description="UEFI Secure Boot is on",
                    severity=Finding.INFO,
                    category="boot",
                ))
            elif state is False:
                self._checked("Secure Boot")
                self.findings.append(Finding(
                    title="Secure Boot disabled",
                    description="System boots via UEFI with Secure Boot off",
                    severity=Finding.WARNING,
                    category="boot",
                    remediation="Enable Secure Boot in the firmware, or in the image build",
                ))
            else:
                self._unchecked("Secure Boot",
                                "UEFI system; the SecureBoot variable could not be read")

        # Check GRUB config permissions
        for grub_cfg in ["/boot/grub/grub.cfg", "/boot/grub2/grub.cfg"]:
            try:
                mode = stat.S_IMODE(os.stat(grub_cfg).st_mode)
            except FileNotFoundError:
                continue
            except OSError as e:
                self._unchecked(f"permissions of {grub_cfg}", f"could not stat ({_why(e)})")
                continue
            self._checked(f"permissions of {grub_cfg}")
            if mode & 0o077:  # Others or group can read/write
                self.findings.append(Finding(
                    title="GRUB config has loose permissions",
                    description=f"{grub_cfg} mode: {oct(mode)}",
                    severity=Finding.WARNING,
                    category="boot",
                    remediation=f"chmod 600 {grub_cfg}",
                ))

    def _scan_suid_binaries(self):
        """Check for SUID/SGID binaries."""
        common_suid_dirs = ["/usr/bin", "/usr/sbin", "/bin", "/sbin"]
        suid_count = 0

        for search_dir in common_suid_dirs:
            if not os.path.isdir(search_dir):
                continue
            try:
                entries = os.listdir(search_dir)
            except OSError as e:
                self._unchecked(f"SUID/SGID binaries in {search_dir}",
                                f"could not list ({_why(e)})")
                continue
            denied = 0
            for entry in entries:
                path = os.path.join(search_dir, entry)
                try:
                    file_stat = os.stat(path)
                except PermissionError:
                    denied += 1
                    continue
                except OSError:
                    continue  # dangling links and the like
                mode = file_stat.st_mode
                if mode & stat.S_ISUID or mode & stat.S_ISGID:
                    suid_count += 1
            if denied:
                self._unchecked(f"SUID/SGID binaries in {search_dir}",
                                f"{denied} entries could not be examined (permission denied)")
            else:
                self._checked(f"SUID/SGID binaries in {search_dir}")

        if suid_count > 0:
            self.findings.append(Finding(
                title=f"Found {suid_count} SUID/SGID binaries",
                description="SUID/SGID binaries run with elevated privileges",
                severity=Finding.INFO if suid_count < 20 else Finding.WARNING,
                category="permissions",
                remediation="Audit SUID/SGID binaries and remove unnecessary ones",
            ))

    def _scan_world_writable(self):
        """Check for world-writable files in sensitive locations."""
        sensitive_dirs = ["/etc", "/usr/lib", "/usr/bin"]
        world_writable = []

        for search_dir in sensitive_dirs:
            if not os.path.isdir(search_dir):
                continue
            try:
                entries = os.listdir(search_dir)
            except OSError as e:
                self._unchecked(f"world-writable files in {search_dir}",
                                f"could not list ({_why(e)})")
                continue
            denied = 0
            for entry in entries:
                path = os.path.join(search_dir, entry)
                try:
                    mode = os.stat(path).st_mode
                except PermissionError:
                    denied += 1
                    continue
                except OSError:
                    continue  # dangling links and the like
                if mode & stat.S_IWOTH and not os.path.islink(path):
                    world_writable.append(path)
            if denied:
                self._unchecked(f"world-writable files in {search_dir}",
                                f"{denied} entries could not be examined (permission denied)")
            else:
                self._checked(f"world-writable files in {search_dir}")

        if world_writable:
            self.findings.append(Finding(
                title=f"Found {len(world_writable)} world-writable files in sensitive dirs",
                description=f"Files: {', '.join(world_writable[:5])}",
                severity=Finding.CRITICAL,
                category="permissions",
                remediation="Remove world-writable bit from sensitive files",
            ))

    def _scan_password_files(self):
        """Check password file security."""
        # Check if password hashes are in /etc/passwd instead of /etc/shadow
        try:
            with open("/etc/passwd", "r") as f:
                lines = f.readlines()
        except OSError as e:
            self._unchecked("password hashes in /etc/passwd",
                            f"/etc/passwd could not be read ({_why(e)})")
            return
        self._checked("password hashes in /etc/passwd")
        for line in lines:
            parts = line.strip().split(":")
            if len(parts) >= 2 and parts[1] not in ("x", "*", "!"):
                self.findings.append(Finding(
                    title="Password hash found in /etc/passwd",
                    description=f"User '{parts[0]}' has password hash in /etc/passwd",
                    severity=Finding.CRITICAL,
                    category="authentication",
                    remediation="Move password hashes to /etc/shadow using pwconv",
                ))

    # "Keyword value", "Keyword=value" or "Keyword = value".
    _SSHD_LINE = re.compile(r"^(\S+?)(?:\s*=\s*|\s+)(.*)$")

    def _read_sshd_config(self, path: str, base: str, depth: int,
                          match: Optional[str], settings: dict,
                          conditional: list, problems: list) -> bool:
        """Walk one sshd config file the way sshd reads it.

        Comments and blank lines are skipped, keywords are case-insensitive,
        the first value obtained for a keyword wins, and Include is followed
        in place (globs sorted, relative paths against ``base``). Lines under
        a ``Match`` other than ``Match all`` apply only to some connections,
        so they go to ``conditional`` rather than the global settings.

        Returns False when ``path`` itself could not be read.
        """
        if depth > self.SSHD_MAX_INCLUDE_DEPTH:
            problems.append(f"{path}: Include nested deeper than "
                            f"{self.SSHD_MAX_INCLUDE_DEPTH}; not followed")
            return True
        try:
            with open(path, "r", errors="replace") as fh:
                lines = fh.readlines()
        except OSError as e:
            problems.append(f"{path} could not be read ({_why(e)})")
            return False

        for lineno, raw in enumerate(lines, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = self._SSHD_LINE.match(line)
            keyword = (m.group(1) if m else line).lower()
            value = m.group(2).strip() if m else ""
            if keyword == "match":
                match = None if value.lower() == "all" else value
                continue
            if keyword == "include":
                for pattern in value.split():
                    pattern = pattern.strip('"')
                    if not os.path.isabs(pattern):
                        pattern = os.path.join(base, pattern)
                    found = sorted(glob.glob(pattern))
                    if not found:
                        # glob returns nothing for a directory it may not
                        # read as well as for one with nothing in it.
                        parent = os.path.dirname(pattern)
                        try:
                            os.listdir(parent)
                        except FileNotFoundError:
                            pass
                        except OSError as e:
                            problems.append(f"Include {pattern} in {path}: "
                                            f"{parent} could not be listed ({_why(e)})")
                    for included in found:
                        self._read_sshd_config(included, base, depth + 1, match,
                                               settings, conditional, problems)
                continue
            if keyword not in self.SSHD_RISKS:
                continue
            first = value.split()[0].strip('"').lower() if value.split() else ""
            where = f"{path} line {lineno}"
            if match is None:
                settings.setdefault(keyword, (first, where))
            else:
                conditional.append((keyword, first, where, match))
        return True

    def _scan_ssh_config(self, sshd_config: Optional[str] = None):
        """Check SSH server configuration, as sshd would read it."""
        sshd_config = sshd_config or self.SSHD_CONFIG
        try:
            os.lstat(sshd_config)
        except FileNotFoundError:
            return  # no SSH server configuration on this machine
        except OSError:
            pass  # the read below fails too, and records why
        name = "SSH server configuration"
        settings: dict = {}
        conditional: list = []
        problems: list = []
        base = os.path.dirname(sshd_config)
        if not self._read_sshd_config(sshd_config, base, 0, None,
                                      settings, conditional, problems):
            self._unchecked(name, "; ".join(problems))
            return
        if problems:
            # A value set in a file that could not be read would have come
            # first, so what was found may not be what sshd uses.
            self._unchecked(f"{name} (part)", "; ".join(problems)[:1000])
        else:
            self._checked(name)

        for keyword, (label, bad, default, desc, severity, fix) in self.SSHD_RISKS.items():
            if keyword in settings:
                value, where = settings[keyword]
                description = f"{label} {value}, the first value set, in {where}"
            else:
                value, where = default, None
                description = (f"{label} is not set in {sshd_config} or its Includes; "
                               f"sshd's compiled-in default is {default}")
            if value == bad:
                self.findings.append(Finding(
                    title=f"SSH: {desc}",
                    description=description,
                    severity=severity,
                    category="ssh",
                    remediation=fix,
                ))

        for keyword, value, where, criteria in conditional:
            label, bad, _default, desc, severity, fix = self.SSHD_RISKS[keyword]
            if value == bad:
                self.findings.append(Finding(
                    title=f"SSH: {desc} under 'Match {criteria}'",
                    description=(f"{label} {value} in {where}, for connections "
                                 f"matching '{criteria}'"),
                    severity=severity,
                    category="ssh",
                    remediation=fix,
                ))

    def _scan_firewall(self):
        """Check firewall status."""
        has_firewall = False
        queried = []

        # Check iptables
        try:
            result = subprocess.run(
                ["iptables", "-L", "-n"],
                capture_output=True, text=True, timeout=5
            )
        except FileNotFoundError:
            self._unchecked("iptables rules", "iptables is not installed")
        except (subprocess.TimeoutExpired, OSError) as e:
            self._unchecked("iptables rules", f"iptables could not be run ({_why(e)})")
        else:
            if result.returncode == 0:
                queried.append("iptables")
                self._checked("iptables rules")
                rules = [l for l in result.stdout.split("\n")
                         if l and not l.startswith("Chain") and not l.startswith("target")]
                if rules:
                    has_firewall = True
                    self.findings.append(Finding(
                        title="iptables firewall rules detected",
                        description=f"Found {len(rules)} firewall rules",
                        severity=Finding.INFO,
                        category="firewall",
                    ))
            else:
                self._unchecked("iptables rules",
                                f"iptables -L exited {result.returncode}: "
                                f"{(result.stderr or '').strip()[:200]}")

        # Check nftables
        try:
            result = subprocess.run(
                ["nft", "list", "ruleset"],
                capture_output=True, text=True, timeout=5
            )
        except FileNotFoundError:
            self._unchecked("nftables rules", "nft is not installed")
        except (subprocess.TimeoutExpired, OSError) as e:
            self._unchecked("nftables rules", f"nft could not be run ({_why(e)})")
        else:
            if result.returncode == 0:
                queried.append("nft")
                self._checked("nftables rules")
                if result.stdout.strip():
                    has_firewall = True
                    self.findings.append(Finding(
                        title="nftables firewall rules detected",
                        description="nftables ruleset is configured",
                        severity=Finding.INFO,
                        category="firewall",
                    ))
            else:
                self._unchecked("nftables rules",
                                f"nft list ruleset exited {result.returncode}: "
                                f"{(result.stderr or '').strip()[:200]}")

        # "No firewall" is only a finding when something was actually asked.
        # Without root both tools fail, and that is not the same as no rules.
        if not has_firewall and queried:
            self.findings.append(Finding(
                title="No firewall detected",
                description=f"No rules found by {' or '.join(queried)}",
                severity=Finding.WARNING,
                category="firewall",
                remediation="Configure a firewall to restrict network access",
            ))

    def _generate_report(self) -> dict:
        """Generate the final scan report."""
        critical = sum(1 for f in self.findings if f.severity == Finding.CRITICAL)
        warning = sum(1 for f in self.findings if f.severity == Finding.WARNING)
        info = sum(1 for f in self.findings if f.severity == Finding.INFO)

        # "checked" and "unchecked" come before "findings" so they survive
        # when a consumer cuts the serialised report short.
        return {
            "total": len(self.findings),
            "critical": critical,
            "warning": warning,
            "info": info,
            "checked": len(self.checked),
            "unchecked": [dict(u) for u in self.unchecked],
            "findings": [f.to_dict() for f in self.findings],
            "categories": list(set(f.category for f in self.findings)),
        }

    def print_report(self, report: dict):
        """Pretty-print a scan report."""
        print("=" * 60)
        print("  Jarvis Security Vulnerability Scan Report")
        print("=" * 60)
        print(f"  Total findings: {report['total']}")
        print(f"  Critical:       {report['critical']}")
        print(f"  Warning:        {report['warning']}")
        print(f"  Info:           {report['info']}")
        if "checked" in report:
            print(f"  Checks run:     {report['checked']}")
            print(f"  Not checked:    {len(report.get('unchecked') or [])}")
        print("=" * 60)
        print()

        for severity in [Finding.CRITICAL, Finding.WARNING, Finding.INFO]:
            findings = [f for f in report["findings"] if f["severity"] == severity]
            if not findings:
                continue
            print(f"--- {severity.upper()} ---")
            for f in findings:
                print(f"  [{f['severity'].upper():8s}] {f['title']}")
                print(f"             {f['description']}")
                if f.get("remediation"):
                    print(f"             Fix: {f['remediation']}")
                print()


class SystemAuditor:
    """High-level system auditor that runs scans and tracks compliance."""

    def __init__(self):
        self.scanner = SecurityScanner()
        self.audit_history: list[dict] = []

    def run_audit(self) -> dict:
        """Run a full security audit."""
        report = self.scanner.full_scan()
        self.audit_history.append(report)
        return report

    def get_compliance_score(self, report: dict) -> Optional[float]:
        """Calculate a compliance score (0-100) from scan results.

        The score covers only the checks that ran: an unchecked item carries
        no penalty and earns no credit. When the report says nothing was
        checked the answer is None, not 100. ``compliance`` gives the score
        together with what it does and does not cover.
        """
        if "checked" in report and not report["checked"]:
            return None
        # A report from before "checked" existed cannot say what it covered;
        # it is scored as it always was.
        if report["total"] == 0:
            return 100.0

        # Weight: critical=-20, warning=-5, info=0
        penalty = (report["critical"] * 20 + report["warning"] * 5)
        score = max(0, 100 - penalty)
        return round(score, 1)

    def compliance(self, report: dict) -> dict:
        """The score, and the basis it was computed on."""
        score = self.get_compliance_score(report)
        unchecked = list(report.get("unchecked") or [])
        if "checked" not in report:
            basis = "the report does not say which checks ran"
        elif score is None:
            basis = "nothing could be checked, so there is no score"
        elif unchecked:
            basis = (f"covers the {report['checked']} checks that ran; "
                     f"{len(unchecked)} could not be performed and are not counted")
        else:
            basis = f"covers all {report['checked']} checks"
        return {"score": score, "checked": report.get("checked"),
                "unchecked": unchecked, "basis": basis}


def main():
    parser = argparse.ArgumentParser(description="Jarvis Security Scanner")
    parser.add_argument("--target", default="/", help="Target path to scan")
    parser.add_argument("--report", help="Write report to file")
    args = parser.parse_args()

    scanner = SecurityScanner()
    report = scanner.full_scan()
    scanner.print_report(report)

    if args.report:
        with open(args.report, "w") as f:
            f.write("Jarvis Security Scan Report\n")
            f.write("=" * 60 + "\n")
            f.write(f"Total: {report['total']} | ")
            f.write(f"Critical: {report['critical']} | ")
            f.write(f"Warning: {report['warning']} | ")
            f.write(f"Info: {report['info']} | ")
            f.write(f"Not checked: {len(report['unchecked'])}\n")
            f.write("=" * 60 + "\n\n")
            for finding in report["findings"]:
                f.write(f"[{finding['severity'].upper():8s}] {finding['title']}\n")
                f.write(f"           {finding['description']}\n")
                if finding.get("remediation"):
                    f.write(f"           Fix: {finding['remediation']}\n")
                f.write("\n")
        print(f"\nReport written to: {args.report}")

    # Exit with non-zero if critical findings
    sys.exit(1 if report["critical"] > 0 else 0)


if __name__ == "__main__":
    main()
