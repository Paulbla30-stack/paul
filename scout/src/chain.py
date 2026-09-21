"""Hash-chained append-only log.

Every entry carries the sha256 of the entry before it. Changing or removing
any entry breaks every hash after it, so tampering is detectable by anyone
holding the log and the verify script — no key, no server, no trust in the
writer required.

This is deliberately the weaker cousin of the Glass Ledger in the Heartbeat
estate: hash-chained but not signed, and not witnessed off-box. It proves
the log has not been edited since it was written. It does not prove who
wrote it, and it does not stop whoever controls the table from discarding
the whole thing and starting again. For a scout that reads public APIs and
sends Paul an email, that is the right amount of machinery. Do not quote it
as an example of the Glass Ledger; it is not one.

Structure in DynamoDB, single table:

    pk="CHAIN"  sk="000000000001"   an entry
    pk="CHAIN"  sk="HEAD"           pointer to the newest entry
    pk="HIT#<source>#<id>" sk="HIT" a stored hit (deduplication key)

The entry and the head move together in one transaction, both conditional,
so two concurrent runs cannot fork the chain.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Protocol

GENESIS_PREV = "0" * 64
SEQ_WIDTH = 12


def canonical(payload: dict) -> str:
    """Stable JSON for hashing. Sorted keys, no incidental whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def entry_hash(seq: int, prev_hash: str, ts: str, payload: dict) -> str:
    body = canonical({"seq": seq, "prev_hash": prev_hash, "ts": ts, "payload": payload})
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


class ChainStore(Protocol):
    def head(self) -> tuple[int, str]: ...
    def append_entry(self, seq: int, entry: dict, prev_entry_hash: str) -> None: ...
    def entries(self): ...


class Chain:
    def __init__(self, store: ChainStore):
        self.store = store

    def append(self, payload: dict, *, ts: str | None = None) -> dict:
        """Append one entry. Returns the entry as written."""
        ts = ts or datetime.now(timezone.utc).isoformat()
        seq_prev, prev = self.store.head()
        seq = seq_prev + 1
        h = entry_hash(seq, prev, ts, payload)
        entry = {
            "seq": seq,
            "ts": ts,
            "prev_hash": prev,
            "entry_hash": h,
            "payload_sha256": payload_hash(payload),
            "payload": payload,
        }
        self.store.append_entry(seq, entry, prev)
        return entry

    def verify(self) -> dict:
        """Walk the chain and recompute every hash.

        Returns a result dict rather than raising, so the verify script can
        report exactly where a chain first goes wrong instead of just failing.
        """
        prev = GENESIS_PREV
        expected_seq = 1
        count = 0
        for e in self.store.entries():
            if e["seq"] != expected_seq:
                return _bad(e["seq"], "sequence gap",
                            f"expected seq {expected_seq}, found {e['seq']}", count)
            if e["prev_hash"] != prev:
                return _bad(e["seq"], "broken link",
                            f"prev_hash {e['prev_hash'][:16]}… does not match the "
                            f"previous entry's hash {prev[:16]}…", count)
            recomputed = entry_hash(e["seq"], e["prev_hash"], e["ts"], e["payload"])
            if recomputed != e["entry_hash"]:
                return _bad(e["seq"], "entry altered",
                            f"stored hash {e['entry_hash'][:16]}… but the content "
                            f"hashes to {recomputed[:16]}…", count)
            if payload_hash(e["payload"]) != e.get("payload_sha256"):
                return _bad(e["seq"], "payload altered",
                            "payload_sha256 does not match the stored payload", count)
            prev = e["entry_hash"]
            expected_seq += 1
            count += 1
        return {"ok": True, "entries": count, "head_hash": prev}


def _bad(seq, kind, detail, verified_before):
    return {"ok": False, "first_bad_seq": seq, "kind": kind,
            "detail": detail, "entries_verified_before_failure": verified_before}


