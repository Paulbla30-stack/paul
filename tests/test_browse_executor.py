"""The browse handlers: what the agent gets back, and what it does not.

The one that matters most is the last class. Page text reaches the model
through the envelope and through nothing else -- it is not written into
durable memory, because the agent's own answer when it was asked what it would
refuse included not caching page content beyond the task that needed it, and
because a store filling with the body of every page read is a store nobody can
find anything in.
"""

import logging
import unittest

from jarvis.agent.executor import TaskExecutor
from jarvis.agent.memory import AgentMemory
from jarvis.agent.planner import Task, TaskType

LOG = logging.getLogger("test.browse")
LOG.addHandler(logging.NullHandler())
NO_HW = {"display": {"enabled": False}, "input": {"enabled": False}}


def a_page(**over):
    base = {"url": "https://example.com/a", "title": "A page", "text": "Body.",
            "fetched_at": 1790000000.0, "links": [], "fields": [],
            "has_password": False, "truncated": False, "status": 200}
    base.update(over)
    return base


class FakeView:
    def __init__(self, answer=None):
        self.answer = answer if answer is not None else a_page()
        self.calls = []

    def open(self, url):
        self.calls.append(("open", url))
        return self.answer

    def follow(self, ref):
        self.calls.append(("follow", ref))
        return self.answer

    def read(self):
        self.calls.append(("read",))
        return self.answer

    def act(self, kind, ref="", text=""):
        self.calls.append(("act", kind, ref, text))
        return self.answer


def executor(view=None):
    ex = TaskExecutor(dict(NO_HW), AgentMemory(), LOG)
    ex.browser = view
    return ex


def task(task_type, **meta):
    return Task(priority=5, description="browse",
                task_type=task_type, metadata=meta)


class TestItSaysSoWhenTheBrowserIsOff(unittest.TestCase):
    """Off is the default, so this is the first thing anyone will hit."""

    def test_open_says_it_is_not_switched_on(self):
        got = executor(None).execute(task(TaskType.BROWSE_OPEN,
                                          url="https://example.com/"))
        self.assertFalse(got["success"])
        self.assertIn("not switched on", got["error"])

    def test_every_browse_handler_says_the_same_thing(self):
        for which, meta in ((TaskType.BROWSE_OPEN, {"url": "https://e.test/"}),
                            (TaskType.BROWSE_FOLLOW, {"ref": "L1"}),
                            (TaskType.BROWSE_READ, {}),
                            (TaskType.BROWSE_ACT, {"kind": "click", "ref": "L1"})):
            got = executor(None).execute(task(which, **meta))
            self.assertFalse(got["success"], which)
            self.assertIn("browser", got["error"], which)


class TestOpening(unittest.TestCase):

    def test_it_opens_the_address_it_was_given(self):
        view = FakeView()
        executor(view).execute(task(TaskType.BROWSE_OPEN, url="https://example.com/a"))
        self.assertEqual(view.calls, [("open", "https://example.com/a")])

    def test_a_bare_host_is_assumed_to_be_https(self):
        view = FakeView()
        executor(view).execute(task(TaskType.BROWSE_OPEN, url="example.com"))
        self.assertEqual(view.calls, [("open", "https://example.com")])

    def test_it_never_downgrades_to_http(self):
        """Silently choosing http is how a page gets read over a wire anyone
        on the path can rewrite."""
        view = FakeView()
        executor(view).execute(task(TaskType.BROWSE_OPEN, url="example.com"))
        self.assertNotIn("http://", view.calls[0][1])

    def test_an_explicit_http_url_is_left_alone(self):
        view = FakeView()
        executor(view).execute(task(TaskType.BROWSE_OPEN, url="http://example.com/"))
        self.assertEqual(view.calls[0][1], "http://example.com/")

    def test_no_url_is_an_error_rather_than_a_blank_page(self):
        got = executor(FakeView()).execute(task(TaskType.BROWSE_OPEN))
        self.assertFalse(got["success"])

    def test_the_result_carries_the_address_and_the_citation(self):
        got = executor(FakeView()).execute(task(TaskType.BROWSE_OPEN,
                                                url="https://example.com/a"))
        self.assertEqual(got["output"]["url"], "https://example.com/a")
        self.assertIn("https://example.com/a", got["output"]["source"])


