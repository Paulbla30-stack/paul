"""Tests for OpenClaw Security Scanner."""

import unittest

from openclaw.security.scanner import SecurityScanner, Finding, SystemAuditor
from openclaw.security.hardening import SystemHardener, HardeningAction


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


if __name__ == "__main__":
    unittest.main()
