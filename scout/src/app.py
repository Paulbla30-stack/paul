"""JARVIS scout — Stage 1: find and log.

Runs once a day. Fetches from the enabled sources, scores everything against
the rules in config.toml, stores what it has not seen before, appends each
new hit to a hash-chained log, and emails Paul a digest of whatever cleared
the threshold. If nothing clears it, no email is sent.

It never posts anywhere. It has no credentials for any posting account, no
access to the heartbeat-memory server, and in Stage 1 it calls no model at
all. Everything it fetches is untrusted data (see sources/__init__.py).
"""

from __future__ import annotations

import logging
import os
import sys
import tomllib
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chain import Chain, DynamoChainStore, LocalChainStore          # noqa: E402
from scoring import Scorer                                          # noqa: E402
from store import DynamoHitStore, LocalHitStore, record_from        # noqa: E402
from sources import arxiv, hackernews, lesswrong, medrxiv, moltbook  # noqa: E402
import digest                                                       # noqa: E402

log = logging.getLogger("jarvis.scout")
logging.getLogger().setLevel(logging.INFO)

CONFIG_PATH = os.environ.get(
    "SCOUT_CONFIG",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.toml"),
)


def load_config(path: str | None = None) -> dict:
    with open(path or CONFIG_PATH, "rb") as fh:
        return tomllib.load(fh)


def collect(cfg: dict, now: datetime) -> tuple[list, list[str], list[str]]:
    """Fetch from every enabled source. One source failing never fails the run."""
    lookback = int(cfg["run"]["lookback_days"])
    limit = int(cfg["run"]["max_items_per_source"])
    kw = cfg["keywords"]
    search_terms = list(kw["tier_a"]) + list(kw["tier_b"])

    items, ok, failed = [], [], []
    # Order matters. arXiv throttles hard and answers 406 when it does, so it
    # goes first, before the ~20 rapid queries Hacker News needs. Running it
    # last cost a whole afternoon of "arXiv is broken" that it was not.
    plan = [
        ("arxiv", lambda c: arxiv.fetch(c, now, lookback, limit)),
        ("medrxiv", lambda c: medrxiv.fetch(c, now, lookback, limit)),
        ("lesswrong", lambda c: lesswrong.fetch(c, now, lookback, limit)),
        ("hackernews", lambda c: hackernews.fetch(c, search_terms, now, lookback, limit)),
        ("moltbook", lambda c: moltbook.fetch(c, search_terms, now, lookback, limit)),
    ]
    for name, fn in plan:
        scfg = cfg["sources"].get(name, {})
        if not scfg.get("enabled"):
            log.info("source %s: disabled in config", name)
            continue
        try:
            got = fn(scfg)
            items.extend(got)
            ok.append(name)
            log.info("source %s: %d items", name, len(got))
        except Exception as e:   # noqa: BLE001 - one bad source must not kill the run
            failed.append(name)
            log.warning("source %s FAILED: %s: %s", name, type(e).__name__, str(e)[:200])
    return items, ok, failed


