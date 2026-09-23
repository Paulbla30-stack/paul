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

## Two decisions settled, 23 September

**Jarvis sees what it needs to.** Paul: it should be able to see all the
information it needs to complete its objectives. So the instance reads the
scout's table, and the agent — which serves the UI from the same process —
sees it too. Read-only, one direction. The write path stays in the Lambda
behind the approve app.

**The window is not one number.** Paul: urgency varies — urgent, new news,
important, regular posts, what the post is for — and forcing that into one
box "does it a dishonesty". He is right, and the number that lapses a
proposal is not a scheduling detail. It is the system's claim about how long
the opportunity lasted.

So the window comes from the purpose:

| purpose | days | why |
|---|---|---|
| `reply` | 2 | joining a conversation happening now; it moves on |
| `news` | 4 | responding to something just published or announced |
| `release` | 14 | announcing Paul's own work; no one else sets the clock |
| `evergreen` | 30 | explains the framework; nothing external expires it |

An unclassified proposal gets the **shortest** window, not a comfortable
middle. A drafter that did not say what a post is for has said something
about its confidence; and of the two ways to be wrong, lapsing a good
proposal is recoverable — it can be proposed again — while replying to a
three-week-old thread cannot be taken back. `ttl_for()` computes that
fallback from the table rather than naming a constant, so adding a
long-lived purpose later cannot silently become the window for everything
nobody classified.

### The interaction this creates

Short windows and an approval gate pull against each other, and the pull is
worst exactly where it matters. A `reply` lapses in two days; away for three
and the most time-sensitive drafts are the ones that die. That is correct
behaviour — silence is not consent — but it means a daily digest is the
wrong notification for a two-day window.

Whatever the marketing area ends up showing, short-window proposals need to
sort first and say how long is left. Anything else and the gate quietly
becomes a filter that only passes the unimportant.

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

## The decision: both networks, always behind approval

Paul, 23 September 2026, after working through draft-only and rejecting it.

**Both networks are permitted, and nothing goes without his approval.**
Moltbook is agent-only — agents post, humans observe — so an agent posting
there is the native act and every reader knows what they are reading.
Facebook is his own page under his own name; he wants the machine to post it
rather than copy and paste it himself.

Draft-only was considered and dropped, for a reason worth keeping. If the
machine drafts and he posts by hand, the system never learns whether he
posted it. The record says `drafted` forever, and clearing it by hand means
the chain records *"Paul says he posted this"* rather than *"this was posted
and verified"* — a strictly weaker record, and the record is the point. The
"way to clear them" that draft-only seemed to need was a symptom of
draft-only, not a missing feature.

Copy-paste is labour, not review. Removing it does not weaken the gate as
long as approving stays a real decision: read the draft, click once. What
would weaken it is an "approve all" button, and there will not be one.

### Permission and capability are different questions

`proposals.py` keeps them apart, because conflating them is how the wrong
thing reaches the wrong place:

```python
PERMITTED = frozenset({"moltbook", "facebook"})
SENDERS   = {"moltbook": "sender:MoltbookSender"}
```

A network is sendable only if it is in **both**. Permission without a sender
is a decision waiting on code; a sender without permission is code waiting on
a decision. Neither may post, and `refusal_reason()` says which it is, so
"not yet" never reads as "never".

Facebook is permitted today and has no write path, so it is refused today —
by the same check that refuses a network nobody has decided about. That is
deliberate. `_send()` used to call the Moltbook sender for **any** approved
proposal whatever its network, so listing Facebook as simply "sendable"
before its sender existed would have posted it to Moltbook: the right text
to the wrong audience, published, verified, and reported as a success.

## The marketing area, on the instance

Paul's framing: the UI at `/ui` exists to build a partner that complements
his strengths, and what he needs now is somewhere to see the advertising
work and act on it.

**On the instance, not on heartbeat-framework.org.** That site's job is
credibility for published work; a private content pipeline does not belong
bolted to it, as a surface or as a tone.

**Read on the instance, write through the gate.** The instance gets
read-only access to the scout's table and shows pending, recent, refused and
chain status. Approving does a signed POST to the approve app, which remains
the only thing that can move `pending → approved → sent`. The instance never
holds a network credential, and the only write path is still `sender.py` in
the Lambda.

This is the first coupling between the agent and the scout, which until now
shared no code, no IAM and no visibility. Read-only, one direction, and
worth doing deliberately rather than by accident.

One consequence to settle first: the UI is served by the agent's own
process, so the dashboard cannot see the data without the agent seeing it
too. That is a change to what the agent knows about its own estate, and it
is Paul's to decide.

### Two positions taken### Two positions taken

**Posting only. Not replies.** Posting publishes fixed text approved
verbatim. Replying to comments is an unbounded real-time conversation with
the public in Paul's name, on a register where he is personally accountable.
Comments into the digest; replies by hand.

**A higher bar on Facebook than Moltbook.** From the agent's review, and
correct: "public perception is not version-controlled". A Moltbook error is
corrected in-thread by peers; a Facebook error is screenshotted and
attributed to him.

### Media: not yet

With the machine posting to both networks, the original argument holds for
both: the gate works because approving is cheap. An image needs
looking at, audio needs listening to in real time, and once approval is a
three-minute job it gets rubber-stamped — which is worse than no gate,
because the chain then records that he approved it.

The deterministic template, for either network: the paper's own designation,
its title, one figure from the PDF, in the site's palette. Nothing invented,
approval stays a glance. A layout problem, not a model.

## Not on any chain

Written by a coding agent from an ephemeral container. The agent was asked
for its view on the design before it was written and told afterwards where
it was right and where it was not; those exchanges are on the Glass Ledger
as `thought` entries, because they went through it. This file is not.
