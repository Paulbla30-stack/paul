"""Where the browser may not go.

This is the fence that matters most in the whole browser design, so it gets
the most tests. A headless browser on an EC2 instance that can reach
169.254.169.254 can be told by any page it loads to fetch the instance role's
credentials. Nothing else in the agent -- not the rung, not the deny-list, not
the ledger -- sits between that and the account, because the role is *below*
all of them.

The DNS-rebinding cases are the ones worth reading. Checking a hostname is
useless on its own: a name can resolve to a public address when it is checked
and to a link-local one when it is fetched. So the check resolves and
inspects every address, and the systemd layer is what holds if resolution
changes in between.
"""

import unittest

from jarvis.browser import guard


def resolves_to(*addresses):
    return lambda host: list(addresses)


PUBLIC = resolves_to("93.184.216.34")


class TestTheMetadataService(unittest.TestCase):
    """The one that would end the account."""

    def refuse(self, url, resolver=PUBLIC):
        with self.assertRaises(guard.Refused) as caught:
            guard.check(url, resolver=resolver)
        return caught.exception

    def test_the_aws_metadata_address_is_refused(self):
        why = self.refuse("http://169.254.169.254/latest/meta-data/iam/")
        self.assertEqual(why.reason, guard.REASON_METADATA)

    def test_it_is_refused_over_ipv6_too(self):
        why = self.refuse("http://[fd00:ec2::254]/latest/meta-data/")
        self.assertEqual(why.reason, guard.REASON_METADATA)

    def test_the_google_metadata_name_is_refused(self):
        why = self.refuse("https://metadata.google.internal/computeMetadata/v1/")
        self.assertEqual(why.reason, guard.REASON_METADATA)

    def test_the_aws_alias_that_still_resolves_inside_a_vpc(self):
        why = self.refuse("http://instance-data/latest/meta-data/")
        self.assertEqual(why.reason, guard.REASON_METADATA)

    def test_a_name_that_resolves_to_the_metadata_address_is_refused(self):
        """The rebinding case, which is the whole reason this resolves."""
        why = self.refuse("https://harmless.example/page",
                          resolver=resolves_to("169.254.169.254"))
        self.assertEqual(why.reason, guard.REASON_METADATA)

    def test_it_is_refused_even_when_one_of_several_addresses_is_public(self):
        # A name with two A records, one of them hostile, must not pass on the
        # strength of the good one: the browser picks, not us.
        why = self.refuse("https://split.example/",
                          resolver=resolves_to("93.184.216.34", "169.254.169.254"))
        self.assertEqual(why.reason, guard.REASON_METADATA)

    def test_the_reason_names_the_metadata_service_rather_than_saying_private(self):
        # Two different pieces of news. "Blocked: a private address" is a
        # misconfiguration; "blocked: the cloud metadata service" is someone
        # trying to take the account.
        why = self.refuse("http://169.254.169.254/")
        self.assertNotEqual(why.reason, guard.REASON_PRIVATE)


class TestEverythingElseThatIsNotPublic(unittest.TestCase):

    def refuse(self, url, resolver=PUBLIC):
        with self.assertRaises(guard.Refused) as caught:
            guard.check(url, resolver=resolver)
        return caught.exception

    def test_loopback_is_refused(self):
        # The agent's own API is on loopback. A page that could reach it could
        # drive the agent.
        self.assertEqual(self.refuse("http://127.0.0.1:8471/status").reason,
                         guard.REASON_PRIVATE)

    def test_loopback_by_name_is_refused(self):
        self.assertEqual(self.refuse("http://localhost:8477/",
                                     resolver=resolves_to("127.0.0.1")).reason,
                         guard.REASON_PRIVATE)

    def test_ipv6_loopback_is_refused(self):
        self.assertEqual(self.refuse("http://[::1]/").reason, guard.REASON_PRIVATE)

    def test_the_rfc1918_ranges_are_refused(self):
        for addr in ("10.0.0.5", "172.16.4.1", "192.168.1.1"):
            self.assertEqual(self.refuse(f"http://{addr}/").reason,
                             guard.REASON_PRIVATE, addr)

    def test_carrier_grade_nat_is_refused(self):
        self.assertEqual(self.refuse("http://100.64.0.1/").reason,
                         guard.REASON_PRIVATE)

    def test_a_public_name_resolving_privately_is_refused(self):
        self.assertEqual(self.refuse("https://ok.example/",
                                     resolver=resolves_to("192.168.0.9")).reason,
                         guard.REASON_PRIVATE)

    def test_a_host_that_does_not_resolve_is_refused(self):
        self.assertEqual(self.refuse("https://nope.invalid/",
                                     resolver=resolves_to()).reason,
                         guard.REASON_UNRESOLVABLE)


class TestSchemes(unittest.TestCase):

    def refuse(self, url):
        with self.assertRaises(guard.Refused) as caught:
            guard.check(url, resolver=PUBLIC)
        return caught.exception

    def test_file_urls_are_refused(self):
        # The browser runs on the box. file:// is how a page asks it to read
        # the disk it is standing on.
        self.assertEqual(self.refuse("file:///etc/jarvis/token").reason,
                         guard.REASON_SCHEME)

    def test_javascript_urls_are_refused(self):
        self.assertEqual(self.refuse("javascript:fetch('/x')").reason,
                         guard.REASON_SCHEME)

    def test_data_urls_are_refused(self):
        self.assertEqual(self.refuse("data:text/html,<script>x()</script>").reason,
                         guard.REASON_SCHEME)

    def test_a_url_with_no_host_is_refused(self):
        self.assertEqual(self.refuse("https:///nowhere").reason, guard.REASON_NO_HOST)

    def test_empty_is_refused_rather_than_allowed(self):
        with self.assertRaises(guard.Refused):
            guard.check("", resolver=PUBLIC)


class TestWhatIsAllowed(unittest.TestCase):
    """A fence that refuses everything is not a fence, it is an outage."""

    def test_an_ordinary_https_page_passes(self):
        self.assertEqual(guard.check("https://example.com/a?q=1", resolver=PUBLIC),
                         "https://example.com/a?q=1")

    def test_http_passes_too(self):
        self.assertTrue(guard.allowed("http://example.com/", resolver=PUBLIC))

    def test_a_public_literal_address_passes(self):
        self.assertTrue(guard.allowed("https://93.184.216.34/", resolver=PUBLIC))

    def test_a_trailing_dot_on_the_host_is_the_same_host(self):
        # example.com. and example.com are the same name, and a check that
        # treats them differently is a check with a bypass in it.
        self.assertTrue(guard.allowed("https://example.com./", resolver=PUBLIC))

    def test_allowed_returns_false_rather_than_raising(self):
        self.assertFalse(guard.allowed("http://169.254.169.254/", resolver=PUBLIC))


class TestCaseAndSpacing(unittest.TestCase):
    """The shapes a bypass usually takes."""

    def test_the_metadata_name_is_matched_whatever_the_case(self):
        with self.assertRaises(guard.Refused):
            guard.check("https://Metadata.Google.Internal/x", resolver=PUBLIC)

    def test_an_uppercase_scheme_is_still_a_scheme(self):
        self.assertTrue(guard.allowed("HTTPS://example.com/", resolver=PUBLIC))

    def test_surrounding_whitespace_does_not_change_the_answer(self):
        with self.assertRaises(guard.Refused):
            guard.check("  http://169.254.169.254/  ", resolver=PUBLIC)


if __name__ == "__main__":
    unittest.main()
