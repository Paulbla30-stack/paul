"""Whose behalf a proposal is made on.

Paul is the first tenant and for now the only one; others are intended. The
field is on the record before a single proposal exists because it cannot be
added later: every transition is appended to a hash chain, and an entry
already written cannot gain a field. Backfilling would mean rewriting the
chain, which defeats it, or leaving a permanent population of proposals that
belong to nobody.

The failure this guards against is one operator seeing, or approving,
another's drafts.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from proposals import DEFAULT_TENANT, Proposal  # noqa: E402


def make(**kw):
    base = dict(kind="post", network="moltbook", target_url="https://x/1",
                target_title="t", draft="d", rationale="r", discloses=[])
    base.update(kw)
    return Proposal.new(**base)


class TenantOnTheRecordTest(unittest.TestCase):
    def test_a_proposal_carries_a_tenant(self):
        self.assertTrue(make().tenant)

    def test_an_unattributed_proposal_defaults_to_the_operator(self):
        # Never nobody. A caller that forgets is attributed to the operator,
        # because an ownerless proposal is the one shape this must not hold.
        for missing in (None, "", "   "):
            self.assertEqual(make(tenant=missing).tenant, DEFAULT_TENANT)

    def test_a_tenant_is_normalised(self):
        for spelling in ("Acme", "ACME", "  acme  "):
            self.assertEqual(make(tenant=spelling).tenant, "acme")

    def test_two_tenants_stay_distinct(self):
        self.assertNotEqual(make(tenant="acme").tenant, make(tenant="beta").tenant)

    def test_the_tenant_survives_a_round_trip(self):
        # Proposals are reconstructed from DynamoDB attributes, so the field
        # has to survive asdict() and Proposal(**fields).
        from dataclasses import asdict
        p = make(tenant="acme")
        again = Proposal(**asdict(p))
        self.assertEqual(again.tenant, "acme")

    def test_tenant_does_not_disturb_the_rest_of_the_record(self):
        p = make(tenant="acme")
        self.assertEqual(p.status, "pending")
        self.assertEqual(p.network, "moltbook")
        self.assertTrue(p.expires_at > p.created_at)


class PendingFilterTest(unittest.TestCase):
    """pending(tenant=...) must actually narrow, not just accept the argument."""

    class _Table:
        def __init__(self, items):
            self.items = items
            self.seen = None

        def scan(self, **kwargs):
            self.seen = kwargs.get("FilterExpression")
            return {"Items": self.items}

    def _store(self, items):
        from proposals import ProposalStore
        s = ProposalStore.__new__(ProposalStore)
        s.table = self._Table(items)
        return s

    def _row(self, tenant):
        from dataclasses import asdict
        d = asdict(make(tenant=tenant))
        d.update(pk=f"PROPOSAL#{d['id']}", sk="PROPOSAL")
        return d

    @staticmethod
    def _attrs(condition) -> set:
        """Every attribute name a boto3 condition tree touches.

        get_expression() only renders the top level; the operands of an AND
        are condition objects in their own right, so a string check on the
        top level silently passes whatever the nested conditions say.
        """
        from boto3.dynamodb.conditions import ConditionBase
        names = set()
        stack = [condition]
        while stack:
            node = stack.pop()
            if isinstance(node, ConditionBase):
                stack.extend(node.get_expression()["values"])
            elif hasattr(node, "name"):
                names.add(node.name)
        return names

    def test_a_tenant_filter_reaches_the_query(self):
        store = self._store([self._row("acme")])
        store.pending(tenant="acme")
        self.assertIn("tenant", self._attrs(store.table.seen))

    def test_no_tenant_means_every_tenant(self):
        store = self._store([self._row("acme"), self._row("beta")])
        store.pending()
        self.assertNotIn("tenant", self._attrs(store.table.seen))

    def test_the_filter_still_narrows_to_pending(self):
        # The tenant filter must be added to the status filter, not replace it.
        store = self._store([self._row("acme")])
        store.pending(tenant="acme")
        self.assertEqual({"sk", "status", "tenant"}, self._attrs(store.table.seen))


if __name__ == "__main__":
    unittest.main()