class TestFollowingAndReading(unittest.TestCase):

    def test_follow_passes_the_ref_through(self):
        view = FakeView()
        executor(view).execute(task(TaskType.BROWSE_FOLLOW, ref="L3"))
        self.assertEqual(view.calls, [("follow", "L3")])

    def test_follow_without_a_ref_explains_what_a_ref_is(self):
        got = executor(FakeView()).execute(task(TaskType.BROWSE_FOLLOW))
        self.assertFalse(got["success"])
        self.assertIn("L3", got["error"])

    def test_read_does_not_move(self):
        view = FakeView()
        executor(view).execute(task(TaskType.BROWSE_READ))
        self.assertEqual(view.calls, [("read",)])


class TestActing(unittest.TestCase):

    def test_a_click_passes_through(self):
        view = FakeView()
        executor(view).execute(task(TaskType.BROWSE_ACT, kind="click", ref="L1"))
        self.assertEqual(view.calls, [("act", "click", "L1", "")])

    def test_typing_carries_the_text(self):
        view = FakeView()
        executor(view).execute(task(TaskType.BROWSE_ACT, kind="type",
                                    ref="F2", text="weather"))
        self.assertEqual(view.calls, [("act", "type", "F2", "weather")])

    def test_an_unknown_kind_is_refused_before_it_reaches_the_browser(self):
        view = FakeView()
        got = executor(view).execute(task(TaskType.BROWSE_ACT, kind="download",
                                          ref="L1"))
        self.assertFalse(got["success"])
        self.assertEqual(view.calls, [])

    def test_a_click_with_no_ref_is_refused_before_it_reaches_the_browser(self):
        view = FakeView()
        got = executor(view).execute(task(TaskType.BROWSE_ACT, kind="click"))
        self.assertFalse(got["success"])
        self.assertEqual(view.calls, [])

    def test_a_refusal_from_the_browser_comes_back_with_its_reason(self):
        view = FakeView({"error": "that field is a secret",
                         "reason": "refused by the browser"})
        got = executor(view).execute(task(TaskType.BROWSE_ACT, kind="type",
                                          ref="F1", text="x"))
        self.assertFalse(got["success"])
        self.assertIn("secret", got["error"])


class TestWhatTheModelIsHanded(unittest.TestCase):

    def result(self, **over):
        return executor(FakeView(a_page(**over))).execute(
            task(TaskType.BROWSE_OPEN, url="https://example.com/a"))["output"]

    def test_the_page_arrives_inside_the_envelope(self):
        out = self.result(text="the body")
        self.assertIn("UNTRUSTED PAGE CONTENT", out["page"])
        self.assertIn("the body", out["page"])

    def test_the_envelope_is_the_only_field_carrying_the_body(self):
        out = self.result(text="a distinctive sentence")
        carrying = [k for k, v in out.items()
                    if isinstance(v, str) and "a distinctive sentence" in v]
        self.assertEqual(carrying, ["page"])

    def test_a_password_page_is_flagged_on_the_result_too(self):
        self.assertTrue(self.result(has_password=True)["has_password"])

    def test_truncation_is_reported_on_the_result(self):
        self.assertTrue(self.result(truncated=True)["truncated"])


class TestThePageBodyIsNotPutInMemory(unittest.TestCase):
    """Its own refusal: do not cache page content longer than the task needs.

    What the agent *concludes* from a page is worth keeping, with its source.
    The page body is working text.
    """

    def test_reading_a_page_stores_nothing(self):
        ex = executor(FakeView(a_page(text="something the page said")))
        before = len(ex.memory)
        ex.execute(task(TaskType.BROWSE_OPEN, url="https://example.com/a"))
        self.assertEqual(len(ex.memory), before)

    def test_no_browse_handler_writes_to_the_store(self):
        """Read the code, so a later edit that adds a store call trips this."""
        import ast
        import inspect
        import textwrap
        for name in ("_handle_browse_open", "_handle_browse_follow",
                     "_handle_browse_read", "_handle_browse_act",
                     "_page_result"):
            src = textwrap.dedent(inspect.getsource(getattr(TaskExecutor, name)))
            called = {n.func.attr for n in ast.walk(ast.parse(src))
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
            self.assertNotIn("store", called, name)
            self.assertNotIn("remember", called, name)


if __name__ == "__main__":
    unittest.main()
