# Stage 2 is built and not connected — and what Facebook would need

23 September 2026. Written while designing a Facebook destination for the
Heartbeat Framework page, because the design ran into something more
important than the design.

## The finding

**The scout cannot currently propose anything.** The daily run reads,
scores, chains and emails. It never drafts and never proposes.

`run()` in `app.py` does, in order: collect → veto → de-duplicate → score →
append to chain → store the hit → build digest → send. There is no call to
`Drafter` anywhere in it, and no call to `Proposal.new` anywhere in `src/`.

Checked rather than assumed:

```
$ grep -rn "Drafter(" scout/src/ scout/tests/
scout/tests/test_stage2.py:261   out = Drafter(client=_FakeClient(payload)).draft(_item())
scout/tests/test_stage2.py:269   ...
(only tests — never in src/)

$ grep -rn "Proposal.new" scout/src/
(nothing)
```

The evidence agrees at the other end: `jarvis-scout-approve` has **0
invocations** and its log group has no events at all.

So the whole posting half exists, is tested, and is wired to nothing:

| Module | State |
|---|---|
| `drafting.py` — the model writes a draft | built, constructed only in tests |
| `voice.py` — deterministic checks on a draft | built, called by the drafter |
| `proposals.py` — the pending→approved→sent machine | built, used only by the approve app |
| `approve_app.py` — the Lambda that takes the decision | deployed, never invoked |
| `sender.py` — the only write path | built, never reached |

That is not a fault. Nothing has ever auto-drafted, so there is no risk
sitting live and no unreviewed text anywhere. But it does mean "add
Facebook to the posting pipeline" starts a step earlier than it looks.

## What went in tonight

**An efficacy rule in `voice.py`.** An assertion about clinical effect or a
change in practice must be attributed, or the draft is refused:

```
FAIL  This could change how we treat depression.
PASS  The authors propose a model for depression treatment.
PASS  The paper reports that it could change how we treat depression.
FAIL  Clinicians should adopt this now.
FAIL  Evidence shows this is effective for older adults.
PASS  The study finds that the tool reduces risk of harm.
```

The rule came from the agent reviewing this design. Its wording was "no
modality may introduce causal claims not explicitly in the source", which is
better than a per-medium judgement because one rule covers text, image and
audio at once.

The half of it that a regex can enforce is **attribution**. Whether the
source actually supports the claim needs the paper in hand, and that is the
operator's check at the gate. The code says so, and an attributed claim
passes with a note saying the operator still has to look. Claiming the rule
verifies against the source would be exactly the kind of "mostly holds"
guarantee `voice.py`'s own doctrine warns about.

14 tests, 125 in the scout suite.

## What connecting Stage 2 actually needs

Decisions, not much code:

1. **How many drafts per run.** Every draft is a model call with a cost, and
   every proposal competes for the operator's attention in one digest. A cap
   belongs here — without one, a flood of mediocre drafts buries a good one.
   `digest_max_items` caps the digest; nothing caps proposals, because
   nothing makes them yet.
2. **Which items qualify.** The digest threshold is calibrated for "worth
   reading". "Worth writing a public post about" is a higher bar and needs
   its own number.
3. **What happens to a draft that fails a voice rule.** Dropped with the
   reason recorded is the current design, and is right. Worth confirming the
   reason reaches the digest so a silently narrow pipeline is visible.

## Facebook, once Stage 2 runs

The gate machinery needs no change. `proposals.py` already anticipates a
second network (`network: str  # "moltbook" | …`).

**Search is closed, so Facebook is a destination only.** Public post data
needs App Review plus Business Verification; X is pay-per-read with no free
tier since February 2026; LinkedIn publishing is Partner Program only.
Scraping is against terms and not available to somebody posting under their
own name on a professional register. The open sources the scout already
reads — arXiv, medRxiv, LessWrong, Hacker News, Moltbook — are where the
subject is actually argued.

**Posting to a Page you administer needs Standard Access, not full App
Review.** App Review (Advanced Access) is the requirement for managing
*clients'* Pages. `pages_manage_posts` cannot be requested alone; it pulls
in `pages_read_engagement` and `pages_show_list`. Token lifecycle —
short-lived → long-lived user token → Page token — is the fiddly part and
should be confirmed against current Meta docs at build time, not from
memory.

### Two positions taken

**Posting only. Not replies.** Posting publishes fixed text approved
verbatim. Replying to comments is an unbounded real-time conversation with
the public in Paul's name, on a register where he is personally accountable.
Comments into the digest; replies by hand.

**A higher bar on Facebook than Moltbook.** From the agent's review, and
correct: "public perception is not version-controlled". A Moltbook error is
corrected in-thread by peers; a Facebook error is screenshotted and
attributed to him.

### Media: not yet

The gate works because approving is cheap — a draft read in ten seconds and
judged. An image needs looking at; audio needs listening to in real time.
Put both in every proposal and approval becomes a three-minute job, at which
point it gets rubber-stamped — and a rubber-stamped approval is worse than
no gate, because the chain then records that he approved it.

When visuals are wanted, the answer is a **deterministic template** — the
paper's own designation, its title, one figure from the PDF, in the site's
palette — not a generative model. Nothing is invented and approval stays a
glance. That is a layout problem.

## Not on any chain

Written by a coding agent from an ephemeral container. The agent was asked
for its view on the design before it was written and told afterwards where
it was right and where it was not; those exchanges are on the Glass Ledger
as `thought` entries, because they went through it. This file is not.
