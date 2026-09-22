"""Tests for the UI login: the token persists, and a session outlives a restart.

The operator kept being asked for a new code. The cause was not a timeout: the
runner minted a fresh token at every start and wrote it only under
/run/jarvis, which systemd deletes and recreates on restart, so every deploy
replaced the credential sitting in his browser.
"""

import logging
import os
import tempfile
import time
import unittest

from jarvis.agent.core import AgentCore
from jarvis.agent.executor import check_command_allowed, normalise_shell_policy
from jarvis.cloud import session
from jarvis.cloud.headless import HeadlessRunner

LOG = logging.getLogger("test")
NO_HW = {"display": None, "input": None, "memory": None, "storage": None}


def temp(name):
    return os.path.join(tempfile.mkdtemp(), name)


class TestSigning(unittest.TestCase):

    def setUp(self):
        self.key = session.load_or_create_key(temp("session.key"), LOG)

    def test_a_fresh_session_verifies(self):
        self.assertTrue(session.verify(self.key, session.issue(self.key)))

    def test_another_key_does_not_verify(self):
        other = session.load_or_create_key(temp("other.key"), LOG)
        self.assertFalse(session.verify(other, session.issue(self.key)))

    def test_a_tampered_session_does_not_verify(self):
        value = session.issue(self.key)
        broken = value[:-1] + ("a" if value[-1] != "a" else "b")
        self.assertFalse(session.verify(self.key, broken))

    def test_editing_the_expiry_does_not_verify(self):
        version, issued, expires, mac = session.issue(self.key).split(".")
        forged = ".".join([version, issued, str(int(expires) + 86400), mac])
        self.assertFalse(session.verify(self.key, forged))

    def test_an_expired_session_does_not_verify(self):
        old = session.issue(self.key, days=1, now=time.time() - 2 * 86400)
        self.assertFalse(session.verify(self.key, old))

    def test_malformed_input_is_refused_not_raised(self):
        for value in (None, "", "nonsense", "a.b.c.d", "v2.1.2.3"):
            self.assertFalse(session.verify(self.key, value))
        self.assertFalse(session.verify(None, session.issue(self.key)))

    def test_lifetime_is_clamped(self):
        value = session.issue(self.key, days=100000)
        _, issued, expires, _ = value.split(".")
        self.assertLessEqual(int(expires) - int(issued), session.MAX_DAYS * 86400)


class TestKeyFile(unittest.TestCase):

    def test_the_key_persists_across_loads(self):
        path = temp("session.key")
        first = session.load_or_create_key(path, LOG)
        self.assertEqual(session.load_or_create_key(path, LOG), first)

    def test_the_key_is_written_0600(self):
        path = temp("session.key")
        session.load_or_create_key(path, LOG)
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")

    def test_an_unwritable_key_disables_sessions_rather_than_faking_one(self):
        # A key that dies with the process would give cookies that stop
        # working at the next restart, which is the bug being fixed.
        self.assertIsNone(session.load_or_create_key("/proc/nowhere/session.key", LOG))

    def test_a_truncated_key_is_replaced(self):
        path = temp("session.key")
        with open(path, "w") as fh:
            fh.write("short\n")
        self.assertGreaterEqual(len(session.load_or_create_key(path, LOG)), 32)


class TestCookies(unittest.TestCase):

    def test_the_cookie_is_httponly_samesite_and_secure_over_tls(self):
        header = session.cookie_header("value", 30, secure=True)
        for flag in ("HttpOnly", "SameSite=Lax", "Secure", "Path=/"):
            self.assertIn(flag, header)

    def test_the_default_is_lax_so_a_link_from_another_app_stays_logged_in(self):
        # Strict withholds the cookie on a navigation arriving from an email,
        # a note or a message. That looked like being logged out, and meant
        # re-entering the token on every visit.
        self.assertEqual(session.DEFAULT_SAMESITE, "Lax")
        self.assertIn("SameSite=Lax", session.cookie_header("value", 30))
        self.assertNotIn("SameSite=Strict", session.cookie_header("value", 30))

    def test_strict_is_still_available_for_anyone_who_wants_the_friction(self):
        self.assertIn("SameSite=Strict",
                      session.cookie_header("value", 30, samesite="Strict"))

    def test_a_nonsense_samesite_falls_back_to_the_default(self):
        for bad in ("", None, "lax", "Whatever", "None; Path=/evil"):
            header = session.cookie_header("value", 30, samesite=bad)
            self.assertIn(f"SameSite={session.DEFAULT_SAMESITE}", header)
            self.assertEqual(header.count("SameSite="), 1, bad)

    def test_plain_http_does_not_claim_secure(self):
        self.assertNotIn("Secure", session.cookie_header("value", 30, secure=False))

    def test_clearing_expires_the_cookie(self):
        self.assertIn("Max-Age=0", session.clear_header())

    def test_clearing_matches_the_attributes_the_cookie_was_set_with(self):
        # A browser keeps the old cookie when the attributes differ, so a
        # logout that does not match silently does nothing.
        for samesite in ("Lax", "Strict"):
            set_header = session.cookie_header("value", 30, secure=True, samesite=samesite)
            clear = session.clear_header(secure=True, samesite=samesite)
            for flag in (f"SameSite={samesite}", "Path=/", "HttpOnly", "Secure"):
                self.assertIn(flag, set_header)
                self.assertIn(flag, clear)

    def test_the_value_is_found_among_other_cookies(self):
        value = "v1.1.2.abc"
        self.assertEqual(
            session.from_cookie_header(f"a=1; {session.COOKIE_NAME}={value}; b=2"), value)

    def test_no_cookie_header_is_no_session(self):
        self.assertIsNone(session.from_cookie_header(None))
        self.assertIsNone(session.from_cookie_header("a=1; b=2"))


