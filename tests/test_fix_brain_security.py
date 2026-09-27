"""Regression tests: planner JSON parsing and the security scanner.

Each test here failed on e7bf0a4 and passes after the fix it names.
"""

import ast
import json
import logging
import os
import stat
import tempfile
import unittest
from unittest import mock

from jarvis.agent.core import AgentCore
from jarvis.brain.bedrock import BedrockBrain
from jarvis.brain.llm import BaseBrain
from jarvis.security import scanner as scanner_mod
from jarvis.security.hardening import SystemHardener
from jarvis.security.scanner import Finding, SecurityScanner, SystemAuditor

LOG = logging.getLogger("test")
NO_HW = {"display": None, "input": None, "memory": None, "storage": None}
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def plan(**overrides):
    d = {"reasoning": "Check disk first.", "task_type": "shell_command",
         "description": "Measure root usage", "priority": 2, "command": "echo bedrock",
         "goal": "Keep root under 80%", "completed_goals": [], "note": ""}
    d.update(overrides)
    return d


def converse_response(text, stop="end_turn"):
    return {"output": {"message": {"role": "assistant", "content": [{"text": text}]}},
            "stopReason": stop, "usage": {"inputTokens": 10, "outputTokens": 5}}


class FakeBedrock:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0) if self.responses else converse_response(json.dumps(plan()))


def make_brain(responses):
    fake = FakeBedrock(responses)
    config = {"model": "meta.llama3-3-70b-instruct-v1:0", "region": "eu-west-2",
              "max_retries": 0}
    return BedrockBrain(config, LOG, client=fake), fake


def make_agent(brain):
    agent = AgentCore({"name": "t", "profile": "cloud"}, dict(NO_HW), LOG, brain=brain)
    agent.planner._boot_tasks_generated = True
    return agent


# ---- F1: _parse_plan --------------------------------------------------------

class TestParsePlan(unittest.TestCase):
    PROSE = "I will use {shell_command} to check. "

    def test_prose_braces_before_the_object_do_not_end_the_search(self):
        text = self.PROSE + json.dumps(plan(task_type="none"))
        self.assertEqual(BaseBrain._parse_plan(text)["task_type"], "none")
        # An object nested in a broken plan is not taken for the plan.
        broken = self.PROSE + '{"reasoning": "x", "hunch": {"about": "y"}, oops}'
        with self.assertRaises(ValueError):
            BaseBrain._parse_plan(broken)

    def test_non_object_json_is_an_error(self):
        for text in ("null", "[1, 2]", "3", '"a string"',
                     json.dumps([plan()])):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    BaseBrain._parse_plan(text)

    def test_fenced_block_after_prose_braces(self):
        text = (self.PROSE + "Here it is:\n```json\n" + json.dumps(plan())
                + "\n```\nDone {really}.")
        self.assertEqual(BaseBrain._parse_plan(text)["priority"], 2)

    def test_braces_inside_strings_after_prose_braces(self):
        inner = plan(reasoning='braces { } and "quotes" and } inside', note="{")
        parsed = BaseBrain._parse_plan(self.PROSE + json.dumps(inner) + " trailing }")
        self.assertEqual(parsed["reasoning"], inner["reasoning"])
        self.assertEqual(parsed["note"], "{")

    def test_bedrock_re_asks_on_a_null_plan(self):
        brain, fake = make_brain([converse_response("null"),
                                  converse_response(json.dumps(plan()))])
        decision = brain.plan(make_agent(brain), {})
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(decision.task.description, "Measure root usage")

    def test_bedrock_does_not_spend_the_re_ask_on_prose_braces(self):
        brain, fake = make_brain([converse_response(self.PROSE + json.dumps(plan()))])
        decision = brain.plan(make_agent(brain), {})
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(decision.task.description, "Measure root usage")

    HUNCH = {"about": "/var/log", "feeling": "grows", "despite": "df ok",
             "expect": "growth", "confidence": 0.3}

    def test_plan_missing_its_last_brace_is_not_read_as_its_hunch(self):
        # Scanning on from the unbalanced plan reaches the nested hunch,
        # which parses cleanly; it must not be taken for the plan.
        text = json.dumps(plan(hunch=dict(self.HUNCH)))[:-1]
        for prefix in ("", self.PROSE):
            with self.subTest(prefix=prefix):
                with self.assertRaises(ValueError) as cm:
                    BaseBrain._parse_plan(prefix + text)
                self.assertIn("unterminated", str(cm.exception))

    def test_unescaped_quote_does_not_hand_over_the_hunch(self):
        # One stray quote (a 5" disk) throws the brace matching off for the
        # outer object, leaving the hunch as the first balanced candidate.
        text = ('{"reasoning": "the 5" disk", "task_type": "none", '
                '"hunch": ' + json.dumps(self.HUNCH) + '}')
        with self.assertRaises(ValueError):
            BaseBrain._parse_plan(text)

    def test_object_without_task_type_is_skipped_for_the_plan(self):
        text = 'See {"x": 1} then ' + json.dumps(plan(task_type="none"))
        self.assertEqual(BaseBrain._parse_plan(text)["task_type"], "none")
        with self.assertRaises(ValueError):
            BaseBrain._parse_plan('Only {"x": 1} here.')

    def test_bedrock_re_asks_when_the_plan_loses_its_last_brace(self):
        text = json.dumps(plan(hunch=dict(self.HUNCH)))[:-1]
        brain, fake = make_brain([converse_response(text),
                                  converse_response(json.dumps(plan()))])
        decision = brain.plan(make_agent(brain), {})
        self.assertEqual(len(fake.calls), 2)
        self.assertFalse(decision.idle)
        self.assertEqual(decision.task.description, "Measure root usage")

    def test_a_non_object_plan_hands_over_to_the_fallback(self):
        # Retries exhausted on a list: plan() says "could not decide" (None)
        # rather than an idle decision nobody asked for.
        brain, fake = make_brain([converse_response("[]")] * 2)
        self.assertIsNone(brain.plan(make_agent(brain), {}))
        self.assertIn("not an object", brain.last_error)


