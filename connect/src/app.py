"""connect-broker: Jarvis's connections, run off his box.

Stage 1 of docs/connections-plan-2026-09-27.md. Built, tested, not deployed
and not wired to Jarvis (Paul, 27 September: "don't link anything up yet,
just build it first and off box").

Why off the box: Jarvis runs as root on a machine it can read end to end, so
anything that holds Paul's keys or sign-ins must live somewhere it cannot
reach. Stage 1 holds no keys at all -- weather, bank holidays and Paul's own
Zenodo records are public -- but it is the framework every later stage
builds on, so the rules are here from the start:

  - the broker checks every call against the frozen manifest itself, and
    does not trust the caller about anything
  - a connection is off until Paul lists it in ENABLED_CONNECTIONS, and the
    box can switch one off (never on) through the "disable" operation
  - hourly caps are counted here, in DynamoDB, not by the caller
  - only typed fields come back, capped at 64 KB
  - logs carry the operation, the connection, byte counts and a result
    code; never a URL, a query, a parameter value or an exception message

Operations (event["op"]):
  catalogue                       what exists, and each manifest's sha256
  call   {connection, operation, params, manifest_sha256}
  disable {connection, reason}    reason is one of DISABLE_REASONS
  status                          enabled, disabled and last results
"""

import json
import logging
import os
import time
from typing import Any, Dict

import fetch
import manifest
from connectors import CONNECTORS
from validate import Refused, check_host

log = logging.getLogger()
log.setLevel(logging.INFO)

MAX_RETURN_BYTES = 64 * 1024
DISABLE_REASONS = ("malformed_data", "anomaly", "conflicts_with_diary", "operator")


def _table():
    import boto3
    return boto3.resource("dynamodb").Table(os.environ["CONNECT_TABLE"])


def _enabled() -> set:
    raw = os.environ.get("ENABLED_CONNECTIONS", "")
    return {c.strip() for c in raw.split(",") if c.strip() in manifest.CATALOGUE}


def _log(op: str, connection: str = "", result: str = "ok", nbytes: int = 0):
    log.info(json.dumps({"op": op, "connection": connection, "result": result,
                         "bytes": nbytes}))


