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
import sources                                                      # noqa: E402
from proposals import Proposal                                       # noqa: E402
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


def collect(cfg: dict, now: datetime) -> tuple[list, list[str], list[str], list[str]]:
    """Fetch from every enabled source. One source failing never fails the run."""
    lookback = int(cfg["run"]["lookback_days"])
    limit = int(cfg["run"]["max_items_per_source"])
    kw = cfg["keywords"]
    search_terms = list(kw["tier_a"]) + list(kw["tier_b"])

    # How long any one source may spend. Nothing bounded this before: every
    # request had a timeout and a paginating source could still make hundreds
    # of them. On 23 Sep 2026 medRxiv took 234 seconds of the Lambda's 300 and
    # the run hit the wall; it survived only because Lambda retried it, and a
    # retry after a partial run can mark items seen and then send nothing.
    budget_s = float(cfg["run"].get("source_budget_s", 90.0))
    items, ok, failed, truncated = [], [], [], []
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
        budget = sources.Budget(budget_s)
        sources.set_budget(budget)
        try:
            got = fn(scfg)
            items.extend(got)
            ok.append(name)
            if budget.tripped:
                # Reported separately from ok and from failed, because it is
                # neither: some of the window was read and the rest was not,
                # and calling that a clean sweep is how a partial day gets
                # filed as a complete one.
                truncated.append(name)
                log.warning("source %s: %d items, TRUNCATED at its %.0fs budget",
                            name, len(got), budget_s)
            else:
                log.info("source %s: %d items", name, len(got))
        except Exception as e:   # noqa: BLE001 - one bad source must not kill the run
            failed.append(name)
            log.warning("source %s FAILED: %s: %s", name, type(e).__name__, str(e)[:200])
        finally:
            sources.set_budget(None)
    return items, ok, failed, truncated


def _subject(title: str) -> str:
    """A crude key for "this is the same paper again".

    arXiv revisions arrive as separate ids with the same title, and the live
    table already holds one paper twice. Drafting it twice is two model calls
    and two near-identical posts for one piece of work.
    """
    return " ".join("".join(c for c in (title or "").lower()
                            if c.isalnum() or c.isspace()).split())[:90]


