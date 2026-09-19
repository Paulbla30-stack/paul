"""What this agent costs, and what it is leaving behind.

Two things nobody was watching.

The first is spend. The planner is an open-weight model imported into Bedrock,
which bills per model-minute rather than per call, so an idle agent that wakes
every thirty seconds to conclude "no action is required" is not free -- it is
the most expensive way to do nothing. Add the instance, the encrypted volume,
the Object Lock bucket that cannot be emptied for thirty days, and the failure
mode is not a crash. It is a bill, noticed a month late.

The second is accumulation. Building this left old AMIs, their snapshots, a
stale key pair and buckets from a previous name, and they are still there,
because they were on a human's list rather than the agent's. An agent that runs
the estate should be the thing that notices the estate growing.

Both are reports. Nothing here deletes, or can: the constitution's "never
permanently delete" is not a rule this asks to be excused from, and an Object
Lock bucket would refuse anyway. What it produces is a list and a number, which
is the part a person actually needs -- deciding what to remove is judgement
about what might still be wanted, and that is not the agent's to make.

Every call is bounded and every failure is a note rather than an exception: a
missing IAM grant should degrade this to "I cannot see that", not stop the loop.
"""

import logging
import time
from typing import Optional

DEFAULT_STALE_DAYS = 30
DEFAULT_MAX_ITEMS = 20


