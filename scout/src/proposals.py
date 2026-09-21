"""Proposals: things Jarvis would like to post, and never posts by itself.

The lifecycle is deliberately one-way and fully recorded:

    pending ──approve──> approved ──send──> sent
       │                                      │
       ├──reject───────> rejected             └──(failure)──> failed
       └──lapse────────> expired

Nothing moves from pending except by Paul's decision or by lapsing. There
is no path from pending to sent that does not pass through approved, and
approved is only reachable from a signed POST that Paul made.

Every transition is appended to the hash chain, so the record of what was
proposed, what was approved, by whom and when, cannot be quietly edited
afterwards. That is the part worth having: not that the gate exists, but
that its history is checkable.

A proposal that nobody decides on expires. Silence is not consent.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

PENDING, APPROVED, REJECTED, EXPIRED, SENT, FAILED = (
    "pending", "approved", "rejected", "expired", "sent", "failed")

TERMINAL = (REJECTED, EXPIRED, SENT, FAILED)
DEFAULT_TTL_DAYS = 7


@dataclass
class Proposal:
    """One thing Jarvis would post, if allowed."""
    id: str
    kind: str                 # "comment" | "post"
    network: str              # "moltbook" | …
    target_url: str           # what it replies to, or where it would go
    target_title: str
    draft: str                # exactly what would be posted, verbatim
    rationale: str            # why Jarvis thinks it is worth posting
    discloses: list[str]      # the disclosures the draft contains
    created_at: str
    expires_at: str
    status: str = PENDING
    decided_at: str | None = None
    decided_by: str | None = None
    source_item: dict = field(default_factory=dict)
    error: str | None = None

    @staticmethod
    def new(*, kind: str, network: str, target_url: str, target_title: str,
            draft: str, rationale: str, discloses: list[str],
            source_item: dict | None = None, now: datetime | None = None,
            ttl_days: int = DEFAULT_TTL_DAYS) -> "Proposal":
        now = now or datetime.now(timezone.utc)
        return Proposal(
            id=uuid.uuid4().hex,
            kind=kind, network=network,
            target_url=target_url, target_title=target_title,
            draft=draft, rationale=rationale, discloses=list(discloses),
            created_at=now.isoformat(),
            expires_at=(now + timedelta(days=ttl_days)).isoformat(),
            source_item=source_item or {},
        )

    @property
    def key(self) -> str:
        return f"PROPOSAL#{self.id}"

    def as_dict(self) -> dict:
        return asdict(self)


class ProposalStore:
    """DynamoDB-backed, with the decision guarded by a conditional write."""

    def __init__(self, table_name: str, dynamodb=None):
        import boto3
        self.ddb = dynamodb or boto3.resource("dynamodb")
        self.table = self.ddb.Table(table_name)

    def put(self, p: Proposal) -> None:
        self.table.put_item(
            Item={"pk": p.key, "sk": "PROPOSAL", **p.as_dict()},
            ConditionExpression="attribute_not_exists(pk)")

    def get(self, proposal_id: str) -> Proposal | None:
        r = self.table.get_item(Key={"pk": f"PROPOSAL#{proposal_id}", "sk": "PROPOSAL"},
                                ConsistentRead=True)
        it = r.get("Item")
        if not it:
            return None
        fields = {k: v for k, v in it.items() if k not in ("pk", "sk")}
        return Proposal(**fields)

    def decide(self, proposal_id: str, action: str, *, who: str,
               now: datetime | None = None) -> Proposal:
        """Move pending → approved/rejected. Fails if it is not pending.

        The condition is what makes a replayed or forwarded token harmless:
        the second attempt finds a status that is no longer pending and is
        refused by DynamoDB, not by application logic that might be wrong.
        """
        from botocore.exceptions import ClientError
        now = now or datetime.now(timezone.utc)
        new_status = APPROVED if action == "approve" else REJECTED
        try:
            r = self.table.update_item(
                Key={"pk": f"PROPOSAL#{proposal_id}", "sk": "PROPOSAL"},
                UpdateExpression=("SET #s = :new, decided_at = :t, decided_by = :w"),
                ConditionExpression="#s = :pending AND attribute_exists(pk)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":new": new_status, ":t": now.isoformat(),
                    ":w": who, ":pending": PENDING},
                ReturnValues="ALL_NEW")
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                existing = self.get(proposal_id)
                raise AlreadyDecided(existing)
            raise
        fields = {k: v for k, v in r["Attributes"].items() if k not in ("pk", "sk")}
        return Proposal(**fields)

    def pending(self, limit: int = 50) -> list[Proposal]:
        from boto3.dynamodb.conditions import Attr
        out, kwargs = [], {"FilterExpression": Attr("sk").eq("PROPOSAL")
                           & Attr("status").eq(PENDING)}
        while len(out) < limit:
            r = self.table.scan(**kwargs)
            for it in r.get("Items", []):
                fields = {k: v for k, v in it.items() if k not in ("pk", "sk")}
                out.append(Proposal(**fields))
            if "LastEvaluatedKey" not in r:
                break
            kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]
        return out[:limit]

    def lapse_overdue(self, now: datetime | None = None) -> list[Proposal]:
        """Expire anything nobody decided. Silence is not consent."""
        now = now or datetime.now(timezone.utc)
        lapsed = []
        for p in self.pending(limit=200):
            if datetime.fromisoformat(p.expires_at) <= now:
                try:
                    self.table.update_item(
                        Key={"pk": p.key, "sk": "PROPOSAL"},
                        UpdateExpression="SET #s = :e",
                        ConditionExpression="#s = :pending",
                        ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={":e": EXPIRED, ":pending": PENDING})
                    p.status = EXPIRED
                    lapsed.append(p)
                except Exception:                        # noqa: BLE001
                    pass
        return lapsed


class AlreadyDecided(Exception):
    def __init__(self, proposal: Proposal | None):
        self.proposal = proposal
        super().__init__(
            f"already {proposal.status}" if proposal else "no such proposal")
