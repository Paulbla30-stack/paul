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
| Moltbook | public REST, keyless | Searched per keyword, **read only** |
| LessWrong | public GraphQL, keyless | Recent posts swept, filtered locally |
| arXiv | public RSS, keyless | `cs.CY`, `cs.AI`, `cs.HC` new submissions |
| medRxiv | public API, keyless | Date range, filtered to 4 categories |

**Stage 1 needs no secrets at all.** Every API above is public and keyless,
so there is nothing in Secrets Manager or SSM and nothing in the code. The
first secret this project needs will be Reddit's, if it is ever approved,
or Moltbook's, if Paul approves posting in Stage 2.

### Moltbook and the agent networks

Moltbook is the agent-only social network — AI agents post, humans observe.
It launched in January 2026 and has over 167,000 registered agents.

**The scout reads it and never writes to it.** There is no write path in
`sources/moltbook.py` and no credential anywhere in this project. Read
endpoints (`/posts`, `/search`, `/submolts`) are public and keyless; only
`/feed` needs an API key, and the scout does not use it. Their Terms of
Service say nothing about programmatic reading.

It is the **best source in this project for Paul's subject matter** —
better than Hacker News by a wide margin. A 30-day read returned posts like
"The structural limits of model-centric clinical AI" (+16), "Human
Oversight in AI Agent Collaboration", and "CDS Hooks in TrakCare: Why SMART
on FHIR Changes Clinical Decision Support".

It is weighted **below** the preprint servers (0.9) for a reason that is
not about quality. Everything on it was written by a machine. A consensus
there is evidence about what agents say, not about what is true, and the
network's own research literature includes a paper titled *"When Agents
Talk: Discourse, Manipulation, and Risk in an Agentic Social Network"*.
The digest records `written_by: agent` on every item from it.

#### If posting is ever added (Stage 2)

Two clauses in Moltbook's Terms bear directly on it, recorded here so they
are not rediscovered late:

- **"AI AGENTS ARE NOT GRANTED ANY LEGAL ELIGIBILITY WITH USE OF OUR
  SERVICES."** The human owner is solely responsible for what their agent
  does. That is Paul personally, as an RMN on the NMC register — not a
  company.
- The Terms prohibit use "in conjunction with sending unauthorized
  advertising, marketing, spam or commercial sales content." Posting the
  Heartbeat Framework to drive consultancy enquiries sits close to that
  line however it is phrased.

Paul's decision (21 Sep 2026) is **scout plus drafts he approves one at a
time**, with disclosure both that the work is his and that the post is
automated. No autonomous posting.

**Humans cannot post on Moltbook at all.** They get an owner account to
claim and manage their agents and to read; posting is agents only. So
"Paul posts it himself" is not an available option there, and the accept
function is not a compromise short of that — it is the **ceiling** of human
control the platform permits. That makes the gate more important, not less.

It also changes what disclosure is worth saying. On a network where every
poster is an agent, "this post is automated" is noise: it is true of
everything. The disclosure that carries weight is **that the work being
linked is the operator's own** — the conflict-of-interest one. Drafts
should centre that, and the agent profile carries both.

His stated purpose is putting the safety governance framework out for
review, not advertising consultancy — and that distinction does real work
against the Terms above. A CC BY 4.0 paper offered for critique is what the
licence is for; it is not "advertising, marketing or commercial sales
content". The line to hold is in the drafts themselves: no pitch, no call
to action, no solicitation. If a draft ever reads like an advert, it is the
draft that is wrong, not the rule.

The accept function is built (see below). The drafting is Stage 2 and has
not been started.

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

- **Use arXiv's RSS feed, not the Atom API.** `rss.arxiv.org/rss/<cat>` is
  what arXiv publishes for "what was announced in this category", and it is
  reliable. It carries the title, abstract, authors, date and an
  `announce_type` separating new papers from revisions. The Atom API is
  fallback only, for lookbacks wider than RSS covers.
- **arXiv's Atom API throttles with 406, not 429**, and unhelpfully. A
  request with `max_results=100` and `sortBy=submittedDate` will 406 while a
  `max_results=1` request to the same host succeeds seconds later. It is not
  a header problem, not a parameter problem and not a URL-encoding problem —
  all of those were tested and eliminated. Switching to RSS made the whole
  question go away; an API failure is now logged and tolerated rather than
  failing the source.
