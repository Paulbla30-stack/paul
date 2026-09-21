# JARVIS scout — Stage 1

Finds online discussions relevant to Paul's work, scores them against rules,
logs them to a hash-chained record, and emails a daily digest at 07:00 UK.

**It never posts anywhere.** It has no credentials for any posting account.
In Stage 1 it calls no model at all. Paul reads the digest and posts as
himself.

Personal to JARVIS, strictly firewalled from Arkin Engine Ltd: no Arkin
code, accounts or data. No access to the `heartbeat-memory` server.

---

## Status

Built and tested locally against the live APIs. **Not deployed** — the
brief says to state the expected monthly cost before deploying anything
billed, and to stop for a decision after the first real run. See
"What the first run actually found" below, which is the decision.

---

## Expected cost

About **£0.05/month**, and under £0.50 on any plausible growth. Nothing
here is billed hourly; every resource is pay-per-use.

| Resource | Basis | Monthly |
|---|---|---|
| Lambda | 30 runs × ~60s × 512MB, arm64 | ~$0.012 |
| DynamoDB | on-demand, ~1,800 writes + ~1,500 reads, <1GB | ~$0.02 |
| EventBridge Scheduler | 30 invocations (free to 14M) | $0.00 |
| SES | ≤30 emails | ~$0.003 |
| CloudWatch Logs | ~5MB ingest, 30-day retention | ~$0.01 |
| S3 | 25KB deploy artefacts | ~$0.00 |

The account budget `jarvis-monthly` ($50, alerts at 20/50/80/100% actual
and 80% forecast) was confirmed present before anything was built.

---

## What the first run actually found

Run against the live APIs on 21 September 2026, with the real daily
settings (2-day lookback, threshold 8):

```
fetched 51 | new 0 | above threshold 0 | vetoed 0
sources ok: medrxiv, lesswrong, hackernews | FAILED: arxiv
```

Over a wider 14-day window, 428 items were fetched and **6** matched any
keyword at all, the best scoring 6.41 — still below the threshold of 8.

That is the finding, and it matters more than the code. **At the
configured keywords and threshold, this scout would email almost nothing.**
Three things are behind it, and they need a decision before deploying:

1. **The threshold is calibrated for a 2-day window.** The 14-day sample is
   depressed by recency decay (an item a week old keeps only half its
   score). On a real daily run a good item scores higher than the sample
   suggests. But not by enough to rescue a yield of 6-in-428.
2. **The keyword list is written the way formal literature writes.** It
   works on medRxiv and arXiv. On discussion sites it matched nothing,
   because people write "clinical AI", not "clinical AI governance".
3. **The niche is genuinely quiet.** This is not a bug. See below.

## What Hacker News actually has

Searched over a 14-day window, with relevance ranking and typo tolerance
off:

| Term | Hits in 14 days |
|---|---|
| `DCB0160` | **0** — and zero all-time |
| `clinical AI governance` | 0 |
| `AI governance healthcare` | 0 |
| `AI mental health safety` | 0 |
| `clinical safety case` | 0 |
| `healthcare AI` | 54 |
| `clinical` | 59 |
| `AI safety` | 409 |

Hacker News does not discuss clinical AI governance, DCB standards or
care-home AI policy. It discusses "healthcare AI" and "clinical AI" in
general terms. A scout looking for the first set will find nothing there
forever; one looking for the second set will find a handful a week, most
of which Paul would not want to join.

**Four terms were added to `tier_c` during the build** — `clinical AI`,
`healthcare AI`, `AI in healthcare`, `AI in nursing` — and marked in
`config.toml` for review. Without them Hacker News contributes nothing at
all. With them it contributes a little, at some cost in noise. Paul's call.

## Why proximity matching

Exact-phrase matching found almost nothing. "AI governance healthcare"
never appears as a phrase; "governance of AI in healthcare" does.

So `tier_b` and `tier_c` match by **proximity**: every significant word of
the keyword must appear within `proximity_window` words of the others, in
any order, with common words (of, in, the) ignored. `tier_a` stays exact,
because those are names — "DCB0160" loosely matched is meaningless.

Both modes and the window are set in `config.toml`.

---

## How scoring works

Rules only. No model. Every number traces to a rule and a match, which is
why the digest can say *why* each item is there.

1. **Find distinct matches** in title and body, per tier.
2. **Weight by tier**, doubled if the match was in the title. A title is a
   claim about what the item is about; a body mention may be an aside.
   Defaults: tier_a 10, tier_b 5, tier_c 3; title multiplier 2.
