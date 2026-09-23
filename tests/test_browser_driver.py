"""The refusals the browser makes for itself, which no rung lifts.

Everything in the agent above this is a gate someone can be given the key to:
the permission spine opens with a config change, and the proposal card opens
with Paul clicking approve. These two do not open at all.

  * nothing is ever typed into a password field;
  * no form is ever submitted on a page that has one.

They are here rather than in a policy document for the reason the agent gave
when it was asked what it would refuse: *"if the capability exists, it will
eventually be triggered. Fence it at birth."*

They run before Chromium is started, which is both why they are testable
without a browser and why they are trustworthy: a check that needs the browser
up has already started doing the thing it is deciding about.
"""

import unittest

from jarvis.browser.driver import Driver
from jarvis.browser.page import Field, Link, Page


def a_page(fields=(), links=(), url="https://example.com/"):
    return Page(url=url, title="t", text="body", fields=tuple(fields),
                links=tuple(links))


SECRET = Field(ref="F1", label="Password", kind="password")
BOX = Field(ref="F2", label="Search", kind="text")
CARD = Field(ref="F3", label="Card number", kind="text", autocomplete="cc-number")


class TestTheTwoRefusalsNoRungLifts(unittest.TestCase):

    def setUp(self):
        self.driver = Driver()

    def test_it_never_types_into_a_password_field(self):
        self.driver._last = a_page(fields=[SECRET])
        with self.assertRaises(PermissionError) as caught:
            self.driver.act("type", "F1", "hunter2")
        self.assertIn("secret", str(caught.exception))

    def test_a_card_number_counts_as_a_secret(self):
        """Declared by the page's own autocomplete token, not guessed from a label."""
        self.driver._last = a_page(fields=[CARD])
        with self.assertRaises(PermissionError):
            self.driver.act("type", "F3", "4111111111111111")

    def test_it_never_submits_a_form_on_a_page_with_a_password_field(self):
        self.driver._last = a_page(fields=[BOX, SECRET])
        with self.assertRaises(PermissionError) as caught:
            self.driver.act("submit", "F2")
        self.assertIn("password", str(caught.exception))

    def test_the_refusal_is_about_the_page_not_the_field(self):
        # Submitting the harmless search box on a page that also has a login
        # form is still submitting on a login page.
        self.driver._last = a_page(fields=[BOX, SECRET])
        with self.assertRaises(PermissionError):
            self.driver.act("submit", "F2")

    def test_typing_into_an_ordinary_box_is_not_refused_here(self):
        """The fence must not be an outage: this fails for want of a browser,
        which is a different failure from being refused."""
        self.driver._last = a_page(fields=[BOX])
        with self.assertRaises(Exception) as caught:
            self.driver.act("type", "F2", "weather")
        self.assertNotIsInstance(caught.exception, PermissionError)


class TestWhatItWillNotBeAskedToDo(unittest.TestCase):

    def setUp(self):
        self.driver = Driver()

    def test_acting_before_reading_anything_is_an_error(self):
        with self.assertRaises(ValueError):
            self.driver.act("click", "L1")

    def test_an_unknown_action_is_an_error(self):
        self.driver._last = a_page()
        with self.assertRaises(ValueError):
            self.driver.act("download", "L1")

    def test_typing_at_a_ref_that_is_not_on_the_page(self):
        self.driver._last = a_page(fields=[BOX])
        with self.assertRaises(ValueError):
            self.driver.act("type", "F9", "x")

    def test_following_a_link_that_is_not_on_the_page(self):
        self.driver._last = a_page(links=[Link("L1", "Next", "https://e.test/b")])
        with self.assertRaises(ValueError):
            self.driver.follow("L7")

    def test_following_before_reading_anything(self):
        with self.assertRaises(ValueError):
            self.driver.follow("L1")


class TestRefs(unittest.TestCase):
    """Refs are one-based because people read them."""

    def test_l1_is_the_first(self):
        self.assertEqual(Driver._index_of("L1"), 0)

    def test_f12_is_the_twelfth(self):
        self.assertEqual(Driver._index_of("F12"), 11)

    def test_a_ref_with_no_number_is_an_error(self):
        with self.assertRaises(ValueError):
            Driver._index_of("L")

    def test_it_never_returns_a_negative_index(self):
        self.assertEqual(Driver._index_of("L0"), 0)


class TestFollowingDoesNotClick(unittest.TestCase):
    """The design point the agent won the argument on.

    Navigation resolves an href and navigates. It must never dispatch a click,
    because a click runs the page's own handler and a handler can do anything.
    """

    def test_follow_goes_through_open_and_therefore_through_the_guard(self):
        calls = []
        driver = Driver()
        driver._last = a_page(links=[Link("L1", "Next", "http://169.254.169.254/")])
        driver.open = lambda url: calls.append(url)
        driver.follow("L1")
        self.assertEqual(calls, ["http://169.254.169.254/"])

    def test_the_source_never_dispatches_a_click_to_navigate(self):
        """Read the code, not the docstring, so a rewrite trips this."""
        import ast
        import inspect
        import textwrap
        tree = ast.parse(textwrap.dedent(inspect.getsource(Driver.follow)))
        called = {n.func.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertNotIn("click", called)


class TestHealthIsHonestWhenItIsDown(unittest.TestCase):

    def test_a_browser_that_never_started_says_so(self):
        got = Driver().health()
        self.assertFalse(got["up"])
        self.assertIn("reason", got)

    def test_it_reports_the_clean_profile(self):
        self.assertIn("clean", Driver().health()["profile"])


if __name__ == "__main__":
    unittest.main()