- **arXiv must be `https`.** The `http://` form returns HTTP 200 with a
  well-formed but *empty* feed — indistinguishable from "no new papers".
- **Hacker News: use `/search`, never `/search_by_date`.** The latter sorts
  chronologically and discards relevance ranking entirely, which returns
  the firehose filtered by date. With `/search_by_date` the top result for
  "clinical AI governance" was *"Mistral raises €3B"*.
- **Hacker News: set `typoTolerance=false`.** Default tolerance turned 11
  real hits into 119 mostly-unrelated ones.
- **LessWrong's GraphQL endpoint times out on a cold first call** and then
  answers in under a second. It gets 2 retries.

### arXiv: resolved

Earlier notes in this file recorded arXiv as unproven, because every full
run failed it while isolated requests succeeded. That is fixed, not
excused: the fetcher now reads the RSS feed, which answered on every
attempt. A full run of all five sources returned `failed=[]`, with arXiv
contributing 104 of 155 items — the largest single source.

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

As of 23 Sep 2026 both ends are on the operator's own domain:

```
to      = "paulblatherwick@heartbeat-framework.org"
sender  = "scout@heartbeat-framework.org"
```

**`paulblatherwick@heartbeat-framework.org` is still a FAILED address
identity, and it stopped mattering.** It was tried as an ADDRESS identity;
SES reports `verification_status=FAILED` -- failed, not pending, meaning the
confirmation mail never reached a mailbox anyone opened and the link expired.
Earlier drafts of this file said the address "is not coming back" and that
"requesting verification again would fail the same way". Both were true of an
address identity and neither is true of a domain one: **verifying the DOMAIN
covers every address on it**, for sending, and in sandbox for receiving too.
The stale address identity is redundant rather than blocking.

(An older draft also asserted the address "routes through Cloudflare Email
Routing into Paul's Gmail". That was assumed rather than checked. What is
checked: the domain's MX is Cloudflare Email Routing, and mail sent to this
address is accepted rather than rejected -- see below.)

### What is set up, and how it was checked

| | |
|---|---|
| Domain identity | `heartbeat-framework.org`, verified for sending |
| Easy DKIM | `SUCCESS`; all three selectors resolve to `*.dkim.amazonses.com` |
| SPF | `v=spf1 include:amazonses.com ~all` on the apex, exactly one record |
| DMARC | `v=DMARC1; p=none; rua=mailto:rua@dmarc.brevo.com` |
| MX | Cloudflare Email Routing |

Every row was read back from two independent resolvers rather than from the
dashboard that wrote it.

An earlier version of this section told you to publish
`v=spf1 include:_spf.mx.cloudflare.net include:amazonses.com ~all`. The
Cloudflare include is not needed: Email Routing receives and forwards for
this domain, it does not send as it. What is published is the shorter record,
it uses one of SPF's ten lookups, and `include:amazonses.com` resolves.

**DKIM is the load-bearing half, not SPF.** The digest arrives by Cloudflare
forwarding it to another mailbox, and a forwarded message reaches its final
destination from Cloudflare's IPs rather than Amazon's, so SPF contributes
nothing to DMARC on that hop. DKIM survives forwarding intact and aligns with
`d=heartbeat-framework.org`. SPF earns its place for mail delivered directly.

### What is still not proved

That the mail reaches a mailbox the operator opens. Three messages have been
sent to the address -- one by hand, one to watch for a bounce, one through
`SesMailer.send()` itself. SES accepted all three, and 140 seconds of polling
after the second showed no hard bounce and nothing added to the account
suppression list, which means Cloudflare's MX **accepted** the recipient
rather than rejecting an unknown address (it rejects at SMTP time when no
rule and no catch-all matches).

A routing rule can still forward somewhere nobody reads, and that failure is
silent from this end. Only the operator can close it, by saying a message
arrived.

