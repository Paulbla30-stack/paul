"""Hit storage and de-duplication.

A hit is stored once, keyed by source + the source's own id. Re-seeing an
item on a later run updates nothing and does not re-enter it in the chain:
the chain records when the scout FIRST saw something, and that fact should
not change afterwards.

The same store also keeps one digest record: the last digest built, and
whether it was sent. Hits are marked seen before the email goes, so a digest
that fails to send exists nowhere else; the next run reads it back and sends
it. In DynamoDB it sits at pk="DIGEST#LATEST", sk="DIGEST", clear of hits
(sk "HIT"), proposals (sk "PROPOSAL") and the chain (pk "CHAIN"). It is
overwritten with PutItem, because the scout has neither UpdateItem nor
DeleteItem.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

DIGEST_KEY = {"pk": "DIGEST#LATEST", "sk": "DIGEST"}
PENDING, SENT = "pending", "sent"


def _digest_record(status: str, items: list[dict]) -> dict:
    if status not in (PENDING, SENT):
        raise ValueError(f"unknown digest status {status!r}")
    # The body is dropped: the digest does not print it, and 25 abstracts
    # would push a DynamoDB item toward its 400 KB limit for nothing.
    slim = [{k: v for k, v in r.items() if k != "body"} for r in items]
    return {"status": status, "count": len(slim),
            "updated": datetime.now(timezone.utc).isoformat(),
            # A JSON string, not a map, for the reason chain.py gives: DynamoDB
            # has no float, and scores are floats.
            "items_json": json.dumps(slim, ensure_ascii=False, sort_keys=True)}


def _pending_items(rec: dict | None) -> list[dict] | None:
    if not rec or rec.get("status") != PENDING:
        return None
    return list(json.loads(rec.get("items_json") or "[]"))


class LocalHitStore:
    def __init__(self, path: str):
        self.path = path
        try:
            with open(path, encoding="utf-8") as fh:
                self._d = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            self._d = {}

    def seen(self, key: str) -> bool:
        return key in self._d

    def put(self, key: str, record: dict) -> None:
        self._d[key] = record
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self._d, fh, indent=1, ensure_ascii=False)

    def count(self) -> int:
        return len(self._d)

    @property
    def _digest_path(self) -> str:
        return self.path + ".digest.json"

    def pending_digest(self) -> list[dict] | None:
        """The items of a digest that was built and never sent, or None.

        A corrupt file raises rather than reading as "nothing pending".
        """
        try:
            with open(self._digest_path, encoding="utf-8") as fh:
                rec = json.load(fh)
        except FileNotFoundError:
            return None
        return _pending_items(rec)

    def put_digest(self, status: str, items: list[dict]) -> None:
        rec = _digest_record(status, items)
        tmp = self._digest_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, indent=1, ensure_ascii=False)
        os.replace(tmp, self._digest_path)


class DynamoHitStore:
    def __init__(self, table_name: str, dynamodb=None):
        import boto3
        self.ddb = dynamodb or boto3.resource("dynamodb")
        self.table = self.ddb.Table(table_name)

    def seen(self, key: str) -> bool:
        r = self.table.get_item(Key={"pk": key, "sk": "HIT"},
                                ProjectionExpression="pk")
        return "Item" in r

    def put(self, key: str, record: dict) -> None:
        from decimal import Decimal

        def enc(v):
            if isinstance(v, float):
                return Decimal(str(round(v, 4)))
            if isinstance(v, dict):
                return {k: enc(x) for k, x in v.items()}
            if isinstance(v, list):
                return [enc(x) for x in v]
            if isinstance(v, str) and v == "":
                return None       # DynamoDB rejects empty strings in some paths
            return v

        item = {"pk": key, "sk": "HIT", **{k: enc(v) for k, v in record.items()}}
        item = {k: v for k, v in item.items() if v is not None}
        # Never overwrite a first sighting.
        try:
            self.table.put_item(Item=item,
                                ConditionExpression="attribute_not_exists(pk)")
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            pass

    def count(self) -> int:
        return int(self.table.item_count)   # approximate; updated ~6-hourly

    def pending_digest(self) -> list[dict] | None:
        r = self.table.get_item(Key=DIGEST_KEY, ConsistentRead=True)
        return _pending_items(r.get("Item"))

    def put_digest(self, status: str, items: list[dict]) -> None:
        # PutItem overwrites: pending before the send, sent after it.
        self.table.put_item(Item={**DIGEST_KEY, **_digest_record(status, items)})


def record_from(item, score, now=None) -> dict:
    """The stored shape. This is also what goes in the chain payload."""
    now = now or datetime.now(timezone.utc)
    return {
        "source": item.source,
        "external_id": item.external_id,
        "url": item.url,
        "title": item.title,
        "author": item.author,
        # The drafter needs the substance, not just the metadata. Without
        # this it judges a paper by its title and the scoring explanation,
        # which is exactly as thin as it sounds — the first drafting check
        # declined all five items for lack of anything to engage with.
        "body": item.body,
        "published": item.published.isoformat(),
        "first_seen": now.isoformat(),
        "matched_keywords": score.matched_keywords,
        "negative_keywords": score.negatives,
        "score": score.total,
        "score_detail": {
            "keyword_subtotal": score.keyword_subtotal,
            "source_weight": score.source_weight,
            "recency_factor": score.recency_factor,
            "penalty": score.penalty,
        },
        "why": score.why(),
        "extra": item.extra,
    }
