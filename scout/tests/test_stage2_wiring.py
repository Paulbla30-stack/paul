"""Stage 2, finally connected to something.

Everything below the daily run was built and tested in September and wired to
nothing: drafting.py, voice.py, proposals.py, approve_app.py and sender.py all
existed, and `grep -rn "Proposal.new" src/` returned nothing outside the
tests. The run read, scored, chained and emailed. It never drafted.

These tests are about the join: which items get drafted, how many, what stops
the same paper being proposed twice, and the rule that nothing here sends
anything.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import app as _app  # noqa: E402
from proposals import DEFAULT_TENANT, Proposal  # noqa: E402

NOW = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)


def rec(score, title="A paper about agent oversight", source="arxiv", **kw):
    base = {"source": source, "external_id": f"id-{title[:12]}-{score}",
            "url": f"https://example.invalid/{abs(hash(title)) % 9999}",
            "title": title, "score": score, "chain_seq": 7,
            "body": "Some body text."}
    base.update(kw)
    return base


class _Drafter:
    """Counts calls, because each one is real money."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def draft(self, item):
        self.calls.append(item)
        if self.outcomes:
            return self.outcomes.pop(0)
        return {"worth_posting": True, "draft": "A considered reply.",
                "rationale": "on topic", "disclosures": [], "voice_ok": True}


class _Store:
    def __init__(self, pending=()):
        self.put_calls = []
        self._pending = list(pending)

    def pending(self, limit=50, tenant=None):
        return list(self._pending)

    def put(self, proposal):
        self.put_calls.append(proposal)


def cfg(**drafting):
    base = {"max_drafts_per_run": 3, "post_threshold": 6.0,
            "network": "moltbook", "purpose": "news"}
    base.update(drafting)
    return {"run": {}, "drafting": base}


class TestWhichItemsGetDrafted(unittest.TestCase):

    def test_only_what_clears_the_posting_bar(self):
        drafter, store = _Drafter(), _Store()
        stats = _app.propose(cfg(), [rec(9.3, "High"), rec(5.9, "Just under"),
                                     rec(2.0, "Low")],
                             drafter=drafter, store=store, now=NOW)
        self.assertEqual(stats["considered"], 1)
        self.assertEqual([c["title"] for c in drafter.calls], ["High"])

    def test_the_posting_bar_is_higher_than_the_digest_bar(self):
        # Worth reading and worth saying something about in public are
        # different questions. If these ever collapse to one number the
        # distinction has quietly been dropped.
        import tomllib
        here = os.path.dirname(__file__)
        with open(os.path.join(here, "..", "config.toml"), "rb") as fh:
            live = tomllib.load(fh)
        self.assertGreater(live["drafting"]["post_threshold"],
                           live["run"]["digest_threshold"])

    def test_the_highest_scoring_go_first(self):
        drafter, store = _Drafter(), _Store()
        _app.propose(cfg(max_drafts_per_run=2),
                     [rec(6.5, "Middle"), rec(9.0, "Best"), rec(7.0, "Second")],
                     drafter=drafter, store=store, now=NOW)
        self.assertEqual([c["title"] for c in drafter.calls], ["Best", "Second"])

    def test_the_cap_is_a_cap_on_model_calls_not_on_proposals(self):
        # A declined draft still cost a call. Counting only what was filed
        # would let a bad day run the cap several times over.
        drafter = _Drafter({"worth_posting": False, "reason": "thin"},
                           {"worth_posting": False, "reason": "thin"},
                           {"worth_posting": False, "reason": "thin"})
        store = _Store()
        stats = _app.propose(cfg(max_drafts_per_run=2),
                             [rec(9.0, "A"), rec(8.0, "B"), rec(7.0, "C")],
                             drafter=drafter, store=store, now=NOW)
        self.assertEqual(len(drafter.calls), 2)
        self.assertEqual(stats["drafted"], 2)
        self.assertEqual(stats["proposed"], 0)

    def test_zero_stops_drafting_without_unwiring_anything(self):
        drafter, store = _Drafter(), _Store()
        stats = _app.propose(cfg(max_drafts_per_run=0), [rec(9.0)],
                             drafter=drafter, store=store, now=NOW)
        self.assertEqual(drafter.calls, [])
        self.assertIn("skipped", stats)


class TestTheSamePaperIsNotProposedTwice(unittest.TestCase):
    """The live table already holds one paper twice — arXiv revisions arrive
    as separate ids with the same title. Drafting it again is two model calls
    and two near-identical posts for one piece of work."""

    def test_a_duplicate_title_in_one_batch_is_drafted_once(self):
        drafter, store = _Drafter(), _Store()
        stats = _app.propose(
            cfg(), [rec(9.0, "Designing Against Deskilling"),
                    rec(8.9, "Designing against deskilling!")],
            drafter=drafter, store=store, now=NOW)
        self.assertEqual(len(drafter.calls), 1)
        self.assertEqual(stats["skipped_duplicate"], 1)

    def test_something_already_waiting_is_not_offered_again(self):
        waiting = Proposal.new(kind="post", network="moltbook",
                               target_url="https://example.invalid/1",
                               target_title="Designing Against Deskilling",
                               draft="d", rationale="r", discloses=[], now=NOW)
        drafter, store = _Drafter(), _Store(pending=[waiting])
        stats = _app.propose(cfg(), [rec(9.0, "Designing against deskilling")],
                             drafter=drafter, store=store, now=NOW)
        self.assertEqual(drafter.calls, [])
        self.assertEqual(stats["skipped_duplicate"], 1)

    def test_an_unreadable_pending_list_does_not_stop_the_run(self):
        class _Broken(_Store):
            def pending(self, limit=50, tenant=None):
                raise RuntimeError("dynamo is having a day")

        drafter, store = _Drafter(), _Broken()
        stats = _app.propose(cfg(), [rec(9.0)], drafter=drafter, store=store, now=NOW)
        self.assertEqual(stats["proposed"], 1)


