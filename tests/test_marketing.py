"""The marketing area: what the scout drafted, and what happened to it.

Paul asked for this on 23 September, the design was written the same day, and
a week later he opened the UI on his phone and the tab was not there. The
design had been written up as though writing it were the work.

The tests are mostly about two claims. That the page cannot approve anything
-- approving means the signing secret, and that living on an internet-facing
box changes what the box can do. And that the drafts closest to lapsing come
first, because a two-day reply window plus a human who was away for three is
how an approval gate quietly becomes a filter that only passes the things
nobody was in a hurry about.
"""

import logging
import time
import unittest

from jarvis.agent import marketing

LOG = logging.getLogger("test")
NOW = 1_800_000_000.0


def proposal(status="pending", hours_left=48.0, title="A paper", **kw):
    import datetime as dt
    created = dt.datetime.fromtimestamp(NOW - 3600, dt.timezone.utc)
    expires = dt.datetime.fromtimestamp(NOW + hours_left * 3600, dt.timezone.utc)
    row = {"sk": "PROPOSAL", "id": kw.get("id", f"p-{title}-{hours_left}"),
           "status": status, "network": "moltbook", "purpose": "news",
           "tenant": "paul", "kind": "post", "target_title": title,
           "target_url": "https://example.invalid/1",
           "draft": "A considered reply that is long enough to matter. " * 3,
           "rationale": "same argument, instrumented",
           "discloses": ["authorship"],
           "created_at": created.isoformat(), "expires_at": expires.isoformat()}
    row.update(kw)
    return row


class _Table:
    name = "jarvis-scout"

    def __init__(self, items, fail=None):
        self.items = items
        self.fail = fail
        self.calls = []

    def scan(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise self.fail
        return {"Items": self.items}


class _Resource:
    def __init__(self, table):
        self._table = table

    def Table(self, name):
        return self._table


def view(items, fail=None, clock=lambda: NOW):
    table = _Table(items, fail)
    got = marketing.ScoutView(logger=LOG, resource=_Resource(table), clock=clock)
    return got, table


class TestItCannotApprove(unittest.TestCase):
    """Approving means the HMAC secret the approve app mints tokens with.
    Putting that on the instance changes it from 'show Paul the drafts' to
    'approve them', which is a decision about the gate."""

    def test_the_module_has_no_write_method_at_all(self):
        names = [n for n in dir(marketing.ScoutView) if not n.startswith("_")]
        for forbidden in ("put", "put_item", "update", "decide", "approve",
                          "reject", "send", "delete"):
            self.assertNotIn(forbidden, names)

    def test_it_never_calls_a_write_operation(self):
        """Checked on the code, not the prose.

        The first version of this searched the file text and failed on the
        docstring, which names those operations in order to say they are
        absent. A test that cannot tell an explanation from an instruction
        would have to be silenced, and a silenced test is worse than none.
        """
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(marketing))
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Attribute):
                    called.add(fn.attr)
                elif isinstance(fn, ast.Name):
                    called.add(fn.id)
        for op in ("put_item", "update_item", "delete_item", "batch_writer",
                   "transact_write_items", "put", "update", "delete"):
            self.assertNotIn(op, called, f"marketing.py calls {op}()")

    def test_it_only_ever_scans(self):
        got, table = view([proposal()])
        got.state()
        self.assertTrue(table.calls, "it did not read at all")


class TestWhatLapsesSoonestComesFirst(unittest.TestCase):

    def test_pending_is_sorted_by_time_remaining(self):
        got, _ = view([proposal(hours_left=72, title="Roomy"),
                       proposal(hours_left=3, title="Nearly gone"),
                       proposal(hours_left=30, title="Middle")])
        state = got.state()
        self.assertEqual([p["title"] for p in state["pending"]],
                         ["Nearly gone", "Middle", "Roomy"])

    def test_the_ones_about_to_lapse_are_counted(self):
        got, _ = view([proposal(hours_left=3), proposal(hours_left=10),
                       proposal(hours_left=72)])
        self.assertEqual(got.state()["expiring_soon"], 2)

    def test_time_left_is_computed_not_guessed(self):
        got, _ = view([proposal(hours_left=2)])
        self.assertEqual(got.state()["pending"][0]["left_s"], 7200)

    def test_a_proposal_with_no_window_sorts_last_rather_than_crashing(self):
        got, _ = view([proposal(hours_left=5, title="Has one"),
                       proposal(title="No window", expires_at="")])
        order = [p["title"] for p in got.state()["pending"]]
        self.assertEqual(order, ["Has one", "No window"])

    def test_an_unparseable_window_is_not_an_exception(self):
        got, _ = view([proposal(expires_at="the day after tomorrow")])
        self.assertIsNone(got.state()["pending"][0]["left_s"])