def run(cfg: dict, *, hit_store, chain_store, mailer, now: datetime | None = None,
        dry_run: bool = False) -> dict:
    now = now or datetime.now(timezone.utc)
    scorer = Scorer(cfg)
    chain = Chain(chain_store)
    threshold = float(cfg["run"]["digest_threshold"])

    # First run sweeps wide. With nothing stored there is no backlog to
    # de-duplicate against, so a narrow window would hand over a near-empty
    # digest and quietly lose everything published before today.
    first_run = False
    try:
        first_run = chain_store.head()[0] == 0
    except Exception:                       # noqa: BLE001 - treat unknown as not-first
        pass
    if first_run:
        wide = int(cfg["run"].get("first_run_lookback_days",
                                  cfg["run"]["lookback_days"]))
        cfg = {**cfg, "run": {**cfg["run"], "lookback_days": wide}}
        log.info("first run: sweeping %d days instead of the usual window", wide)

    items, ok, failed = collect(cfg, now)

    vetoed = 0
    new_records: list[dict] = []
    for it in items:
        if scorer.vetoed(it.title, it.body):
            vetoed += 1
            continue
        if hit_store.seen(it.key):
            continue
        weight = float(cfg["sources"][it.source]["weight"])
        if it.source == "hackernews" and it.extra.get("kind") == "comment":
            weight *= float(cfg["sources"]["hackernews"].get("comment_weight", 1.0))
        sc = scorer.score(title=it.title, body=it.body,
                          source_weight=weight, published=it.published, now=now)
        if not sc.matches:
            continue        # nothing matched; not worth a row or a chain entry
        rec = record_from(it, sc, now)
        if not dry_run:
            hit_store.put(it.key, rec)
            entry = chain.append({k: rec[k] for k in
                                  ("source", "external_id", "url", "title",
                                   "published", "first_seen", "matched_keywords",
                                   "score")})
            rec["chain_seq"] = entry["seq"]
        new_records.append(rec)

    above = sorted([r for r in new_records if r["score"] >= threshold],
                   key=lambda r: r["score"], reverse=True)
    capped = above[: int(cfg["run"]["digest_max_items"])]

    stats = {
        "first_run": first_run,
        "lookback_days": int(cfg["run"]["lookback_days"]),
        "fetched": len(items),
        "vetoed": vetoed,
        "new": len(new_records),
        "above": len(above),
        "sources_ok": ok,
        "sources_failed": failed,
        "threshold": threshold,
    }
    try:
        stats["chain_entries"] = chain.verify().get("entries", "?")
    except Exception:            # noqa: BLE001
        stats["chain_entries"] = "?"

    sent = False
    if capped and not dry_run:
        subject, text, html_body = digest.build(capped, run_stats=stats, threshold=threshold)
        sent = mailer.send(subject, text, html_body)
    elif not capped:
        log.info("nothing above threshold %.1f — no email sent", threshold)

    stats["email_sent"] = sent
    stats["digest_items"] = len(capped)
    log.info("run complete: %s", stats)
    return {"stats": stats, "digest": capped, "all_new": new_records}


class SesMailer:
    def __init__(self, cfg: dict, region: str):
        import boto3
        self.cfg = cfg["email"]
        self.ses = boto3.client("sesv2", region_name=region)
        self.prefix = self.cfg.get("subject_prefix", "JARVIS scout")

    def _verified(self) -> set[str]:
        try:
            r = self.ses.list_email_identities()
            return {i["IdentityName"].lower() for i in r.get("EmailIdentities", [])
                    if i.get("SendingEnabled")}
        except Exception as e:                      # noqa: BLE001
            log.warning("could not list SES identities: %s", e)
            return set()

    def resolve(self) -> tuple[str, str, bool]:
        """Pick sender/recipient that SES will actually accept.

        SES is in sandbox on this account: both ends must be verified. If
        the configured pair is not, fall back only if the config allows it,
        and say so loudly — a digest silently going to the wrong address is
        worse than one that fails.
        """
        ok = self._verified()
        to, sender = self.cfg["to"], self.cfg["sender"]
        if to.lower() in ok and sender.lower() in ok:
            return to, sender, False
        if self.cfg.get("fallback_enabled"):
            log.warning(
                "SES: %s and/or %s not verified; falling back to %s. "
                "Verify the real address to stop this.",
                to, sender, self.cfg["fallback_to"])
            return self.cfg["fallback_to"], self.cfg["fallback_sender"], True
        raise RuntimeError(
            f"SES sandbox: {to} / {sender} not verified and fallback disabled")

    def send(self, subject: str, text: str, html_body: str) -> bool:
        to, sender, fell_back = self.resolve()
        subj = f"{self.prefix}: {subject}" + (" [fallback address]" if fell_back else "")
        self.ses.send_email(
            FromEmailAddress=sender,
            Destination={"ToAddresses": [to]},
            Content={"Simple": {
                "Subject": {"Data": subj, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": text, "Charset": "UTF-8"},
                         "Html": {"Data": html_body, "Charset": "UTF-8"}},
            }},
        )
        log.info("digest sent to %s (fallback=%s)", to, fell_back)
        return True


def handler(event, context):
    """Lambda entry point."""
    cfg = load_config()
    table = os.environ["SCOUT_TABLE"]
    region = os.environ.get("AWS_REGION", "us-west-2")
    result = run(
        cfg,
        hit_store=DynamoHitStore(table),
        chain_store=DynamoChainStore(table),
        mailer=SesMailer(cfg, region),
    )
    return {"ok": True, **result["stats"]}