3. **Diminishing returns.** Matches are sorted strongest first and the Nth
   contributes `weight × 0.6^(N-1)`. Twelve weak terms never outrank one
   strong one.
4. **Source weight, then recency decay.** 8% of the score per day of age,
   with a floor of 50%.
5. **Subtract negative keywords**, 6 points each.

Worked example — a medRxiv paper titled *"Crowdsourcing AI solutions in
healthcare using sensitive data"*:

```
healthcare AI      title  tier_c  3 × 2 = 6.0      → 6.00
AI in healthcare   title  tier_c  3 × 2 = 6.0      → 6.00 × 0.6  = 3.60
clinical AI        body   tier_c  3     = 3.0      → 3.00 × 0.36 = 1.08
                                         subtotal  = 10.68
× source weight (medrxiv 1.2)                      = 12.82
× recency (7 days old, floored at 0.5)             =  6.41
```

Below the threshold of 8 only because the sample was a week old. On the day
of publication it scores 12.8.

The threshold is what decides the digest. At 8: a single broad term in a
title is not enough; two are, and one specific term is.

---

## Sources

| Source | Access | How it is queried |
|---|---|---|
| Hacker News | Algolia, keyless | Searched per keyword, relevance-ranked |
| LessWrong | public GraphQL, keyless | Recent posts swept, filtered locally |
| arXiv | public Atom API, keyless | `cs.CY`, `cs.AI`, `cs.HC`, swept |
| medRxiv | public API, keyless | Date range, filtered to 4 categories |

**Stage 1 needs no secrets at all.** Every API above is public and keyless,
so there is nothing in Secrets Manager or SSM and nothing in the code. The
first secret this project needs will be Reddit's, if it is ever approved.

### Reddit

**Skipped, per the brief.** Reddit closed self-service API registration
under its Responsible Builder Policy (updated 11 November 2025). Every new
OAuth client, free or paid, now needs manual approval, reported at 2–4
weeks with personal projects the most-rejected category. Unauthenticated
access was withdrawn in May 2026 and returns 403 — confirmed directly on
21 September 2026 against `r/nursing`.

The brief says: *if approval is required, tell me and skip Reddit for now.*
So `sources.reddit.enabled = false`. The five subreddits are already listed
in the config for whenever access is granted. Their existence could not be
checked, because checking requires the access we do not have.

### LinkedIn and agent-only networks

Excluded by the brief. Not implemented, not stubbed.

---

## Known quirks

Each of these cost real time. They are written down so they do not cost it
twice.

- **arXiv must be `https`.** The `http://` form returns HTTP 200 with a
  well-formed but *empty* feed — indistinguishable from "no new papers".
- **arXiv throttles with 406, not 429.** Under load it answers `406 Not
  Acceptable` on every request after the first in a batch, then recovers on
  its own. It is not a header problem and not a parameter problem: the same
  URL that 406s will return 200 a minute later. The fetcher backs off
  3→6→12→24s and runs first, before the Hacker News burst.
- **Hacker News: use `/search`, never `/search_by_date`.** The latter sorts
  chronologically and discards relevance ranking entirely, which returns
  the firehose filtered by date. With `/search_by_date` the top result for
  "clinical AI governance" was *"Mistral raises €3B"*.
- **Hacker News: set `typoTolerance=false`.** Default tolerance turned 11
  real hits into 119 mostly-unrelated ones.
- **LessWrong's GraphQL endpoint times out on a cold first call** and then
  answers in under a second. It gets 2 retries.

### One thing still unproven

**arXiv has not been observed succeeding inside a full run from this
sandbox.** It works in isolation — 12 consecutive requests returned 200 —
but every full run from here failed it, because this environment shares an
outbound proxy IP that arXiv had already rate-limited from the testing.
The code is believed correct and the backoff is generous, but *believed
correct is not proven*. The first Lambda run, from its own IP making three
requests a day, will settle it. The digest names failed sources explicitly,
so a silent arXiv failure cannot hide.

---

## The hash chain

Every new hit appends an entry carrying the sha256 of the entry before it.
Altering or removing any entry breaks every hash after it.

```
python3 scripts/verify_chain.py --table jarvis-scout --profile openclaw
python3 scripts/verify_chain.py --file .local/chain.jsonl
```

