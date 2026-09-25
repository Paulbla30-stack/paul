"""Saying something in public is not the same as reading, and needs asking.

Paul's hard rule: "if jarvis wants to post on something he get approval first."
The classifier's direction is the whole design and is what these tests pin:
an action is publishing unless it is *recognisably* not. The structural
signals -- a submit, a press inside a form, text composed on the page -- force
approval on their own; the word list is only ever an additional trigger,
because a page controls what its buttons say and cannot control what a form
submission is.
"""

import unittest

from jarvis.browser import publish as P


class TestWhatIsPlainlyNotPublishing(unittest.TestCase):

    def test_moving_about_is_free(self):
        for kind in ("scroll", "back", "forward", "reload"):
            self.assertEqual(P.classify(kind)[0], P.FREE, kind)

    def test_clicking_a_plain_link_or_button_is_free(self):
        self.assertEqual(P.classify("click", element_text="Next page")[0], P.FREE)
        self.assertEqual(P.classify("click", element_text="Show more")[0], P.FREE)

    def test_typing_is_free_even_into_a_textarea(self):
        """Typing is not yet saying. It arms the page; it does not fire it."""
        self.assertEqual(P.classify("type", field_kind="textarea")[0], P.FREE)
        self.assertEqual(P.classify("type", field_kind="text")[0], P.FREE)

    def test_choosing_from_a_dropdown_is_free(self):
        self.assertEqual(P.classify("select")[0], P.FREE)


class TestTheStructuralSignals(unittest.TestCase):
    """These need no interpretation and no word list."""

    def test_a_form_submission_always_needs_approval(self):
        v, why = P.classify("submit")
        self.assertEqual(v, P.NEEDS_APPROVAL)
        self.assertIn("form", why)

    def test_a_press_inside_a_form_needs_approval_whatever_it_says(self):
        # "Next" inside a form is how a multi-step checkout sends step one.
        v, _ = P.classify("click", element_text="Next", in_form=True)
        self.assertEqual(v, P.NEEDS_APPROVAL)

    def test_a_press_when_text_is_composed_needs_approval_whatever_it_says(self):
        # The button that sends a comment can be labelled anything.
        v, why = P.classify("click", element_text="Go", page_has_composed_text=True)
        self.assertEqual(v, P.NEEDS_APPROVAL)
        self.assertIn("composed", why)

    def test_a_key_press_fails_closed(self):
        """Enter in a comment box posts it, and the caller cannot say which box."""
        self.assertEqual(P.classify("press", element_text="search")[0], P.NEEDS_APPROVAL)


class TestTheWordListIsOnlyAnExtraTrigger(unittest.TestCase):

    def test_an_honest_post_button_is_caught_by_its_label(self):
        for text in ("Post comment", "Reply", "Send", "Publish", "Tweet", "Buy now",
                     "Place order", "Sign up", "Subscribe", "Delete account"):
            self.assertEqual(P.classify("click", element_text=text)[0],
                             P.NEEDS_APPROVAL, text)

    def test_a_dishonest_button_is_still_caught_by_structure(self):
        """The case the word list cannot see, and the reason it is not the gate."""
        v, _ = P.classify("click", element_text="Continue reading", in_form=True)
        self.assertEqual(v, P.NEEDS_APPROVAL)

    def test_case_and_spacing_do_not_hide_a_word(self):
        self.assertEqual(P.classify("click", element_text="  POST  ")[0], P.NEEDS_APPROVAL)


class TestTheDescription(unittest.TestCase):

    def test_it_tells_the_model_nothing_lifts_the_rule(self):
        said = P.describe()
        self.assertIn("nothing lifts it", said)
        self.assertIn("Paul", said)


if __name__ == "__main__":
    unittest.main()
