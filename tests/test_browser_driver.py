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

    def test_a_link_to_the_metadata_service_is_refused_when_followed(self):
        """The case that matters: the href comes off the page, not the agent.

        A refactor once moved guard.check() up to the public open(), which
        left follow() -- the one path whose URL is chosen by the page --
        unchecked. This asserts the refusal through follow() rather than
        through open(), because that is where an untrusted address enters.
        """
        from jarvis.browser import guard
        driver = Driver()
        driver._last = a_page(links=[Link("L1", "Next", "http://169.254.169.254/")])
        with self.assertRaises(guard.Refused) as caught:
            driver.follow("L1")
        self.assertEqual(caught.exception.reason, guard.REASON_METADATA)

    def test_a_link_to_a_private_address_is_refused_when_followed(self):
        from jarvis.browser import guard
        driver = Driver()
        driver._last = a_page(links=[Link("L1", "Home", "http://127.0.0.1:8471/status")])
        with self.assertRaises(guard.Refused):
            driver.follow("L1")

    def test_the_source_never_dispatches_a_click_to_navigate(self):
        """Read the code, not the docstring, so a rewrite trips this."""
        import ast
        import inspect
        import textwrap
        tree = ast.parse(textwrap.dedent(inspect.getsource(Driver._follow)))
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


# ---- posting needs approval, and Paul pressing it himself does not --------

class TestPostingWaitsForPaul(unittest.TestCase):
    """The hard rule, at the driver: above the grant, before the browser."""

    def setUp(self):
        from jarvis.browser.page import Control
        self.driver = Driver()
        self.driver._last = Page(
            url="https://forum.test/thread", title="t", text="b",
            links=(Link("L1", "Next page", "https://forum.test/thread?page=2"),),
            fields=(Field(ref="F1", label="Your reply", kind="textarea"),),
            controls=(Control("C1", "Post reply", "button", in_form=True),
                      Control("C2", "Show 10 more", "button", in_form=False)))

    def test_a_submit_raises_needs_approval_with_the_publish_gate(self):
        from jarvis.browser.driver import NeedsApproval
        with self.assertRaises(NeedsApproval) as caught:
            self.driver.act("submit")
        self.assertEqual(caught.exception.gate, "publish")

    def test_pressing_a_button_inside_a_form_waits(self):
        from jarvis.browser.driver import NeedsApproval
        with self.assertRaises(NeedsApproval):
            self.driver.act("click", "C1")

    def test_pressing_a_button_outside_a_form_does_not_wait_here(self):
        """Fails for want of a browser, not by refusal: the gate let it through."""
        from jarvis.browser.driver import NeedsApproval
        with self.assertRaises(Exception) as caught:
            self.driver.act("click", "C2")
        self.assertNotIsInstance(caught.exception, NeedsApproval)
        self.assertNotIsInstance(caught.exception, PermissionError)

    def test_needs_approval_is_a_permission_error_but_a_distinct_one(self):
        """The service must be able to tell 'ask Paul' from 'never'."""
        from jarvis.browser.driver import NeedsApproval
        self.assertTrue(issubclass(NeedsApproval, PermissionError))

    def test_it_is_raised_before_the_browser_is_started(self):
        from jarvis.browser.driver import NeedsApproval
        started = []
        self.driver._start = lambda: started.append(1)
        with self.assertRaises(NeedsApproval):
            self.driver.act("submit")
        self.assertEqual(started, [])

    def test_paul_pressing_it_himself_is_not_asked(self):
        """operator=True: there is nobody to ask. Fails only for want of a browser."""
        from jarvis.browser.driver import NeedsApproval
        with self.assertRaises(Exception) as caught:
            self.driver.act("click", "C1", operator=True)
        self.assertNotIsInstance(caught.exception, NeedsApproval)

    def test_operator_does_not_lift_the_secret_refusal(self):
        """That one is a property of the browser, not a judgement about who asks."""
        self.driver._last = a_page(fields=[SECRET])
        with self.assertRaises(PermissionError) as caught:
            self.driver.act("type", "F1", "hunter2", operator=True)
        from jarvis.browser.driver import NeedsApproval
        self.assertNotIsInstance(caught.exception, NeedsApproval)

    def test_typing_into_a_textarea_arms_the_page(self):
        """After composing, even a plain button waits."""
        from jarvis.browser.driver import NeedsApproval
        self.driver._typed_composing.add("F1")
        with self.assertRaises(NeedsApproval) as caught:
            self.driver.act("click", "C2")
        self.assertIn("composed", caught.exception.detail)

    def test_navigating_disarms_it(self):
        from unittest import mock
        from jarvis.browser import driver as _drv
        self.driver._typed_composing.add("F1")
        self.driver._start = lambda: None
        class _P:
            url = "https://forum.test/other"
            def goto(self, *a, **k): return None
        self.driver._page = _P()
        self.driver._extract = lambda status=None: self.driver._last
        # forum.test has no DNS; the guard is not what this test is about.
        with mock.patch.object(_drv.guard, "check", lambda url: url):
            self.driver._open("https://forum.test/other")
        self.assertEqual(self.driver._typed_composing, set())