class Broker:
    """The logic, with its table and network passed in so tests need neither."""

    def __init__(self, table=None, enabled=None, get=None, clock=time.time):
        self.table = table
        self.enabled = set(enabled) if enabled is not None else _enabled()
        self.get = get or fetch.get
        self.clock = clock

    # ---- operations ----------------------------------------------------

    def catalogue(self) -> dict:
        return {"connections": [
            {"id": cid, "label": m["label"], "host": m["host"], "kind": m["kind"],
             "auth": m["auth"], "data_class": m["data_class"],
             "operations": {name: {"effect": op["effect"], "params": op["params"]}
                            for name, op in m["operations"].items()},
             "limits": m["limits"], "manifest_sha256": manifest.manifest_sha256(m),
             "enabled": cid in self.enabled}
            for cid, m in sorted(manifest.CATALOGUE.items())]}

    def call(self, event: Dict[str, Any]) -> dict:
        cid = event.get("connection")
        m = manifest.get(cid)                               # CallRefused if unknown
        if event.get("manifest_sha256") != manifest.manifest_sha256(m):
            # The caller is working from a manifest that is not this one.
            return {"ok": False, "error": "manifest_changed"}
        if cid not in self.enabled:
            return {"ok": False, "error": "not_enabled"}
        if self._disabled(cid):
            return {"ok": False, "error": "disabled"}
        params = manifest.check_call(cid, event.get("operation"), event.get("params"))
        if not self._take_quota(cid, m["limits"]["calls_per_hour"]):
            return {"ok": False, "error": "hourly_limit"}
        connector = CONNECTORS[cid]
        url = connector.build(event["operation"], params)
        body = self.get(url, check_host(m["host"]),
                        max_bytes=m["limits"]["max_bytes"],
                        timeout=float(m["limits"]["timeout_s"]))
        try:
            records = connector.normalise(event["operation"], body, params)
        except (ValueError, KeyError, TypeError, AttributeError):
            self._note_result(cid, "malformed")
            return {"ok": False, "error": "malformed_response"}
        payload = {"ok": True, "connection": cid, "operation": event["operation"],
                   "fetched_at": int(self.clock()), "records": records}
        size = len(json.dumps(payload))
        if size > MAX_RETURN_BYTES:
            return {"ok": False, "error": "result_too_large"}
        self._note_result(cid, "ok")
        return payload

    def disable(self, event: Dict[str, Any]) -> dict:
        """The box may switch a connection off. Switching on is Paul's alone."""
        cid = event.get("connection")
        manifest.get(cid)
        reason = event.get("reason")
        if reason not in DISABLE_REASONS:
            return {"ok": False, "error": "unknown_reason"}
        if self.table is not None:
            self.table.put_item(Item={"pk": f"conn#{cid}", "sk": "state",
                                      "disabled": True, "reason": reason,
                                      "at": int(self.clock())})
        return {"ok": True, "connection": cid, "disabled": True, "reason": reason}

    def status(self) -> dict:
        out = {}
        for cid in sorted(manifest.CATALOGUE):
            state = self._state(cid)
            out[cid] = {"enabled": cid in self.enabled,
                        "disabled": bool(state.get("disabled")),
                        "disabled_reason": state.get("reason"),
                        "last_result": state.get("last_result"),
                        "last_at": state.get("last_at")}
        return {"connections": out}

    # ---- the table -------------------------------------------------------

    def _state(self, cid: str) -> dict:
        if self.table is None:
            return {}
        item = self.table.get_item(Key={"pk": f"conn#{cid}", "sk": "state"}).get("Item")
        return dict(item or {})

    def _disabled(self, cid: str) -> bool:
        return bool(self._state(cid).get("disabled"))

    def _note_result(self, cid: str, result: str):
        if self.table is None:
            return
        self.table.update_item(
            Key={"pk": f"conn#{cid}", "sk": "state"},
            UpdateExpression="SET last_result = :r, last_at = :t",
            ExpressionAttributeValues={":r": result, ":t": int(self.clock())})

    def _take_quota(self, cid: str, per_hour: int) -> bool:
        """One call against this hour's cap, counted atomically, or False."""
        if self.table is None:
            return True
        hour = int(self.clock() // 3600)
        from botocore.exceptions import ClientError
        try:
            self.table.update_item(
                Key={"pk": f"rate#{cid}", "sk": str(hour)},
                UpdateExpression="ADD calls :one SET expires = :exp",
                ConditionExpression="attribute_not_exists(calls) OR calls < :cap",
                ExpressionAttributeValues={":one": 1, ":cap": per_hour,
                                           ":exp": (hour + 2) * 3600})
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise


def handler(event, context=None):
    op = (event or {}).get("op") if isinstance(event, dict) else None
    cid = event.get("connection", "") if isinstance(event, dict) else ""
    cid = cid if cid in manifest.CATALOGUE else ""
    try:
        broker = Broker(table=_table())
        if op == "catalogue":
            out = broker.catalogue()
        elif op == "call":
            out = broker.call(event)
        elif op == "disable":
            out = broker.disable(event)
        elif op == "status":
            out = broker.status()
        else:
            out = {"ok": False, "error": "unknown_op"}
    except manifest.CallRefused as exc:
        out = {"ok": False, "error": "refused", "why": str(exc)}
    except Refused:
        out = {"ok": False, "error": "host_refused"}
    except fetch.FetchError as exc:
        out = {"ok": False, "error": exc.code}
    except Exception as exc:                  # noqa: BLE001
        # The type only. urllib and botocore messages can carry URLs.
        _log(str(op), cid, f"internal:{type(exc).__name__}")
        return {"ok": False, "error": "internal"}
    _log(str(op), cid, "ok" if out.get("ok", True) else str(out.get("error")),
         len(json.dumps(out)))
    return out
