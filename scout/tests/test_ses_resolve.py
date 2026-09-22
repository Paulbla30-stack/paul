"""Which address the digest actually goes to, and what it says when it cannot.

On 22 September 2026 the 06:00 run logged "SES: X and/or X not verified",
naming the same address twice because the sender and the recipient are the
same address, and fell back to the operator's personal mailbox. That message
says neither which end failed nor what would fix it.
"""

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from app import SesMailer  # noqa: E402

REAL = "paulblatherwick@heartbeat-framework.org"
FALLBACK = "Paulbla30@hotmail.com"

CFG = {"email": {
    "to": REAL, "sender": REAL,
    "fallback_to": FALLBACK, "fallback_sender": FALLBACK,
    "fallback_enabled": True, "subject_prefix": "JARVIS scout"}}


def mailer(verified, cfg=None):
    m = SesMailer.__new__(SesMailer)
    m.cfg = (cfg or CFG)["email"]
    m.prefix = m.cfg["subject_prefix"]
    m._verified = lambda: {v.lower() for v in verified}
    return m


class ResolveTest(unittest.TestCase):
    def test_unverified_falls_back(self):
        to, sender, fell = mailer({FALLBACK}).resolve()
        self.assertEqual((to, sender, fell), (FALLBACK, FALLBACK, True))

    def test_a_verified_address_is_used(self):
        to, sender, fell = mailer({REAL}).resolve()
        self.assertEqual((to, sender, fell), (REAL, REAL, False))

    def test_a_verified_domain_covers_the_address(self):
        # The real fix for sandbox: verify the domain with Easy DKIM and every
        # address on it is accepted. Checking only for the exact address would
        # report a working setup as broken and keep falling back forever.
        to, sender, fell = mailer({"heartbeat-framework.org"}).resolve()
        self.assertEqual((to, sender, fell), (REAL, REAL, False))

    def test_the_domain_must_match(self):
        _, _, fell = mailer({"example.org", FALLBACK}).resolve()
        self.assertTrue(fell)

    def test_the_warning_names_the_address_once(self):
        with self.assertLogs("jarvis.scout", level="WARNING") as cap:
            mailer({FALLBACK}).resolve()
        msg = "\n".join(cap.output)
        self.assertIn(REAL, msg)
        self.assertNotIn("and/or", msg)
        # to and sender are the same address; it should be named once, not twice.
        self.assertEqual(msg.count(REAL), 1)
        self.assertIn("domain", msg)

    def test_the_warning_names_both_ends_when_they_differ(self):
        cfg = {"email": dict(CFG["email"], sender="scout@heartbeat-framework.org")}
        with self.assertLogs("jarvis.scout", level="WARNING") as cap:
            mailer({FALLBACK}, cfg).resolve()
        msg = "\n".join(cap.output)
        self.assertIn(REAL, msg)
        self.assertIn("scout@heartbeat-framework.org", msg)

    def test_no_fallback_means_it_raises_rather_than_going_quiet(self):
        cfg = {"email": dict(CFG["email"], fallback_enabled=False)}
        with self.assertRaises(RuntimeError) as e:
            mailer({FALLBACK}, cfg).resolve()
        self.assertIn(REAL, str(e.exception))

    def test_case_does_not_decide_whether_mail_is_sent(self):
        cfg = {"email": dict(CFG["email"], to=REAL.upper(), sender=REAL.upper())}
        _, _, fell = mailer({"HEARTBEAT-FRAMEWORK.ORG"}, cfg).resolve()
        self.assertFalse(fell)


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