# --------------------------------------------------------------------------
# Stores
# --------------------------------------------------------------------------

class LocalChainStore:
    """JSON-lines file. Used by the local dry run and the tests."""

    def __init__(self, path: str):
        self.path = path

    def head(self) -> tuple[int, str]:
        last = None
        try:
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        last = json.loads(line)
        except FileNotFoundError:
            pass
        return (last["seq"], last["entry_hash"]) if last else (0, GENESIS_PREV)

    def append_entry(self, seq: int, entry: dict, prev_entry_hash: str) -> None:
        cur_seq, cur_hash = self.head()
        if cur_seq + 1 != seq or cur_hash != prev_entry_hash:
            raise RuntimeError("chain moved under us; refusing to append")
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(canonical(entry) + "\n")

    def entries(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        yield json.loads(line)
        except FileNotFoundError:
            return


class DynamoChainStore:
    """DynamoDB-backed chain.

    The entry write and the head move happen in one TransactWriteItems with
    conditions on both, so a concurrent run either wins cleanly or fails
    cleanly. Neither can produce a fork.
    """

    def __init__(self, table_name: str, dynamodb=None, client=None):
        import boto3
        self.table_name = table_name
        self.ddb = dynamodb or boto3.resource("dynamodb")
        self.table = self.ddb.Table(table_name)
        self.client = client or boto3.client("dynamodb")

    def head(self) -> tuple[int, str]:
        r = self.table.get_item(Key={"pk": "CHAIN", "sk": "HEAD"}, ConsistentRead=True)
        it = r.get("Item")
        if not it:
            return 0, GENESIS_PREV
        return int(it["seq"]), it["entry_hash"]

    def append_entry(self, seq: int, entry: dict, prev_entry_hash: str) -> None:
        from boto3.dynamodb.types import TypeSerializer
        ser = TypeSerializer()

        item = {"pk": "CHAIN", "sk": f"{seq:0{SEQ_WIDTH}d}", **entry}
        head = {"pk": "CHAIN", "sk": "HEAD", "seq": seq, "entry_hash": entry["entry_hash"]}

        def av(d):
            return {k: ser.serialize(v) for k, v in d.items()}

        if seq == 1:
            head_condition = "attribute_not_exists(pk)"
            names, values = None, None
        else:
            head_condition = "entry_hash = :prev AND #s = :prevseq"
            names = {"#s": "seq"}
            values = {":prev": ser.serialize(prev_entry_hash),
                      ":prevseq": ser.serialize(seq - 1)}

        put_head = {"Put": {"TableName": self.table_name, "Item": av(head),
                            "ConditionExpression": head_condition}}
        if names:
            put_head["Put"]["ExpressionAttributeNames"] = names
        if values:
            put_head["Put"]["ExpressionAttributeValues"] = values

        self.client.transact_write_items(TransactItems=[
            {"Put": {"TableName": self.table_name, "Item": av(item),
                     "ConditionExpression": "attribute_not_exists(sk)"}},
            put_head,
        ])

    def entries(self):
        from boto3.dynamodb.conditions import Key
        kwargs = {
            "KeyConditionExpression": Key("pk").eq("CHAIN") & Key("sk").lt("HEAD"),
            "ConsistentRead": True,
        }
        while True:
            r = self.table.query(**kwargs)
            for it in r.get("Items", []):
                yield _to_plain(it)
            if "LastEvaluatedKey" not in r:
                return
            kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def _to_plain(item):
    """DynamoDB gives Decimals back; hashing needs the ints it was given."""
    from decimal import Decimal

    def conv(v):
        if isinstance(v, Decimal):
            return int(v) if v == v.to_integral_value() else float(v)
        if isinstance(v, dict):
            return {k: conv(x) for k, x in v.items()}
        if isinstance(v, list):
            return [conv(x) for x in v]
        return v

    return {k: conv(v) for k, v in item.items()}
