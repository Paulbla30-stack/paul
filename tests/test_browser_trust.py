"""Per-origin trust: the control that makes staying signed in survivable.

Jarvis's design, given when it was asked what it would want structurally now
that Paul had decided to let it act without asking. Acting and staying signed
in are each survivable alone; together they mean an injected instruction on
any page can act as him on any site he is signed into, and no care about the
*text* prevents that.

So the rule is on the pairing:

  clean profile      -> acting is free. Nothing is signed in; a click by the
                        agent is a click by a stranger.
  persistent profile -> each origin is approved once, by Paul.

Widening one capability re-narrows the other, automatically, rather than by
anyone remembering to.
"""

import unittest

from jarvis.browser import trust


class TestWhatAnOriginIs(unittest.TestCase):
    """Trust is granted in units, and the unit has to be exact."""

    def test_scheme_host_and_nothing_else(self):
        self.assertEqual(trust.origin("https://example.com/a/b?q=1#x"),
                         "https://example.com")

    def test_case_does_not_make_a_second_site(self):
        self.assertEqual(trust.origin("HTTPS://Example.COM/"), "https://example.com")

    def test_a_trailing_dot_is_the_same_host(self):
        self.assertEqual(trust.origin("https://example.com./"), "https://example.com")

    def test_the_default_port_is_not_written_out(self):
        self.assertEqual(trust.origin("https://example.com:443/"), "https://example.com")
        self.assertEqual(trust.origin("http://example.com:80/"), "http://example.com")

    def test_a_non_default_port_is_part_of_the_site(self):
        self.assertEqual(trust.origin("http://example.com:8080/"),
                         "http://example.com:8080")

    def test_http_and_https_are_different_sites(self):
        """Approving a site must not quietly approve a plaintext version.

        Otherwise an approval made over TLS is spendable by anyone who can get
        the browser to the http:// spelling.
        """
        self.assertNotEqual(trust.origin("https://example.com/"),
                            trust.origin("http://example.com/"))

    def test_a_subdomain_is_a_different_site(self):
        self.assertNotEqual(trust.origin("https://pay.example.com/"),
                            trust.origin("https://example.com/"))

    def test_rubbish_is_empty_rather_than_a_guess(self):
        for bad in ("", "not a url", "://x", None):
            self.assertEqual(trust.origin(bad), "")


class TestACleanProfileIsFree(unittest.TestCase):

    def test_acting_anywhere_is_allowed(self):
        got, why = trust.decide("https://anything.test/x", persistent=False)
        self.assertEqual(got, trust.ALLOW)

    def test_the_reason_says_why_it_is_safe(self):
        _, why = trust.decide("https://anything.test/x", persistent=False)
        self.assertIn("nobody to be", why)

    def test_an_approved_list_is_not_needed(self):
        got, _ = trust.decide("https://anything.test/", persistent=False, approved=[])
        self.assertEqual(got, trust.ALLOW)


class TestASignedInProfileIsPerOrigin(unittest.TestCase):

    def test_an_unapproved_site_needs_approval(self):
        got, why = trust.decide("https://bank.test/pay", persistent=True, approved=[])
        self.assertEqual(got, trust.NEEDS_APPROVAL)
        self.assertIn("bank.test", why)

    def test_needs_approval_is_not_the_same_as_refused(self):
        """Two different pieces of news, and the agent must report the right one."""
        self.assertNotEqual(trust.NEEDS_APPROVAL, trust.REFUSE)

    def test_an_approved_site_is_allowed(self):
        got, _ = trust.decide("https://bank.test/pay", persistent=True,
                              approved=["https://bank.test"])
        self.assertEqual(got, trust.ALLOW)

    def test_approval_of_one_site_does_not_approve_another(self):
        got, _ = trust.decide("https://other.test/", persistent=True,
                              approved=["https://bank.test"])
        self.assertEqual(got, trust.NEEDS_APPROVAL)

    def test_approval_written_with_a_path_still_approves_the_site(self):
        # Paul will paste a URL, not an origin.
        got, _ = trust.decide("https://bank.test/pay", persistent=True,
                              approved=["https://bank.test/login?next=/home"])
        self.assertEqual(got, trust.ALLOW)

    def test_approving_https_does_not_approve_http(self):
        got, _ = trust.decide("http://bank.test/pay", persistent=True,
                              approved=["https://bank.test"])
        self.assertEqual(got, trust.NEEDS_APPROVAL)


class TestSecretsAreASeparateSwitch(unittest.TestCase):
    """Pressing 'next page' and filling in a password are not one decision."""

    def test_a_password_page_is_refused_even_on_an_approved_site(self):
        got, why = trust.decide("https://bank.test/login", persistent=True,
                                approved=["https://bank.test"], has_secret=True)
        self.assertEqual(got, trust.REFUSE)
        self.assertIn("not switched on", why)

    def test_a_password_page_is_refused_even_with_a_clean_profile(self):
        got, _ = trust.decide("https://bank.test/login", persistent=False,
                              has_secret=True)
        self.assertEqual(got, trust.REFUSE)

    def test_it_is_allowed_once_the_operator_unlocks_it(self):
        got, _ = trust.decide("https://bank.test/login", persistent=True,
                              approved=["https://bank.test"], has_secret=True,
                              secrets_unlocked=True)
        self.assertEqual(got, trust.ALLOW)

    def test_unlocking_secrets_does_not_also_approve_the_site(self):
        got, _ = trust.decide("https://bank.test/login", persistent=True,
                              approved=[], has_secret=True, secrets_unlocked=True)
        self.assertEqual(got, trust.NEEDS_APPROVAL)


class TestNormalisingTheApprovedList(unittest.TestCase):

    def test_entries_are_reduced_to_origins(self):
        self.assertEqual(trust.normalise_approved(["HTTPS://Example.COM/a/b"]),
                         frozenset({"https://example.com"}))

    def test_two_spellings_of_one_site_collapse(self):
        got = trust.normalise_approved(["https://example.com/a",
                                        "https://EXAMPLE.com:443/b"])
        self.assertEqual(len(got), 1)

    def test_a_bare_string_is_not_a_list_of_sites(self):
        self.assertEqual(trust.normalise_approved("https://example.com"), frozenset())

    def test_rubbish_entries_are_dropped(self):
        self.assertEqual(trust.normalise_approved(["not a url", None, 7]), frozenset())


class TestNothingOpenIsRefused(unittest.TestCase):

    def test_acting_with_no_page_is_refused(self):
        got, _ = trust.decide("", persistent=False)
        self.assertEqual(got, trust.REFUSE)


class TestWhatTheModelIsTold(unittest.TestCase):

    def test_a_clean_profile_is_described_as_free(self):
        self.assertIn("act on any page", trust.describe(False))

    def test_a_signed_in_profile_names_the_approved_sites(self):
        said = trust.describe(True, ["https://bank.test"])
        self.assertIn("bank.test", said)

    def test_with_nothing_approved_it_says_so_rather_than_listing_nothing(self):
        self.assertIn("none yet", trust.describe(True, []))


if __name__ == "__main__":
    unittest.main()