class TestOriginTrustAtTheDriver(unittest.TestCase):

    def test_an_unapproved_site_while_signed_in_waits_with_the_origin_gate(self):
        from jarvis.browser.driver import NeedsApproval
        from jarvis.browser.page import Control
        d = Driver(persistent=True)
        d._last = Page(url="https://bank.test/", title="t", text="b",
                       controls=(Control("C1", "Show more", in_form=False),))
        with self.assertRaises(NeedsApproval) as caught:
            d.act("click", "C1", approved=[])
        self.assertEqual(caught.exception.gate, "origin")


class TestElementsAreLocatedByStampNotPosition(unittest.TestCase):
    """The extractor reports only shown elements; the DOM holds the rest.

    So a position-based locator drifts: on the first live proof the page's
    F12 resolved to a checkbox in the DOM and the fill failed. The extractor
    now stamps data-jarvis-ref on each reported element and the driver locates
    by that stamp. Read from the source, so a rewrite back to nth() trips it.
    """

    def test_the_extractor_stamps_links_fields_and_controls(self):
        from jarvis.browser.page import EXTRACT_JS
        self.assertEqual(EXTRACT_JS.count("setAttribute('data-jarvis-ref'"), 3)
        self.assertIn("data-jarvis-form", EXTRACT_JS)

    def test_the_driver_locates_by_the_stamp(self):
        import ast, inspect, textwrap
        src = textwrap.dedent(inspect.getsource(Driver._act))
        self.assertIn('data-jarvis-ref', src)
        called = {n.func.attr for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertNotIn("nth", called, "an element is being located by position again")

    def test_submit_treats_the_navigation_teardown_as_success(self):
        import inspect
        src = inspect.getsource(Driver._act)
        self.assertIn("context was destroyed", src)


class TestReadingAfterANavigationDoesNotDieInTheGap(unittest.TestCase):
    """wait_for_load_state can return for the OLD document. Then the read lands
    between teardown and the new document. Seen live: the submit succeeded and
    the read after it did not."""

    def test_extract_retries_across_the_teardown(self):
        d = Driver()
        calls = {"n": 0}
        class _P:
            def evaluate(self, js):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("Execution context was destroyed, most likely because of a navigation")
                return {"url": "https://e.test/after", "title": "after", "text": "b"}
            def wait_for_load_state(self, *a, **k): return None
        d._page = _P()
        page = d._extract()
        self.assertEqual(page.url, "https://e.test/after")
        self.assertEqual(calls["n"], 2)

    def test_an_unrelated_error_is_not_swallowed(self):
        d = Driver()
        class _P:
            def evaluate(self, js): raise RuntimeError("something else entirely")
            def wait_for_load_state(self, *a, **k): return None
        d._page = _P()
        with self.assertRaises(RuntimeError):
            d._extract()

    def test_it_gives_up_rather_than_looping(self):
        d = Driver()
        class _P:
            def evaluate(self, js): raise RuntimeError("Execution context was destroyed")
            def wait_for_load_state(self, *a, **k): return None
        d._page = _P()
        with self.assertRaises(RuntimeError):
            d._extract()
