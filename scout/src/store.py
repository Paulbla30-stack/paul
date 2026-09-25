"""Hit storage and de-duplication.

A hit is stored once, keyed by source + the source's own id. Re-seeing an
item on a later run updates nothing and does not re-enter it in the chain:
the chain records when the scout FIRST saw something, and that fact should
not change afterwards.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone


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
