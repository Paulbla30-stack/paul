"""What the scout has drafted, and what happened to it.

Paul's framing, 23 September 2026: the UI exists to build a partner that
complements his strengths, and what he needs from it is somewhere to see the
advertising work and act on it. The design was written that day and not built;
he noticed a week later that the tab was not there.

**This reads. It does not decide.** Two separate fences, and it is worth being
clear which does what, because "read-only" is easy to say and easy to lose.

  * The IAM policy gives the instance `dynamodb:Query` and `GetItem` on the
    scout's table and nothing else -- no PutItem, no UpdateItem, no
    DeleteItem. That is the fence that holds if this file is wrong.
  * Nothing here constructs a writer, so a later edit has to add a capability
    rather than remove a check. That is the fence that makes the first one
    hard to trip over by accident.

**Approval stays on the emailed link.** The approve app authenticates each
decision with an HMAC token minted when the digest is sent. Approving from
this page would mean putting that signing secret on an internet-facing box,
which changes what the box can do from "show Paul the drafts" to "approve
them", and that is a decision about the gate rather than about a dashboard.
So the page shows the draft, the rationale, the disclosures and how long is
left, and the decision is made where it already is.

The one number that matters on the page is time remaining. Short windows and
an approval gate pull against each other, and worst exactly where it matters:
a reply lapses in two days, so away for three and the most time-sensitive
drafts are the ones that die. That is correct -- silence is not consent -- but
it means the ones closest to lapsing have to sort first, or the gate quietly
becomes a filter that only passes the unimportant.
"""

import logging
import time
from typing import Optional

DEFAULT_TABLE = "jarvis-scout"
DEFAULT_REGION = "us-west-2"

# Pending first and soonest-to-lapse at the top; then what was decided.
PENDING = "pending"
LIVE_STATES = (PENDING,)
MAX_DRAFT_CHARS = 4000


def _clip(value, limit=300) -> str:
    return " ".join(str(value or "").split())[:limit]


class ScoutView:
    """A read-only window onto the scout's proposals.

    Every method returns plain data and swallows its own failures: the scout
    is a separate system with its own account boundary, and the agent's UI
    should degrade to "cannot reach it" rather than fall over. A marketing
    panel that 500s takes the chat panel with it.
    """

    def __init__(self, table: str = DEFAULT_TABLE, region: str = DEFAULT_REGION,
                 logger: Optional[logging.Logger] = None, resource=None,
                 clock=time.time):
        self.table_name = table
        self.region = region
        self.log = logger or logging.getLogger("jarvis.marketing")
        self.clock = clock
        self._resource = resource
        self._table = None
        self.reason: Optional[str] = None

    # ---- the connection -------------------------------------------------

    @property
    def table(self):
        if self._table is None:
            if self._resource is None:
                import boto3
                self._resource = boto3.resource("dynamodb", region_name=self.region)
            self._table = self._resource.Table(self.table_name)
        return self._table

    def available(self) -> bool:
        try:
            self.table.name          # noqa: B018 - cheap reachability probe
            return True
        except Exception as exc:     # noqa: BLE001
            self.reason = f"{type(exc).__name__}: {exc}"
            return False

    # ---- reading --------------------------------------------------------

    def _rows(self, limit: int) -> list:
        """Every proposal row, newest first. Scan, because proposals are few.

        A Query would need an index this table does not have, and the whole
        point of the cap on drafts per run is that there are never many of
        these. If that stops being true, this wants an index, not a bigger
        scan.
        """
        from boto3.dynamodb.conditions import Attr
        out, start = [], None
        while len(out) < limit:
            kwargs = {"FilterExpression": Attr("sk").eq("PROPOSAL")}
            if start:
                kwargs["ExclusiveStartKey"] = start
            page = self.table.scan(**kwargs)
            out.extend(page.get("Items") or [])
            start = page.get("LastEvaluatedKey")
            if not start:
                break
        out.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        return out[:limit]

    def _decorate(self, row: dict) -> dict:
        """One proposal, as the page needs it.

        `left_s` is the number the page sorts on and the only derived value
        here: everything else is what the scout wrote.
        """
        import datetime as _dt
        expires = str(row.get("expires_at") or "")
        left = None
        if expires:
            try:
                when = _dt.datetime.fromisoformat(expires)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=_dt.timezone.utc)
                left = when.timestamp() - self.clock()
            except ValueError:
                left = None
        return {
            "id": str(row.get("id") or ""),
            "status": str(row.get("status") or "?"),
            "network": str(row.get("network") or "?"),
            "purpose": str(row.get("purpose") or "?"),
            "tenant": str(row.get("tenant") or "?"),
            "kind": str(row.get("kind") or "post"),
            "title": _clip(row.get("target_title")),
            "url": _clip(row.get("target_url"), 500),
            # The draft is the thing being approved, so it is NOT summarised:
            # a truncated draft on the page and a full one on the network is
            # approving something nobody read.
            "draft": str(row.get("draft") or "")[:MAX_DRAFT_CHARS],
            "rationale": _clip(row.get("rationale"), 600),
            "discloses": [_clip(d, 80) for d in (row.get("discloses") or [])],
            "created_at": str(row.get("created_at") or ""),
            "expires_at": expires,
            "left_s": None if left is None else round(left),
            "decided_by": _clip(row.get("decided_by"), 80) or None,
            "decided_at": str(row.get("decided_at") or "") or None,
            "detail": _clip(row.get("detail"), 300) or None,
        }

    def state(self, limit: int = 40) -> dict:
        """Everything the marketing panel shows, in one call."""
        if not self.available():
            return {"reachable": False, "reason": self.reason,
                    "pending": [], "recent": [], "counts": {}}
        try:
            rows = [self._decorate(r) for r in self._rows(limit)]
        except Exception as exc:     # noqa: BLE001
            self.log.warning("could not read proposals: %s", exc)
            return {"reachable": False, "reason": f"{type(exc).__name__}: {exc}",
                    "pending": [], "recent": [], "counts": {}}

        pending = [r for r in rows if r["status"] == PENDING]
        # Soonest to lapse first. A reply lapses in two days; if the ones
        # closest to the edge are not at the top, the gate passes only the
        # things nobody was in a hurry about.
        pending.sort(key=lambda r: (r["left_s"] is None, r["left_s"]))
        recent = [r for r in rows if r["status"] != PENDING][:20]

        counts: dict = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        return {"reachable": True, "reason": None, "pending": pending,
                "recent": recent, "counts": counts,
                "expiring_soon": sum(1 for r in pending
                                     if r["left_s"] is not None
                                     and r["left_s"] < 86400)}

    def summary(self) -> dict:
        """Two numbers for the agent's own status, not the page."""
        got = self.state(limit=40)
        return {"reachable": got["reachable"],
                "pending": len(got["pending"]),
                "expiring_soon": got.get("expiring_soon", 0)}


def build_view(config: Optional[dict] = None,
               logger: Optional[logging.Logger] = None) -> Optional[ScoutView]:
    """Build the view from config, or None when the operator has not asked.

    Off unless configured. The instance and the scout have shared no code, no
    IAM and no visibility until now, and the first coupling between them is
    worth switching on deliberately rather than finding on by default.
    """
    cfg = (config or {}).get("marketing")
    cfg = cfg if isinstance(cfg, dict) else {}
    if not cfg.get("enabled"):
        return None
    return ScoutView(table=str(cfg.get("table") or DEFAULT_TABLE),
                     region=str(cfg.get("region") or DEFAULT_REGION),
                     logger=logger)
