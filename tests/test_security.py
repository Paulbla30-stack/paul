"""Tests for Jarvis Security Scanner."""

import os
import tempfile
import unittest

from jarvis.security.scanner import SecurityScanner, Finding, SystemAuditor
from jarvis.security.hardening import SystemHardener, HardeningAction


class TestFinding(unittest.TestCase):
    """Tests for Finding."""

    def test_init(self):
        f = Finding(
            title="Test Finding",
            description="A test",
            severity=Finding.CRITICAL,
            category="test",
        )
        self.assertEqual(f.title, "Test Finding")
        self.assertEqual(f.severity, "critical")

    def test_to_dict(self):
        f = Finding(
            title="Test",
            description="desc",
            severity=Finding.WARNING,
            category="test",
            remediation="fix it",
        )
        d = f.to_dict()
        self.assertEqual(d["title"], "Test")
        self.assertEqual(d["severity"], "warning")
        self.assertEqual(d["remediation"], "fix it")


class TestSecurityScanner(unittest.TestCase):
    """Tests for SecurityScanner."""

    def test_init(self):
        scanner = SecurityScanner()
        self.assertEqual(len(scanner.findings), 0)

    def test_full_scan_returns_report(self):
        scanner = SecurityScanner()
        report = scanner.full_scan()
        self.assertIn("total", report)
        self.assertIn("critical", report)
        self.assertIn("warning", report)
        self.assertIn("info", report)
        self.assertIn("findings", report)
        self.assertIn("categories", report)

    def test_full_scan_counts(self):
        scanner = SecurityScanner()
        report = scanner.full_scan()
        total = report["total"]
        self.assertEqual(
            total,
            report["critical"] + report["warning"] + report["info"],
        )

    def test_findings_have_required_fields(self):
        scanner = SecurityScanner()
        report = scanner.full_scan()
        for finding in report["findings"]:
            self.assertIn("title", finding)
            self.assertIn("description", finding)
            self.assertIn("severity", finding)
            self.assertIn("category", finding)

    def test_severity_values(self):
        scanner = SecurityScanner()
        report = scanner.full_scan()
        valid_severities = {"critical", "warning", "info"}
        for finding in report["findings"]:
            self.assertIn(finding["severity"], valid_severities)

    def test_scan_clears_previous(self):
        scanner = SecurityScanner()
        scanner.full_scan()
        first_count = len(scanner.findings)
        scanner.full_scan()
        # Should have fresh findings, not accumulated
        self.assertEqual(len(scanner.findings), first_count)


class TestSystemAuditor(unittest.TestCase):
    """Tests for SystemAuditor."""

    def test_init(self):
        auditor = SystemAuditor()
        self.assertEqual(len(auditor.audit_history), 0)

    def test_run_audit(self):
        auditor = SystemAuditor()
        report = auditor.run_audit()
        self.assertIn("total", report)
        self.assertEqual(len(auditor.audit_history), 1)

    def test_compliance_score_perfect(self):
        auditor = SystemAuditor()
        report = {"total": 0, "critical": 0, "warning": 0, "info": 0}
        score = auditor.get_compliance_score(report)
        self.assertEqual(score, 100.0)

    def test_compliance_score_critical(self):
        auditor = SystemAuditor()
        report = {"total": 1, "critical": 1, "warning": 0, "info": 0}
        score = auditor.get_compliance_score(report)
        self.assertEqual(score, 80.0)

    def test_compliance_score_warning(self):
        auditor = SystemAuditor()
        report = {"total": 1, "critical": 0, "warning": 1, "info": 0}
        score = auditor.get_compliance_score(report)
        self.assertEqual(score, 95.0)

    def test_compliance_score_floor(self):
        auditor = SystemAuditor()
        report = {"total": 10, "critical": 10, "warning": 0, "info": 0}
        score = auditor.get_compliance_score(report)
        self.assertEqual(score, 0)


class TestHardeningAction(unittest.TestCase):
    """Tests for HardeningAction."""

    def test_init(self):
        action = HardeningAction(
            name="Test", description="A test action",
            command="echo test",
        )
        self.assertFalse(action.applied)
        self.assertIsNone(action.error)

    def test_to_dict(self):
        action = HardeningAction(
            name="Test", description="desc",
            command="echo test",
        )
        d = action.to_dict()
        self.assertEqual(d["name"], "Test")
        self.assertFalse(d["applied"])