class TestWhatIsFiled(unittest.TestCase):

    def test_a_good_draft_becomes_a_pending_proposal(self):
        drafter, store = _Drafter(), _Store()
        stats = _app.propose(cfg(), [rec(9.0, "A paper")],
                             drafter=drafter, store=store, now=NOW)
        self.assertEqual(stats["proposed"], 1)
        p = store.put_calls[0]
        self.assertEqual((p.status, p.network, p.purpose, p.tenant),
                         ("pending", "moltbook", "news", DEFAULT_TENANT))
        self.assertEqual(p.target_title, "A paper")
        self.assertEqual(p.draft, "A considered reply.")

    def test_the_window_comes_from_the_purpose(self):
        drafter, store = _Drafter(), _Store()
        _app.propose(cfg(purpose="evergreen"), [rec(9.0)],
                     drafter=drafter, store=store, now=NOW)
        p = store.put_calls[0]
        window = (datetime.fromisoformat(p.expires_at)
                  - datetime.fromisoformat(p.created_at))
        self.assertEqual(window, timedelta(days=30))

    def test_a_declined_draft_is_recorded_with_its_reason(self):
        # Dropped silently, a pipeline that has narrowed to nothing looks
        # exactly like a quiet week.
        drafter = _Drafter({"worth_posting": False, "reason": "no argument to add"})
        store = _Store()
        stats = _app.propose(cfg(), [rec(9.0, "A paper")],
                             drafter=drafter, store=store, now=NOW)
        self.assertEqual(store.put_calls, [])
        self.assertEqual(stats["declined"][0]["reason"], "no argument to add")
        self.assertEqual(stats["declined"][0]["title"], "A paper")

    def test_a_drafter_that_throws_costs_one_item_not_the_run(self):
        class _Boom(_Drafter):
            def draft(self, item):
                self.calls.append(item)
                raise RuntimeError("model unavailable")

        drafter, store = _Boom(), _Store()
        stats = _app.propose(cfg(), [rec(9.0, "A"), rec(8.0, "B")],
                             drafter=drafter, store=store, now=NOW)
        self.assertEqual(len(stats["declined"]), 2)
        self.assertEqual(stats["proposed"], 0)

    def test_a_dry_run_drafts_but_files_nothing(self):
        drafter, store = _Drafter(), _Store()
        stats = _app.propose(cfg(), [rec(9.0)], drafter=drafter, store=store,
                             now=NOW, dry_run=True)
        self.assertEqual(stats["proposed"], 1)
        self.assertEqual(store.put_calls, [], "a dry run wrote to the table")

    def test_the_source_item_carries_the_chain_sequence(self):
        # So a proposal can be traced back to the chain entry for the item it
        # came from, a year later.
        drafter, store = _Drafter(), _Store()
        _app.propose(cfg(), [rec(9.0)], drafter=drafter, store=store, now=NOW)
        self.assertEqual(store.put_calls[0].source_item["chain_seq"], 7)


class TestNothingHereSends(unittest.TestCase):

    def test_every_proposal_starts_pending(self):
        drafter, store = _Drafter(), _Store()
        _app.propose(cfg(), [rec(9.0, "A"), rec(8.0, "B")],
                     drafter=drafter, store=store, now=NOW)
        self.assertTrue(all(p.status == "pending" for p in store.put_calls))

    def test_it_does_nothing_at_all_without_a_drafter_or_a_store(self):
        self.assertIn("skipped", _app.propose(cfg(), [rec(9.0)],
                                              drafter=None, store=_Store()))
        self.assertIn("skipped", _app.propose(cfg(), [rec(9.0)],
                                              drafter=_Drafter(), store=None))


class TestTheRoleCanActuallyDoIt(unittest.TestCase):
    """Two things the code cannot do on its own, both found by reading the
    live role rather than the template: the sender has to be in the SES
    condition, and drafting needs a bedrock grant the role did not have."""

    def setUp(self):
        here = os.path.dirname(__file__)
        with open(os.path.join(here, "..", "template.yaml")) as fh:
            self.template = fh.read()
        import tomllib
        with open(os.path.join(here, "..", "config.toml"), "rb") as fh:
            self.cfg = tomllib.load(fh)

    def test_the_configured_sender_is_permitted_by_the_role(self):
        # Changing `sender` in config alone is not enough: the Lambda sends as
        # its role, and an address missing here is AccessDenied on the first
        # day something clears the threshold.
        self.assertIn(self.cfg["email"]["sender"], self.template)

    def test_the_fallback_sender_is_permitted_too(self):
        self.assertIn(self.cfg["email"]["fallback_sender"], self.template)

    def test_the_drafting_model_is_granted_to_the_scout_role(self):
        model = self.cfg["drafting"].get("converse_model", "")
        self.assertTrue(model, "no converse_model configured")
        scout_role = self.template.split("ScoutRole:")[1].split("ApproveFunction:")[0]
        self.assertIn("bedrock:InvokeModel", scout_role)
        self.assertIn(model, scout_role)


if __name__ == "__main__":
    unittest.main()