class Estate:
    """Read-only look at what the account is spending and holding."""

    def __init__(self, region: Optional[str] = None,
                 logger: Optional[logging.Logger] = None,
                 config: Optional[dict] = None, clients=None, clock=time.time):
        cfg = dict(config or {})
        self.region = region
        self.log = logger or logging.getLogger("jarvis.estate")
        self.clock = clock
        self.enabled = bool(cfg.get("enabled", True))
        self.stale_days = int(cfg.get("stale_days", DEFAULT_STALE_DAYS))
        self.max_items = int(cfg.get("max_items", DEFAULT_MAX_ITEMS))
        self.project_tag = str(cfg.get("project_tag") or "jarvis")
        self._clients = clients or {}

    def _client(self, name: str):
        if name not in self._clients:
            import boto3
            kwargs = {"region_name": self.region} if self.region else {}
            # Cost Explorer only answers in us-east-1, whatever the account's
            # home region is.
            if name == "ce":
                kwargs = {"region_name": "us-east-1"}
            self._clients[name] = boto3.client(name, **kwargs)
        return self._clients[name]

    # ---- spend ----------------------------------------------------------

    def cost(self, days: int = 7) -> dict:
        """What the account actually spent, by service, from Cost Explorer.

        Asked of the billing system rather than estimated from local counters,
        because an estimate that drifts is worse than no number: it gets
        believed. Cost Explorer charges a cent a call, which is why this is
        on a slow clock and not a per-cycle probe.
        """
        from datetime import date, timedelta
        end = date.fromtimestamp(self.clock())
        start = end - timedelta(days=max(1, days))
        try:
            resp = self._client("ce").get_cost_and_usage(
                TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
                Granularity="DAILY", Metrics=["UnblendedCost"],
                GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}])
        except Exception as exc:
            return {"available": False,
                    "reason": f"{type(exc).__name__}: {exc}"[:200]}
        by_service: dict = {}
        currency = "USD"
        for period in resp.get("ResultsByTime", []):
            for group in period.get("Groups", []):
                service = (group.get("Keys") or ["unknown"])[0]
                amount = group.get("Metrics", {}).get("UnblendedCost", {})
                currency = amount.get("Unit") or currency
                by_service[service] = by_service.get(service, 0.0) + float(
                    amount.get("Amount") or 0.0)
        ranked = sorted(by_service.items(), key=lambda kv: -kv[1])
        total = sum(by_service.values())
        return {"available": True, "days": days, "currency": currency,
                "total": round(total, 2),
                "per_day": round(total / max(1, days), 2),
                "by_service": [{"service": s, "amount": round(a, 2)}
                               for s, a in ranked[:self.max_items] if a > 0.005]}

    # ---- accumulation ---------------------------------------------------

    def leftovers(self) -> dict:
        """Images, snapshots and buckets that have been sitting a while."""
        cutoff = self.clock() - self.stale_days * 86400
        out = {"stale_days": self.stale_days, "images": [], "snapshots": [],
               "buckets": [], "unavailable": []}
        self._images(out, cutoff)
        self._snapshots(out, cutoff)
        self._buckets(out, cutoff)
        out["total"] = len(out["images"]) + len(out["snapshots"]) + len(out["buckets"])
        return out

    def _images(self, out: dict, cutoff: float):
        try:
            images = self._client("ec2").describe_images(Owners=["self"])["Images"]
        except Exception as exc:
            out["unavailable"].append(f"images: {type(exc).__name__}")
            return
        for image in images:
            when = _iso_to_epoch(image.get("CreationDate"))
            if when is not None and when < cutoff:
                out["images"].append({"id": image.get("ImageId"),
                                      "name": image.get("Name"),
                                      "age_days": _age_days(when, self.clock())})
        out["images"] = sorted(out["images"], key=lambda i: -i["age_days"])[:self.max_items]

    def _snapshots(self, out: dict, cutoff: float):
        try:
            snaps = self._client("ec2").describe_snapshots(OwnerIds=["self"])["Snapshots"]
        except Exception as exc:
            out["unavailable"].append(f"snapshots: {type(exc).__name__}")
            return
        for snap in snaps:
            when = snap.get("StartTime")
            when = when.timestamp() if hasattr(when, "timestamp") else _iso_to_epoch(when)
            if when is not None and when < cutoff:
                out["snapshots"].append({"id": snap.get("SnapshotId"),
                                         "gb": snap.get("VolumeSize"),
                                         "age_days": _age_days(when, self.clock())})
        out["snapshots"] = sorted(out["snapshots"],
                                  key=lambda s: -s["age_days"])[:self.max_items]

    def _buckets(self, out: dict, cutoff: float):
        try:
            buckets = self._client("s3").list_buckets()["Buckets"]
        except Exception as exc:
            out["unavailable"].append(f"buckets: {type(exc).__name__}")
            return
        for bucket in buckets:
            when = bucket.get("CreationDate")
            when = when.timestamp() if hasattr(when, "timestamp") else _iso_to_epoch(when)
            if when is not None and when < cutoff:
                out["buckets"].append({"name": bucket.get("Name"),
                                       "age_days": _age_days(when, self.clock())})
        out["buckets"] = sorted(out["buckets"], key=lambda b: -b["age_days"])[:self.max_items]

    # ---- what it says ---------------------------------------------------

    def report(self, days: int = 7) -> dict:
        if not self.enabled:
            return {"enabled": False}
        return {"enabled": True, "cost": self.cost(days), "leftovers": self.leftovers()}

    def lines(self, days: int = 7) -> list:
        """Sentences for the operator, and for the model's own context."""
        report = self.report(days)
        if not report.get("enabled"):
            return []
        out = []
        cost = report["cost"]
        if cost.get("available"):
            top = ", ".join(f"{c['service']} {c['amount']}"
                            for c in cost["by_service"][:3]) or "nothing measurable"
            out.append(f"The account spent {cost['total']} {cost['currency']} over "
                       f"{cost['days']} days, about {cost['per_day']} a day. "
                       f"Mostly: {top}.")
        else:
            out.append(f"Spend is not visible from here ({cost.get('reason')}).")
        left = report["leftovers"]
        if left["total"]:
            bits = []
            for key, one, many in (("images", "machine image", "machine images"),
                                   ("snapshots", "snapshot", "snapshots"),
                                   ("buckets", "bucket", "buckets")):
                count = len(left[key])
                if count:
                    bits.append(f"{count} {one if count == 1 else many}")
            out.append(f"Older than {left['stale_days']} days and still here: "
                       f"{_join(bits)}. Nothing has been deleted: whether these are "
                       f"still wanted is the operator's call, not the agent's.")
        if left["unavailable"]:
            out.append("Could not look at: " + ", ".join(left["unavailable"]) + ".")
        return out


def _join(items) -> str:
    """a, b and c -- because this is read by a person, on a phone."""
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _iso_to_epoch(value) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _age_days(when: float, now: float) -> int:
    return int(max(0.0, now - when) / 86400)


def build_estate(config: Optional[dict], logger=None, clients=None) -> Estate:
    cloud = (config or {}).get("cloud") or {}
    region = (cloud.get("instance") or {}).get("region") or (config or {}).get("region")
    return Estate(region, logger, cloud.get("estate") or {}, clients=clients)