class TestSystemHardener(unittest.TestCase):
    """Tests for SystemHardener."""

    def test_init(self):
        hardener = SystemHardener()
        self.assertGreater(len(hardener.actions), 0)

    def test_actions_have_names(self):
        hardener = SystemHardener()
        for action in hardener.actions:
            self.assertTrue(len(action.name) > 0)
            self.assertTrue(len(action.description) > 0)

    def test_get_status(self):
        hardener = SystemHardener()
        status = hardener.get_status()
        self.assertIn("total_actions", status)
        self.assertIn("applied", status)
        self.assertIn("actions", status)


class TestReversePathFilter(unittest.TestCase):
    """The check that produced a false positive on the live instance.

    The kernel's effective setting for an interface is max(conf.all,
    conf.<iface>). Reading conf.all alone called a machine unprotected while
    every interface was at 2, and the planner then proposed a fix for a
    problem that did not exist.
    """

    @staticmethod
    def tree(values: dict) -> str:
        root = tempfile.mkdtemp()
        for name, value in values.items():
            os.makedirs(os.path.join(root, name))
            with open(os.path.join(root, name, "rp_filter"), "w") as fh:
                fh.write(str(value))
        return root

    def check(self, values: dict):
        scanner = SecurityScanner()
        scanner.findings.clear()
        scanner._check_rp_filter(self.tree(values))
        return scanner.findings

    def test_interface_setting_covers_a_zero_in_all(self):
        # The live instance: conf.all=0, every interface at 2.
        found = self.check({"all": 0, "default": 2, "ens5": 2, "lo": 2})
        self.assertEqual(found[0].severity, Finding.INFO)

    def test_all_setting_covers_a_zero_on_an_interface(self):
        found = self.check({"all": 1, "default": 0, "eth0": 0, "lo": 0})
        self.assertEqual(found[0].severity, Finding.INFO)

    def test_loose_mode_is_not_a_finding(self):
        # 2 is the right choice where routing can be asymmetric.
        found = self.check({"all": 0, "eth0": 2})
        self.assertEqual(found[0].severity, Finding.INFO)

    def test_genuinely_unfiltered_interface_is_reported(self):
        found = self.check({"all": 0, "default": 0, "eth0": 0, "lo": 0})
        self.assertEqual(found[0].severity, Finding.WARNING)
        self.assertIn("eth0", found[0].description)

    def test_loopback_is_ignored(self):
        # lo at 0 alongside a filtered interface is not a finding.
        found = self.check({"all": 0, "eth0": 1, "lo": 0})
        self.assertEqual(found[0].severity, Finding.INFO)

    def test_missing_tree_is_silent(self):
        scanner = SecurityScanner()
        scanner.findings.clear()
        scanner._check_rp_filter("/definitely/not/here")
        self.assertEqual(scanner.findings, [])


class TestSecureBootState(unittest.TestCase):
    """The variable exists on every UEFI system; the value is the state."""

    @staticmethod
    def efivar(data: bytes) -> str:
        root = tempfile.mkdtemp()
        with open(os.path.join(root, "SecureBoot-abcd"), "wb") as fh:
            fh.write(data)
        return os.path.join(root, "SecureBoot-*")

    def test_enabled_is_read_from_the_value(self):
        self.assertIs(SecurityScanner._secure_boot_state(
            self.efivar(b"\x06\x00\x00\x00\x01")), True)

    def test_disabled_is_read_from_the_value(self):
        # What the live instance actually reports; presence alone had this
        # reported as "Secure Boot detected".
        self.assertIs(SecurityScanner._secure_boot_state(
            self.efivar(b"\x06\x00\x00\x00\x00")), False)

    def test_bare_single_byte_form_is_read(self):
        self.assertIs(SecurityScanner._secure_boot_state(self.efivar(b"\x01")), True)

    def test_absent_variable_is_unknown(self):
        self.assertIsNone(SecurityScanner._secure_boot_state("/nowhere/SecureBoot-*"))


if __name__ == "__main__":
    unittest.main()