# ---- F2: file permissions ---------------------------------------------------

class TestFilePermissionBits(unittest.TestCase):
    def scan(self, mode, allowed):
        fd, path = tempfile.mkstemp()
        os.close(fd)
        self.addCleanup(os.unlink, path)
        os.chmod(path, mode)
        s = SecurityScanner()
        with mock.patch.object(SecurityScanner, "SENSITIVE_FILES", [(path, allowed, "test file")]):
            s._scan_file_permissions()
        return [f for f in s.findings if f.category == "permissions"]

    def test_world_readable_below_the_number_is_still_flagged(self):
        found = self.scan(0o604, 0o640)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].severity, Finding.WARNING)
        self.assertIn("0o4 is beyond", found[0].description)

    def test_stricter_modes_pass(self):
        self.assertEqual(self.scan(0o640, 0o640), [])
        self.assertEqual(self.scan(0o440, 0o640), [])
        self.assertEqual(self.scan(0o000, 0o640), [])


# ---- F3: sshd_config --------------------------------------------------------

class TestSshdConfig(unittest.TestCase):
    def tree(self, files: dict) -> str:
        root = tempfile.mkdtemp()
        for rel, text in files.items():
            path = os.path.join(root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(text)
        return os.path.join(root, "sshd_config")

    def scan(self, files: dict):
        s = SecurityScanner()
        s._scan_ssh_config(self.tree(files))
        return s

    @staticmethod
    def titles(s):
        return [f.title for f in s.findings if f.category == "ssh"]

    def test_commented_default_is_not_read_as_live(self):
        s = self.scan({"sshd_config": "#PasswordAuthentication yes\n"
                                      "#PermitRootLogin yes\n"
                                      "PasswordAuthentication no\n"})
        self.assertEqual(self.titles(s), [])
        self.assertEqual(s.unchecked, [])

    def test_first_value_wins_and_case_does_not_matter(self):
        s = self.scan({"sshd_config": "permitrootlogin NO\nPermitRootLogin yes\n"
                                      "PasswordAuthentication=no\n"})
        self.assertEqual(self.titles(s), [])
        s = self.scan({"sshd_config": "PERMITROOTLOGIN Yes\nPermitRootLogin no\n"
                                      "PasswordAuthentication no\n"})
        self.assertEqual(self.titles(s), ["SSH: Root login via SSH is enabled"])

    def test_include_is_followed_and_the_file_is_named(self):
        # The Debian and cloud-init layout: the drop-in comes first and wins.
        s = self.scan({
            "sshd_config": "Include sshd_config.d/*.conf\nPasswordAuthentication no\n",
            "sshd_config.d/50-cloud-init.conf": "passwordauthentication yes\n",
        })
        found = [f for f in s.findings if f.category == "ssh"]
        self.assertEqual([f.title for f in found], ["SSH: Password authentication is enabled"])
        self.assertIn("50-cloud-init.conf line 1", found[0].description)

    def test_unset_keyword_reports_the_compiled_in_default(self):
        s = self.scan({"sshd_config": "Port 22\n"})
        found = [f for f in s.findings if f.category == "ssh"]
        self.assertEqual([f.title for f in found], ["SSH: Password authentication is enabled"])
        self.assertIn("compiled-in default", found[0].description)

    def test_match_block_is_conditional_not_global(self):
        s = self.scan({"sshd_config": "PasswordAuthentication no\n"
                                      "Match User backup\n"
                                      "    PermitRootLogin yes\n"})
        self.assertEqual(self.titles(s),
                         ["SSH: Root login via SSH is enabled under 'Match User backup'"])

    def test_include_loop_is_bounded_and_recorded(self):
        s = self.scan({"sshd_config": "Include sshd_config\nPasswordAuthentication no\n"})
        self.assertEqual(self.titles(s), [])
        self.assertTrue(any("nested deeper" in u["reason"] for u in s.unchecked))

    def test_unreadable_config_is_unchecked_not_clean(self):
        path = self.tree({"sshd_config/placeholder": ""})  # a directory: open() fails
        s = SecurityScanner()
        s._scan_ssh_config(path)
        self.assertEqual(self.titles(s), [])
        self.assertEqual([u["check"] for u in s.unchecked], ["SSH server configuration"])
        self.assertTrue(any(f.category == "unchecked" for f in s.findings))


# ---- F4: what could not be checked ------------------------------------------

def _denied(*_a, **_k):
    raise PermissionError(13, "Permission denied")


class TestUnchecked(unittest.TestCase):
    def test_a_scan_that_can_read_nothing_claims_nothing(self):
        with mock.patch.object(scanner_mod, "open", _denied, create=True), \
                mock.patch.object(scanner_mod.os, "stat", _denied), \
                mock.patch.object(scanner_mod.os, "lstat", _denied), \
                mock.patch.object(scanner_mod.os, "listdir", _denied), \
                mock.patch.object(scanner_mod.subprocess, "run", _denied):
            report = SecurityScanner().full_scan()
        for key in ("total", "critical", "warning", "info", "findings", "categories"):
            self.assertIn(key, report)
        self.assertEqual(report["checked"], 0)
        self.assertTrue(report["unchecked"])
        self.assertEqual({f["category"] for f in report["findings"]}, {"unchecked"})
        self.assertEqual(len(report["findings"]), len(report["unchecked"]))
        # Named before the long findings list, so a truncated view keeps it.
        keys = list(report)
        self.assertLess(keys.index("unchecked"), keys.index("findings"))
        for name in ("ASLR", "ptrace scope", "reverse path filtering",
                     "SSH server configuration", "iptables rules", "nftables rules"):
            self.assertIn(name, [u["check"] for u in report["unchecked"]])
        self.assertIsNone(SystemAuditor().get_compliance_score(report))
        self.assertIn("nothing could be checked", SystemAuditor().compliance(report)["basis"])

    def test_score_is_never_100_when_nothing_was_checked(self):
        report = {"total": 0, "critical": 0, "warning": 0, "info": 0,
                  "checked": 0, "unchecked": [], "findings": [], "categories": []}
        self.assertIsNone(SystemAuditor().get_compliance_score(report))

    def test_firewall_query_refused_is_not_no_firewall(self):
        refused = mock.Mock(returncode=1, stdout="", stderr="Permission denied (you must be root)")
        s = SecurityScanner()
        with mock.patch.object(scanner_mod.subprocess, "run", return_value=refused):
            s._scan_firewall()
        self.assertNotIn("No firewall detected", [f.title for f in s.findings])
        self.assertEqual([u["check"] for u in s.unchecked], ["iptables rules", "nftables rules"])

    def test_absent_sysctl_is_recorded(self):
        real_open = open

        def fake_open(path, *a, **k):
            if str(path).endswith("yama/ptrace_scope"):
                raise FileNotFoundError(2, "No such file or directory", path)
            return real_open(path, *a, **k)

        s = SecurityScanner()
        with mock.patch.object(scanner_mod, "open", fake_open, create=True):
            s._scan_kernel_security()
        self.assertIn({"check": "ptrace scope",
                       "reason": "/proc/sys/kernel/yama/ptrace_scope does not exist on this kernel"},
                      s.unchecked)


# ---- F5: rp_filter hardening agrees with the scanner ------------------------

class TestRpFilterHardening(unittest.TestCase):
    def action(self):
        [a] = [a for a in SystemHardener().actions
               if a.verify_path == "/proc/sys/net/ipv4/conf/all/rp_filter"]
        return a

    def test_hardens_to_loose_and_accepts_strict(self):
        a = self.action()
        self.assertEqual(a.verify_value, "2")
        self.assertIn("echo 2 >", a.command)
        self.assertTrue(a.satisfied_by("1"))
        self.assertTrue(a.satisfied_by("2"))
        self.assertFalse(a.satisfied_by("0"))

    def test_apply_leaves_strict_alone_and_raises_zero_to_loose(self):
        for before, after in (("1", "1"), ("2", "2"), ("0", "2")):
            with self.subTest(before=before):
                fd, path = tempfile.mkstemp()
                os.close(fd)
                self.addCleanup(os.unlink, path)
                with open(path, "w") as fh:
                    fh.write(before + "\n")
                a = self.action()
                a.verify_path = path
                result = SystemHardener().apply(a)
                self.assertTrue(result["applied"])
                with open(path) as fh:
                    self.assertEqual(fh.read().strip(), after)


# ---- F6: console f-strings --------------------------------------------------

class TestConsoleFStrings(unittest.TestCase):
    def test_no_f_string_without_a_placeholder(self):
        path = os.path.join(REPO, "jarvis", "ui", "console.py")
        with open(path) as fh:
            tree = ast.parse(fh.read())
        # A format spec such as ":<12s" is itself a JoinedStr; not a finding.
        specs = {id(n.format_spec) for n in ast.walk(tree)
                 if isinstance(n, ast.FormattedValue) and n.format_spec is not None}
        bare = [n.lineno for n in ast.walk(tree)
                if isinstance(n, ast.JoinedStr) and id(n) not in specs
                and not any(isinstance(v, ast.FormattedValue) for v in n.values)]
        self.assertEqual(bare, [])


if __name__ == "__main__":
    unittest.main()