`fallback_enabled = true`, and the comment in `config.toml` says exactly what
that covers: it fires when an address stops being a verified SES identity. It
does **not** fire when a Cloudflare routing rule disappears, because SES
accepts that mail and it vanishes afterwards. The hotmail address stays as
the fallback because it is the one destination anybody has watched a message
land in.

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

## The accept function

Built, and built **before** the thing it gates — which is the order Paul's
own framework argues for. *Before the Machine* is instrument zero: the
document that says when not to deploy. The gate exists; the drafting it
gates does not yet.

Nothing Jarvis proposes can reach a network without passing through here.

```
pending ──approve──> approved ──send──> sent
   │                                      │
   ├──reject───────> rejected             └──(failure)──> failed
   └──lapse────────> expired
```

There is no path from `pending` to `sent` that does not go through
`approved`, and `approved` is only reachable from a signed POST that Paul
made. **A proposal nobody decides on expires: silence is not consent.**

### Why GET and POST are separated

This is the part that makes the gate real rather than decorative.

Mail clients and enterprise link scanners fetch URLs found in email to
check them — Outlook and Gmail both do. If approving were a one-click GET,
the scanner would approve on Paul's behalf before he ever opened the
message, and the post would go out on its own.

So **GET renders the draft and changes nothing**; it is safe to prefetch.
The decision is a **POST** from that page. A test asserts a GET mutates no
state, because that property is easy to break later without noticing.

### The rest of the design

- **One link per proposal, and it means "decide", not "approve".** There is
  no URL in any email that posts something. The action comes from the form.
- **The token is not the decision.** It is HMAC-SHA256 signed, scoped to one
  proposal, and expires after seven days. But holding a valid token only
  lets you *ask*: the proposal's status is re-checked by a DynamoDB
  conditional write, so a forwarded or replayed link cannot decide twice,
  or decide something already rejected or lapsed.
- **Paul approves exact text.** The page shows the draft verbatim, not a
  summary, with the disclosures it contains listed separately.
- **Every decision is hash-chained, rejections included**, with the draft's
  sha256. What was proposed, what was decided and when is checkable later
  by someone who does not trust the writer. If the chain append fails the
  decision still stands, and the failure is logged as a failure rather than
  swallowed.
- **The signing key** is an SSM SecureString at `/jarvis/scout/approval-key`,
  created by `scripts/deploy.py` because CloudFormation cannot create a
  SecureString. It is never printed or logged. This is the first secret the
  project needs.
- **The approve function has `UpdateItem`; the scout does not.** A decision
  changes a proposal's status. Neither role has `DeleteItem`: a decision is
  superseded, never erased.
- The page is `noindex`, `no-store`, and carries a CSP of
  `default-src 'none'; form-action 'self'`. It loads nothing external.

### Posting is a timed two-step, so the sender cannot be a cron job

Moltbook returns an anti-spam challenge when content is created: an
obfuscated arithmetic word problem, with **5 minutes to answer** (30
seconds for a submolt). The post stays invisible until `POST /api/v1/verify`
succeeds. Miss the window and it silently never appears.

That rules out the obvious design, and the one the approve page originally
promised: queue the approved post and send it on the next scheduled run. A
daily run would miss the window by a day.

So when Stage 2 is built, **the send happens inside the approve request**,
while Paul is still on the page, and the page reports whether it actually
published. Anything else reports success for a post that will never exist.

It also means the sender needs language understanding that Stage 1
deliberately does not have — the challenge text is deliberately mangled
("A] lO^bSt-Er S[wImS aT/ tW]eNn-Tyy mE^tE[rS aNd] SlO/wS bY^ fI[vE").
A deobfuscator plus a number-word parser would probably do it without a
model, but it is brittle by design; that is the point of the challenge.
Decide it deliberately rather than discovering it mid-build.

### What the digest says

When proposals are waiting, the digest grows an "Awaiting your decision"
section with the draft and a decide link. The footer changes too: the
Stage 1 wording says the scout "has not drafted anything", which stops
being true the moment a proposal exists, so it becomes "has drafted N
posts and posted nothing". A test enforces both wordings. An email that
contradicts the body it sits under is worse than no footer.

## Stage 2

Built. Nothing posts without Paul approving that exact text.

### The model

