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
"""

import os
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


class SecurityScanner:
    """
    Comprehensive system security vulnerability scanner.

    Runs multiple scan modules and aggregates findings into a report.
    """

    def __init__(self):
        self.findings: list[Finding] = []

    def full_scan(self) -> dict:
        """Run all security scans and return a report."""
        self.findings.clear()

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
            "/proc/sys/net/ipv4/conf/all/rp_filter": {
                "name": "reverse path filtering",
                "expected": "1",
                "severity": Finding.WARNING,
                "remediation": "echo 1 > /proc/sys/net/ipv4/conf/all/rp_filter",
            },
            "/proc/sys/net/ipv4/icmp_ignore_bogus_error_responses": {
                "name": "ICMP bogus error response ignore",
                "expected": "1",
                "severity": Finding.INFO,
                "remediation": "echo 1 > /proc/sys/net/ipv4/icmp_ignore_bogus_error_responses",
            },
        }

        for path, check in checks.items():
            if os.path.exists(path):
                try:
                    with open(path, "r") as f:
                        value = f.read().strip()
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
                except (PermissionError, OSError):
                    pass

        # Check for kernel lockdown
        lockdown_path = "/sys/kernel/security/lockdown"
        if os.path.exists(lockdown_path):
            try:
                with open(lockdown_path, "r") as f:
                    lockdown = f.read().strip()
                if "none" in lockdown.lower():
                    self.findings.append(Finding(
                        title="Kernel lockdown disabled",
                        description=f"Lockdown status: {lockdown}",
                        severity=Finding.WARNING,
                        category="kernel",
                        remediation="Boot with lockdown=integrity or lockdown=confidentiality",
                    ))
            except (PermissionError, OSError):
                pass

    def _scan_memory_protections(self):
        """Check memory protection features."""
        # Check NX bit support
        try:
            with open("/proc/cpuinfo", "r") as f:
                cpuinfo = f.read()
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
        except FileNotFoundError:
            pass

        # Check SMEP/SMAP
        try:
            with open("/proc/cpuinfo", "r") as f:
                cpuinfo = f.read().lower()
            for feature, name in [("smep", "SMEP"), ("smap", "SMAP")]:
                if feature not in cpuinfo:
                    self.findings.append(Finding(
                        title=f"{name} not detected",
                        description=f"CPU may not support {name}",
                        severity=Finding.WARNING,
                        category="memory",
                    ))
        except FileNotFoundError:
            pass

        # Check for swap encryption
        try:
            with open("/proc/swaps", "r") as f:
                swaps = f.read()
            if len(swaps.strip().split("\n")) > 1:  # Has swap
                # Check if swap is encrypted (dm-crypt)
                self.findings.append(Finding(
                    title="Swap space detected",
                    description="Verify swap is encrypted to prevent data leakage",
                    severity=Finding.WARNING,
                    category="memory",
                    remediation="Use encrypted swap or disable swap entirely",
                ))
        except FileNotFoundError:
            pass

    def _scan_file_permissions(self):
        """Check for common file permission issues."""
        sensitive_files = [
            ("/etc/shadow", 0o640, "Shadow password file"),
            ("/etc/gshadow", 0o640, "Group shadow file"),
            ("/etc/passwd", 0o644, "Password file"),
            ("/etc/ssh/sshd_config", 0o600, "SSH server config"),
            ("/root/.ssh", 0o700, "Root SSH directory"),
            ("/boot/grub/grub.cfg", 0o600, "GRUB config"),
        ]

        for path, max_mode, description in sensitive_files:
            if os.path.exists(path):
                try:
                    file_stat = os.stat(path)
                    mode = stat.S_IMODE(file_stat.st_mode)
                    if mode > max_mode:
                        self.findings.append(Finding(
                            title=f"Excessive permissions on {path}",
                            description=(
                                f"{description}: mode {oct(mode)} "
                                f"(should be {oct(max_mode)} or stricter)"
                            ),
                            severity=Finding.WARNING,
                            category="permissions",
                            remediation=f"chmod {oct(max_mode)} {path}",
                        ))
                except (PermissionError, OSError):
                    pass

    def _scan_network_services(self):
        """Scan for open ports and network services."""
        # Check /proc/net/tcp for listening sockets
        for proto, path in [("tcp", "/proc/net/tcp"), ("tcp6", "/proc/net/tcp6")]:
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r") as f:
                    lines = f.readlines()[1:]  # Skip header
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
            except (PermissionError, OSError):
                pass

        # Check for commonly exploited services
        try:
            result = subprocess.run(
                ["ss", "-tlnp"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
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
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    def _scan_boot_integrity(self):
        """Check boot chain integrity."""
        # Check for Secure Boot
        sb_path = "/sys/firmware/efi/efivars/SecureBoot-*"
        sb_files = glob.glob(sb_path)
        if not sb_files:
            if os.path.isdir("/sys/firmware/efi"):
                self.findings.append(Finding(
                    title="Secure Boot not detected",
                    description="System boots via UEFI but Secure Boot may be disabled",
                    severity=Finding.WARNING,
                    category="boot",
                    remediation="Enable Secure Boot in UEFI firmware settings",
                ))
        else:
            self.findings.append(Finding(
                title="Secure Boot detected",
                description="UEFI Secure Boot appears to be available",
                severity=Finding.INFO,
                category="boot",
            ))

        # Check GRUB config permissions
        for grub_cfg in ["/boot/grub/grub.cfg", "/boot/grub2/grub.cfg"]:
            if os.path.exists(grub_cfg):
                try:
                    mode = stat.S_IMODE(os.stat(grub_cfg).st_mode)
                    if mode & 0o077:  # Others or group can read/write
                        self.findings.append(Finding(
                            title="GRUB config has loose permissions",
                            description=f"{grub_cfg} mode: {oct(mode)}",
                            severity=Finding.WARNING,
                            category="boot",
                            remediation=f"chmod 600 {grub_cfg}",
                        ))
                except (PermissionError, OSError):
                    pass

    def _scan_suid_binaries(self):
        """Check for SUID/SGID binaries."""
        common_suid_dirs = ["/usr/bin", "/usr/sbin", "/bin", "/sbin"]
        suid_count = 0

        for search_dir in common_suid_dirs:
            if not os.path.isdir(search_dir):
                continue
            try:
                for entry in os.listdir(search_dir):
                    path = os.path.join(search_dir, entry)
                    try:
                        file_stat = os.stat(path)
                        mode = file_stat.st_mode
                        if mode & stat.S_ISUID or mode & stat.S_ISGID:
                            suid_count += 1
                    except (PermissionError, OSError):
                        pass
            except PermissionError:
                pass

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
                for entry in os.listdir(search_dir):
                    path = os.path.join(search_dir, entry)
                    try:
                        mode = os.stat(path).st_mode
                        if mode & stat.S_IWOTH and not os.path.islink(path):
                            world_writable.append(path)
                    except (PermissionError, OSError):
                        pass
            except PermissionError:
                pass

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
        if os.path.exists("/etc/passwd"):
            try:
                with open("/etc/passwd", "r") as f:
                    for line in f:
                        parts = line.strip().split(":")
                        if len(parts) >= 2 and parts[1] not in ("x", "*", "!"):
                            self.findings.append(Finding(
                                title="Password hash found in /etc/passwd",
                                description=f"User '{parts[0]}' has password hash in /etc/passwd",
                                severity=Finding.CRITICAL,
                                category="authentication",
                                remediation="Move password hashes to /etc/shadow using pwconv",
                            ))
            except (PermissionError, OSError):
                pass

    def _scan_ssh_config(self):
        """Check SSH configuration security."""
        sshd_config = "/etc/ssh/sshd_config"
        if not os.path.exists(sshd_config):
            return

        try:
            with open(sshd_config, "r") as f:
                config_text = f.read()

            dangerous_settings = {
                "PermitRootLogin yes": (
                    "Root login via SSH is enabled",
                    Finding.CRITICAL,
                    "Set PermitRootLogin to 'no' or 'prohibit-password'",
                ),
                "PasswordAuthentication yes": (
                    "Password authentication is enabled",
                    Finding.WARNING,
                    "Use key-based authentication instead",
                ),
                "PermitEmptyPasswords yes": (
                    "Empty passwords allowed for SSH",
                    Finding.CRITICAL,
                    "Set PermitEmptyPasswords to 'no'",
                ),
            }

            for pattern, (desc, severity, remediation) in dangerous_settings.items():
                if pattern in config_text:
                    self.findings.append(Finding(
                        title=f"SSH: {desc}",
                        description=f"Found '{pattern}' in {sshd_config}",
                        severity=severity,
                        category="ssh",
                        remediation=remediation,
                    ))
        except (PermissionError, OSError):
            pass

    def _scan_firewall(self):
        """Check firewall status."""
        has_firewall = False

        # Check iptables
        try:
            result = subprocess.run(
                ["iptables", "-L", "-n"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
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
        except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError):
            pass

        # Check nftables
        try:
            result = subprocess.run(
                ["nft", "list", "ruleset"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                has_firewall = True
                self.findings.append(Finding(
                    title="nftables firewall rules detected",
                    description="nftables ruleset is configured",
                    severity=Finding.INFO,
                    category="firewall",
                ))
        except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError):
            pass

        if not has_firewall:
            self.findings.append(Finding(
                title="No firewall detected",
                description="No iptables or nftables rules found",
                severity=Finding.WARNING,
                category="firewall",
                remediation="Configure a firewall to restrict network access",
            ))

    def _generate_report(self) -> dict:
        """Generate the final scan report."""
        critical = sum(1 for f in self.findings if f.severity == Finding.CRITICAL)
        warning = sum(1 for f in self.findings if f.severity == Finding.WARNING)
        info = sum(1 for f in self.findings if f.severity == Finding.INFO)

        return {
            "total": len(self.findings),
            "critical": critical,
            "warning": warning,
            "info": info,
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

    def get_compliance_score(self, report: dict) -> float:
        """Calculate a compliance score (0-100) from scan results."""
        if report["total"] == 0:
            return 100.0

        # Weight: critical=-20, warning=-5, info=0
        penalty = (report["critical"] * 20 + report["warning"] * 5)
        score = max(0, 100 - penalty)
        return round(score, 1)


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
            f.write(f"Info: {report['info']}\n")
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