class TestRunnerCredentials(unittest.TestCase):

    def agent(self):
        a = AgentCore({"name": "t", "profile": "cloud"}, dict(NO_HW), LOG)
        a.planner._boot_tasks_generated = True
        return a

    def runner(self, persist, key_file):
        return HeadlessRunner(self.agent(), LOG, token_file=None,
                              token_persist_file=persist, session_key_file=key_file)

    def test_the_token_survives_a_restart(self):
        persist, key_file = temp("token"), temp("session.key")
        first = self.runner(persist, key_file)
        first.start_status_server = None                   # not needed for this
        from jarvis.cloud.headless import write_token_file
        write_token_file(first.token, persist)
        second = self.runner(persist, key_file)
        self.assertEqual(second.token, first.token)

    def test_a_session_issued_before_a_restart_still_verifies_after(self):
        # The actual complaint: a redeploy used to log the operator out.
        persist, key_file = temp("token"), temp("session.key")
        before = self.runner(persist, key_file)
        cookie = before.new_session_cookie()
        value = cookie.split(";")[0].split("=", 1)[1]
        after = self.runner(persist, key_file)
        self.assertTrue(after.session_valid(f"{session.COOKIE_NAME}={value}"))

    def test_a_session_from_another_deployment_is_refused(self):
        theirs = self.runner(temp("token"), temp("session.key"))
        value = theirs.new_session_cookie().split(";")[0].split("=", 1)[1]
        mine = self.runner(temp("token"), temp("session.key"))
        self.assertFalse(mine.session_valid(f"{session.COOKIE_NAME}={value}"))

    def test_no_cookie_is_no_session(self):
        runner = self.runner(temp("token"), temp("session.key"))
        self.assertFalse(runner.session_valid(None))
        self.assertFalse(runner.session_valid("jarvis_session=rubbish"))

    def test_the_runner_issues_a_lax_cookie_by_default(self):
        runner = self.runner(temp("token"), temp("session.key"))
        self.assertEqual(runner.session_samesite, "Lax")
        self.assertIn("SameSite=Lax", runner.new_session_cookie())
        self.assertIn("SameSite=Lax", runner.clear_session_cookie())

    def test_the_runner_honours_a_configured_samesite(self):
        runner = HeadlessRunner(self.agent(), LOG, token_file=None,
                                token_persist_file=temp("token"),
                                session_key_file=temp("session.key"),
                                ui={"session_samesite": "Strict"})
        self.assertIn("SameSite=Strict", runner.new_session_cookie())
        self.assertIn("SameSite=Strict", runner.clear_session_cookie())

    def test_sessions_degrade_off_when_the_key_cannot_be_written(self):
        runner = self.runner(temp("token"), "/proc/nowhere/session.key")
        self.assertIsNone(runner.session_key)
        self.assertIsNone(runner.new_session_cookie())
        self.assertFalse(runner.session_valid("jarvis_session=anything"))


class TestCredentialsAreFencedFromTheAgent(unittest.TestCase):

    def test_the_agent_cannot_read_its_operator_s_credentials(self):
        policy = normalise_shell_policy({"enabled": True}, LOG)
        for command in ("cat /etc/jarvis/token", "cat /etc/jarvis/session.key",
                        "cat /run/jarvis/token"):
            self.assertIsNotNone(check_command_allowed(command, policy), command)


if __name__ == "__main__":
    unittest.main()