def propose(cfg: dict, records: list[dict], *, drafter, store,
            now: datetime | None = None, dry_run: bool = False) -> dict:
    """Draft the few items worth a public post, and file them for approval.

    Separate from the digest and held to a higher bar. The digest threshold
    answers "worth reading"; this answers "worth saying something about in
    public, under Paul's name", which is a different and rarer question. On
    the first 41 stored hits: 17 cleared the digest's 3.0, six cleared 6.0 and
    three cleared 7.0.

    Two caps, for two different reasons. The threshold is about quality; the
    per-run limit is about cost and attention -- every draft is a model call,
    and every proposal competes with the others for the one operator who has
    to read them. A flood of mediocre drafts buries a good one.

    Nothing here sends anything. It files proposals; the approve app is the
    only thing that can move one to sent.
    """
    now = now or datetime.now(timezone.utc)
    draft_cfg = cfg.get("drafting") or {}
    threshold = float(draft_cfg.get("post_threshold", 6.0))
    cap = int(draft_cfg.get("max_drafts_per_run", 3))
    network = str(draft_cfg.get("network", "moltbook")).strip().lower()
    purpose = str(draft_cfg.get("purpose", "news")).strip().lower()

    stats = {"considered": 0, "drafted": 0, "proposed": 0, "declined": [],
             "skipped_duplicate": 0, "ids": [], "threshold": threshold,
             "cap": cap, "network": network}
    if drafter is None or store is None:
        stats["skipped"] = "no drafter or proposal store configured"
        return stats
    if cap <= 0:
        stats["skipped"] = "max_drafts_per_run is 0"
        return stats

    eligible = sorted((r for r in records if float(r.get("score") or 0) >= threshold),
                      key=lambda r: -float(r["score"]))
    stats["considered"] = len(eligible)
    if not eligible:
        return stats

    # Do not propose the same paper twice. Checked against what is already
    # waiting as well as within this batch: a proposal sitting unread for
    # three days is exactly when the next run would offer it again.
    seen_subjects = set()
    try:
        for p in store.pending(limit=100):
            seen_subjects.add(_subject(p.target_title))
    except Exception as exc:                      # noqa: BLE001
        log.warning("could not read pending proposals; duplicate guard is "
                    "this run only: %s", exc)

    for rec in eligible:
        if stats["drafted"] >= cap:
            break
        subject = _subject(rec.get("title"))
        if subject in seen_subjects:
            stats["skipped_duplicate"] += 1
            log.info("already proposed, not drafting again: %s",
                     str(rec.get("title"))[:70])
            continue
        seen_subjects.add(subject)

        item = {"source": rec.get("source"), "title": rec.get("title"),
                "author": rec.get("author") or "", "url": rec.get("url"),
                "body": rec.get("body") or rec.get("summary") or ""}
        stats["drafted"] += 1
        try:
            out = drafter.draft(item)
        except Exception as exc:                  # noqa: BLE001 - a bad draft
            log.warning("drafting failed for %s: %s: %s",
                        str(rec.get("title"))[:60], type(exc).__name__, exc)
            stats["declined"].append({"title": str(rec.get("title"))[:120],
                                      "reason": f"{type(exc).__name__}: {exc}"[:200]})
            continue

        if not out.get("worth_posting"):
            # Recorded with its reason rather than dropped silently, so a
            # pipeline that has quietly narrowed to nothing is visible.
            stats["declined"].append({"title": str(rec.get("title"))[:120],
                                      "reason": str(out.get("reason") or "declined")[:200]})
            continue

        proposal = Proposal.new(
            kind="post", network=network,
            target_url=str(rec.get("url") or ""),
            target_title=str(rec.get("title") or "")[:300],
            draft=out.get("draft", ""),
            rationale=str(out.get("rationale") or "")[:600],
            discloses=list(out.get("disclosures") or []),
            source_item={k: rec.get(k) for k in
                         ("source", "external_id", "url", "score", "chain_seq")},
            now=now, purpose=purpose)
        if not dry_run:
            store.put(proposal)
        stats["proposed"] += 1
        stats["ids"].append(proposal.id)
        log.info("proposal %s filed for %s (%s, expires %s)", proposal.id,
                 network, purpose, proposal.expires_at)
    return stats


