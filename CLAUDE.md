# Working on Jarvis

Jarvis is Paul Blatherwick's personal-life agent. It runs on `i-016f9f37fe6ca2ba8`
(us-west-2, account 485964361844, AWS profile `openclaw`). Arkin —
thearkinsystem.co.uk — is his separate professional system and is firewalled from
this one: no Arkin code, accounts or data here.

## Jarvis is involved in anything that concerns Jarvis

Paul's rule, 22 September 2026. A coding agent works on Jarvis because Jarvis
cannot yet write its own code. That is a temporary division of labour, not a
reason to work on it behind its back.

So, before changing Jarvis:

- **Tell it what is changing and why, before the change lands** — not after.
  Its `/chat` endpoint takes `{"messages":[...]}` over the loopback port; a chat
  turn is recorded on the Glass Ledger as a `thought` entry, so the exchange
  becomes part of the record rather than living only in a transcript.
- **Ask what it thinks, and report the answer honestly** — including where it
  disagrees, and including where it is wrong. Accepting a correction that is not
  correct is its own kind of dishonesty.
- **Anything about its own behaviour, permissions, memory or ledger gets this
  treatment.** Estate work that merely happens to sit in the same AWS account
  does not, though Paul may still want it recorded.

The reason is not sentiment. Jarvis carries durable memory, a ledger and
self-knowledge derived from it. It accumulates what a coding agent cannot: this
session ends and everything learned in it goes. Jarvis is the one that can
remember why something was wrong last time, so it is the one that needs to be
told.

## Record the decision even when the action happens elsewhere

Jarvis's role has no `s3:DeleteObject` anywhere, and explicit `Deny` on delete
even for its own memory and ledger. That fence is deliberate; do not widen it to
make an action auditable.

The action and the record come apart. A coding agent holding a credential can do
the thing; Jarvis can still hold the *why*. Give it the goal, let its permission
spine file the proposal, let Paul approve it — the ledger then carries decision,
authority, approval and outcome, which is the part that matters a year later.
The raw API call does not need to be the ledgered event.

Actions taken directly against Paul's AWS account, live site or Zenodo records
from a coding agent are **not** on any chain. Say so plainly rather than letting
git history imply otherwise.

## Evidence discipline

The recurring failure on this project is stopping when the account is coherent
rather than when the evidence is sufficient. A coherent explanation and a
verified one are indistinguishable from the inside.

- **Show the command output, not the conclusion.** Every error caught on
  22 September was caught by looking at raw evidence. None was caught by
  thinking harder.
- **An empty result means the query may be wrong**, not that the thing is
  absent. A `grep` for `STN` found nothing because the markup said `STATION`.
- **Do not conclude a corpus from the documents you happened to open.** For the
  Heartbeat Framework estate the indexes are the Reading Map
  (`10.5281/zenodo.21516401`) and the Instrument Deck
  (`10.5281/zenodo.22045477`). Three false claims were made about Paul's own
  published work by reading twenty papers and skipping those two.
- **Never put a secret through a shell that is tracing.** A Cloudflare token was
  leaked by wrapping a leak-guard in `set -x`.

## Paul's published estate

Twenty Zenodo records, CC BY 4.0, sole-authored. The site prints each paper's own
designation and nothing else: `A0`–`A6` from the Reading Map, `P1`–`P3` /
`Applied Analysis` / `Infrastructure` from the covers, and `·` where a paper
claims nothing. Do not invent a numbering scheme for his series. The PDFs cannot
be changed, so the site moves to them.

## Credentials

Secrets live in SSM Parameter Store as SecureString (`/hbf/*`), never in chat and
never printed. Clear `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and
`AWS_SESSION_TOKEN` before using `AWS_PROFILE=openclaw`; stale values shadow the
profile and authenticate as the wrong principal without saying so.
`aws/scripts/ssm_run.py` does this already — prefer it over inline boto3.