The entry and the head pointer move in one DynamoDB transaction, both
conditional, so two concurrent runs cannot fork the chain. The runtime role
is granted `GetItem`, `PutItem`, `Query` and `TransactWriteItems` — but
deliberately **not** `DeleteItem` or `UpdateItem`. The scout appends; it has
no reason to be able to rewrite a chain entry, so it cannot.

**This is not the Glass Ledger.** It is hash-chained but not signed, and
not witnessed off-box. It proves the log has not been edited since it was
written. It does not prove who wrote it, and it does not stop whoever
controls the table from discarding the whole thing and starting fresh. For
a scout that reads public APIs and sends an email, that is the right amount
of machinery. Do not cite it as an implementation of the Glass Ledger.

---

## Security

- **Everything fetched is untrusted data.** It is never executed and never
  interpolated into a command or query. `sources/sanitise()` is the single
  choke point: it strips control characters, removes zero-width and
  bidirectional marks, and caps length. Non-http(s) URLs are dropped, so a
  `javascript:` link cannot reach the digest. The digest HTML escapes every
  field.
- `sanitise()` does **not** attempt to detect prompt injection — that is not
  a solvable filtering problem. The defence is structural: Stage 1 calls no
  model, and Stage 2 must pass fetched text inside a delimited block
  explicitly marked as data.
- The scout runs under `jarvis-scout-role`, which can write to its own
  table and send from two named addresses. Nothing else. No Secrets
  Manager, no other table, no model service, no role assumption.
- No access to `heartbeat-memory`. No personal data. No posting credentials.

---

## SES sandbox

SES on this account is **in sandbox** (`ProductionAccessEnabled: false`),
which means both sender and recipient must be verified identities.

As of 21 September 2026 the only verified identity is
`Paulbla30@hotmail.com`. **`paulblatherwick@heartbeat-framework.org` is not
verified**, so the digest cannot reach it yet. A verification email has been
requested for that address; it routes through Cloudflare Email Routing into
Paul's Gmail. Click the link, and the digest goes to the right place.

Until then `fallback_enabled = true` sends to the hotmail address instead,
and marks the subject line `[fallback address]` so it is never silent.
Set `fallback_enabled = false` once verification is done, so a broken
recipient fails loudly rather than quietly going somewhere else.

Sandbox is sufficient here: 200 emails/day against a need of one.

---

## Layout

```
config.toml            keywords, weights, threshold — edit this, not the code
template.yaml          SAM: table, function, least-privilege role, schedule
src/app.py             the run: collect → score → store → chain → digest
src/scoring.py         the rules
src/chain.py           hash chain, DynamoDB and local backends
src/store.py           hit storage and de-duplication
src/digest.py          the email
src/sources/           one module per source; sanitise() lives here
scripts/run_local.py   rehearse against the live APIs, write nothing
scripts/verify_chain.py
scripts/deploy.py      package → S3 → CloudFormation
tests/
```

## Why SAM, not CDK

SAM, deployed through plain CloudFormation.

A SAM template is a CloudFormation template with a transform, and
CloudFormation processes that transform server-side — so `scripts/deploy.py`
can zip the source, put it in S3 and hand over the template without the SAM
CLI or Docker being installed. CDK would need a bootstrap stack, an S3
bucket and an ECR repository created first, which is more standing estate
than this deserves.

SAM also earns its place on the schedule: `ScheduleV2` gives EventBridge
Scheduler with `ScheduleExpressionTimezone: Europe/London`, so 07:00 stays
07:00 across the BST/GMT change. A plain cron rule is UTC-only and would
silently drift by an hour twice a year.

There are no binary dependencies — the function is pure Python using only
the standard library plus the boto3 already in the Lambda runtime — so
there is no build step to containerise.

## Deploy

```
python3 scripts/deploy.py --profile openclaw --dry-run   # package only
python3 scripts/deploy.py --profile openclaw             # for real
```

Pause without deleting anything:

```
aws cloudformation update-stack --stack-name jarvis-scout \
  --parameters ParameterKey=Enabled,ParameterValue=false ...
```

The table is `DeletionPolicy: Retain`. Deleting the stack leaves the chain
intact, on purpose.

---

## Stage 2

Not started, and not to be started until Paul says so.

Drafting needs an instruction-tuned model. Qwen3.5-9B-Base is a base model
and will not draft usably. The options and their costs go to Paul as a
decision, not a recommendation acted on.