def run(cfg: dict, *, hit_store, chain_store, mailer, now: datetime | None = None,
        dry_run: bool = False, drafter=None, proposal_store=None) -> dict:
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

    items, ok, failed, truncated = collect(cfg, now)

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
            # Chain FIRST, then store the hit. The order matters and the
            # obvious one is wrong: storing the hit first marks it seen, so
            # if the chain append then fails the item is skipped as a
            # duplicate on every future run and never enters the record at
            # all — a silent, permanent gap. This way a failed append leaves
            # the item unseen and the next run picks it up again.
            try:
                entry = chain.append({k: rec[k] for k in
                                      ("source", "external_id", "url", "title",
                                       "published", "first_seen",
                                       "matched_keywords", "score")})
            except Exception as e:                    # noqa: BLE001
                log.error("chain append failed for %s; not storing the hit so "
                          "it is retried next run: %s", it.key, e)
                continue
            rec["chain_seq"] = entry["seq"]
            hit_store.put(it.key, rec)
        new_records.append(rec)

    above = sorted([r for r in new_records if r["score"] >= threshold],
                   key=lambda r: r["score"], reverse=True)
    capped = above[: int(cfg["run"]["digest_max_items"])]

    # Drafting runs on what is new this run, not on the digest's selection:
    # the digest is capped for readability and this is capped for cost, and
    # conflating them would let a long digest decide how much gets written.
    stats_propose = propose(cfg, new_records, drafter=drafter,
                            store=proposal_store, now=now, dry_run=dry_run)

    stats = {
        "first_run": first_run,
        "lookback_days": int(cfg["run"]["lookback_days"]),
        "fetched": len(items),
        "vetoed": vetoed,
        "new": len(new_records),
        "above": len(above),
        "sources_ok": ok,
        "sources_failed": failed,
        "sources_truncated": truncated,
        "threshold": threshold,
        "proposed": stats_propose["proposed"],
        "drafted": stats_propose["drafted"],
        "declined": len(stats_propose["declined"]),
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
        if self._accepted(to, ok) and self._accepted(sender, ok):
            return to, sender, False
        missing = sorted({a for a in (to, sender) if not self._accepted(a, ok)})
        if self.cfg.get("fallback_enabled"):
            # Name the addresses that are actually missing. The old message
            # said "X and/or Y" even when X and Y were the same address, which
            # tells a reader neither which end failed nor how to fix it.
            log.warning(
                "SES: not a verified identity: %s. Falling back to %s. "
                "Verify the address, or verify the whole domain (Easy DKIM), "
                "which covers every address on it.",
                ", ".join(missing), self.cfg["fallback_to"])
            return self.cfg["fallback_to"], self.cfg["fallback_sender"], True
        raise RuntimeError(
            "SES sandbox: not a verified identity: " + ", ".join(missing)
            + "; fallback disabled")

    @staticmethod
    def _accepted(address: str, verified: set) -> bool:
        """True when SES will accept this address.

        A verified DOMAIN identity covers every address on it, so checking
        only for the exact address reports a working setup as broken.
        """
        address = (address or "").lower()
        if address in verified:
            return True
        _, _, domain = address.partition("@")
        return bool(domain) and domain in verified

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


def stage_two(cfg: dict, table: str, region: str):
    """Build the drafter and the proposal store, or explain why not.

    Returns (None, None) rather than raising. Stage 2 is the half that writes
    drafts for Paul to approve; the half that reads, scores, chains and emails
    has run every day since 21 September and must not stop because the model
    is unreachable or the config turned drafting off.
    """
    draft_cfg = cfg.get("drafting") or {}
    if int(draft_cfg.get("max_drafts_per_run", 0)) <= 0:
        log.info("drafting off (max_drafts_per_run is 0)")
        return None, None
    try:
        from drafting import Drafter
        from proposals import ProposalStore
        provider = str(draft_cfg.get("provider") or "converse").strip().lower()
        # Each provider names its model in its own key, because the ids are
        # not interchangeable and a single "model" would silently send an
        # Anthropic id to Converse the day the provider changed.
        model = {"converse": draft_cfg.get("converse_model"),
                 "bedrock": draft_cfg.get("bedrock_model")}.get(
                     provider, draft_cfg.get("model"))
        drafter = Drafter(model=model, provider=provider,
                          region=str(draft_cfg.get("bedrock_region") or region),
                          effort=str(draft_cfg.get("effort") or "high"))
        log.info("drafting enabled: %s via %s, at or above %.1f, %d per run",
                 drafter.model, provider,
                 float(draft_cfg.get("post_threshold", 6.0)),
                 int(draft_cfg.get("max_drafts_per_run", 3)))
        return drafter, ProposalStore(table)
    except Exception as exc:                      # noqa: BLE001
        log.error("drafting unavailable, the rest of the run continues: %s: %s",
                  type(exc).__name__, exc)
        return None, None


def handler(event, context):
    """Lambda entry point."""
    cfg = load_config()
    table = os.environ["SCOUT_TABLE"]
    region = os.environ.get("AWS_REGION", "us-west-2")
    drafter, proposal_store = stage_two(cfg, table, region)
    result = run(
        cfg,
        hit_store=DynamoHitStore(table),
        chain_store=DynamoChainStore(table),
        mailer=SesMailer(cfg, region),
        drafter=drafter,
        proposal_store=proposal_store,
    )
    return {"ok": True, **result["stats"]}