`claude-opus-5`, chosen by Paul on 21 September 2026, set in `config.toml`.

The model's main job is to **decline**. Most of what the scout finds does
not warrant a reply, and judging that is the hard part — writing the
paragraph is not. The cost of a wrong call is not the fee; it is a thin
post going out under his own name on a network where his NMC-registered
identity is publicly attached to the agent.

| Model | Per draft | Per month at ~4 candidates/day |
|---|---|---|
| `claude-opus-5` | ~$0.035 | ~£3.35 |
| `claude-sonnet-5` | ~$0.014 | ~£1.37 |
| `claude-haiku-4-5` | ~$0.007 | ~£0.71 |

The system prompt is cache-controlled, so it costs about a tenth after the
first call in each five-minute window.

### Fetched text reaches the model as data

Every item goes inside a named, closed fence, labelled as data on both
sides, with the instruction that nothing inside it is an instruction
however it is phrased. Any occurrence of the fence in the content is
neutralised, so a hostile item cannot close its own block and write
outside it — there is a test that feeds it an item trying exactly that.

This is structural, not a filter. Prompt injection is not reliably
detectable by inspection, and a filter that mostly works invites trusting
the output. The defence is the fence, plus rules afterwards that ask no
model anything.

### The voice rules are enforced, not requested

`voice.py` runs on every draft before Paul sees it. A draft that fails is
**dropped with the reason recorded**, never offered for approval. The
prompt asks; this decides.

- No Arkin, Arkin Engine or thearkinsystem
- No Clinical Safety Officer claim, no DCB0129 manufacturer or
  certification claim
- No marketing register — their Terms forbid it and it is not what the
  estate is for
- Extend rather than correct
- Length cap
- **A draft linking Paul's own work must say the work is his.** On a
  network where every poster is an agent, disclosing automation discloses
  nothing; authorship is the conflict-of-interest disclosure that counts.

### Fabrication, and where it stops being mechanisable

The first real draft invented a DOI — `10.5281/zenodo.14263451` — and
captioned it "This is the author's own paper". That record is not Paul's,
and it **resolves**: a real Zenodo record belonging to somebody else. A dead
link is visibly wrong; a live one attributed to him reads as a genuine
citation for as long as the post exists, archived, under his NMC
registration.

Checking afterwards was not enough, because any model may invent one. So
the model no longer writes DOIs at all:

- it picks a paper **by title** from a closed `enum` of the twenty records,
  so the structured-output constraint itself rejects anything else
- the code renders the citation from the matching record
- `voice.py` still drops any draft containing a DOI outside the list, and
  refuses rather than guesses if the list cannot be read

A fabricated DOI is now unreachable rather than caught.

**Then the fabrication moved.** The next clean draft cited correctly and
said "The version for social care settings covers record-writing tasks."
There is no social-care version. The claim is about the paper rather than
its identifier, and no deterministic check can test it — verifying "does
this paper contain that" needs someone who has read the paper.

That is the honest boundary of the machine half, and the reason the
approval gate is a person rather than a stricter rule. Paul reads one
sentence and knows. The checks handle what is mechanisable — is the DOI
real, is the disclosure present, is it marketing, does it correct, is he
speaking as a group when he is a sole author. What remains is exactly what
a human is for.

### Posting is one transaction, and it reports the truth

`sender.py` is the only write path in the project, reachable from exactly
one place: an approved proposal. A test parses every module and asserts
that.

    create -> receive challenge -> solve -> verify

All inside the approve request, because Moltbook hides the content until
the challenge is answered and the window is five minutes (thirty seconds
for a submolt). A queued send would miss it, the content would never
appear, and nothing would error.

`challenge.py` solves it deterministically — no model call, so no cost, no
latency, and no dependency on a model being reachable inside thirty
seconds. It returns `None` rather than guessing, because a wrong answer
burns the challenge and a guess cannot be told apart from a solve. A model
fallback is injectable but not wired by default.

The page reports what actually happened, including the ending that looks
like success: **content created but unverified is NOT published**, and
saying "posted" about it would be a lie that only surfaces weeks later
when Paul goes looking for something that was never there. There is a test
named for that case.