class TestWhatThePageIsGiven(unittest.TestCase):

    def test_the_draft_is_not_truncated_for_display(self):
        # A short draft on the page and a full one on the network would mean
        # approving something nobody read.
        body = "word " * 300
        got, _ = view([proposal(draft=body)])
        self.assertEqual(got.state()["pending"][0]["draft"], body[:marketing.MAX_DRAFT_CHARS])

    def test_decided_proposals_are_separated_from_waiting_ones(self):
        got, _ = view([proposal(status="pending", title="Waiting"),
                       proposal(status="sent", title="Gone out"),
                       proposal(status="rejected", title="Turned down")])
        state = got.state()
        self.assertEqual([p["title"] for p in state["pending"]], ["Waiting"])
        self.assertEqual({p["title"] for p in state["recent"]},
                         {"Gone out", "Turned down"})

    def test_the_counts_cover_every_status(self):
        got, _ = view([proposal(status="pending"), proposal(status="pending"),
                       proposal(status="sent")])
        self.assertEqual(got.state()["counts"], {"pending": 2, "sent": 1})

    def test_the_rationale_and_disclosures_reach_the_page(self):
        got, _ = view([proposal()])
        row = got.state()["pending"][0]
        self.assertEqual(row["rationale"], "same argument, instrumented")
        self.assertEqual(row["discloses"], ["authorship"])


class TestItDegradesRatherThanFalling(unittest.TestCase):
    """The scout is a separate system behind its own account boundary. A
    marketing panel that raises takes the chat panel with it."""

    def test_an_unreachable_table_reports_why(self):
        got, _ = view([], fail=RuntimeError("AccessDeniedException"))
        state = got.state()
        self.assertFalse(state["reachable"])
        self.assertIn("AccessDenied", state["reason"])
        self.assertEqual(state["pending"], [])

    def test_the_summary_survives_it_too(self):
        got, _ = view([], fail=RuntimeError("no"))
        self.assertEqual(got.summary()["pending"], 0)


class TestItIsOffUnlessAskedFor(unittest.TestCase):

    def test_no_config_means_no_view(self):
        for cfg in ({}, {"marketing": {}}, {"marketing": {"enabled": False}},
                    {"marketing": "yes please"}):
            self.assertIsNone(marketing.build_view(cfg, LOG), cfg)

    def test_enabling_it_builds_one(self):
        got = marketing.build_view(
            {"marketing": {"enabled": True, "table": "t", "region": "eu-west-2"}}, LOG)
        self.assertEqual((got.table_name, got.region), ("t", "eu-west-2"))


class TestItIsReachableFromTheUi(unittest.TestCase):
    """A capability nobody can get at is the half-built shape the tool
    register exists to stop -- and this one was noticed by its absence."""

    def setUp(self):
        from jarvis.cloud.headless import UI_HTML_PATH
        with open(UI_HTML_PATH, encoding="utf-8") as fh:
            self.page = fh.read()

    def test_the_tab_exists(self):
        self.assertIn('data-tab="marketing"', self.page)
        self.assertIn('id="marketing"', self.page)
        self.assertIn("loadMarketing", self.page)

    def test_it_calls_the_endpoint(self):
        self.assertIn('api("/marketing")', self.page)

    def test_the_page_has_no_approve_control(self):
        panel = self.page.split('<section id="marketing">')[1].split("</section>")[0]
        for control in ("/marketing/decide", "/proposals/decide", "Approve",
                        "approveProposal"):
            self.assertNotIn(control, panel, control)

    def test_the_endpoint_is_declared_and_read_only(self):
        import inspect
        from jarvis.cloud import headless
        source = inspect.getsource(headless)
        self.assertIn('"/marketing"', source)
        self.assertNotIn('"/marketing/decide"', source)


if __name__ == "__main__":
    unittest.main()
