# What the Jarvis design pack gives this platform

**Status: assessment, 18 September 2026.** The pack lives in `Paulbla30-stack/AI-projects` under `jarvis/`
(DESIGN.md, CONSTITUTION.md, PAPERS.md, spec/01–08, runbooks/phase-0 and incident; v0.1, 21 August 2026,
"awaiting Paul's review"). It designs "a personal AI colleague named Jarvis" on a VPS, on Paul's Claude
subscription, with Telegram as the front door. This repo is now that colleague's first real host: an
always-on AWS agent with an imported Qwen3-32B planner on Bedrock, a web UI, and the Glass Ledger
witnessed in S3. This document says what to carry across, what to adapt, and what to leave.

## The shape of the design, in this platform's terms

- **Constitution first.** A written character specification and two-sided contract, ratified by a
  DECISIONS.log entry, injected into every session by code, root-owned and immutable. Hard gates
  (Article 22) never relax: no credentials, no money, no permanent deletion, no external sends
  without approval, no patient-identifiable data, no editing its own governance, no self-granted
  exceptions.
- **Doorbell and shifts.** A dumb, model-free bridge runs around the clock; everything with a mind is
  a bounded shift that opens with a ritual (constitution, STATUS, last handover, autonomy register)
  and closes at an exchange zone (handover written, STATUS rewritten, memory proposals filed).
  Continuity lives in files, never in the transcript.
- **Permission spine.** Propose → approve → enact. A root-owned ceiling of denies; an ask tier that
  renders a card with the exact action and a one-line why, fails closed to deny after ten minutes
  but keeps the card as `held`; an allow tier only for classes ratified in an autonomy register,
  earned rung by rung (0 observer, 1 proposer, 2 standing approval, 3 scheduled) and demoted
  automatically by a tripwire. A hook arbitrates every call and any hook failure is a deny.
- **The ledger as the spine.** Every intent, call, approval, hold, override and shift boundary is a
  signed, chained entry written by a dedicated daemon under its own uid; no record, no action; the
  witness (verifier, pinned key, checkpoint pin) never lives with the writer; BROKEN is an incident,
  a torn tail is maintenance, and the two are never confused.
- **Memory by ritual.** Short-term dies with the shift; consolidation proposes facts as cards;
  long-term is human-readable markdown Paul can edit, guarded by a deterministic lint (secret
  shapes, PHI, verdicts about people, provenance tags); document-derived facts are propose-and-confirm
  forever; recall says "that is not in my notes" rather than inventing.
- **The Mine.** Documents pass a deterministic intake gate before anything indexes them: type
  allowlist, secret shapes hold, instruction-shaped text is flagged and framed as data, a PHI
  tripwire quarantines with a card that never carries the matched text.
- **Routines.** A 07:30 briefing, a Sunday review, nightly consolidation, watchdogs; deterministic
  first, model only on change; an alert budget with a tiny sacred tier; a routine that did not run
  is a headline, never silence.
- **Ops floor and drills.** Hardened host, cage and yard, no unit both holds a credential and runs
  the model, restic off-box, and a written incident drill (contain, log, tell, grade, fix, feed the
  register, demote) with rotation and seeded-fault drills on a calendar.

## Where it conflicts with the AWS build, and what to do

| Conflict | Design | This platform | Recommendation |
|---|---|---|---|
| Always-on vs episodic | Shifts with an open ritual and a close at the exchange zone | A 30-second loop that never closes | Keep the loop, add the *files* the shifts exist for: a git-versioned yard with STATUS.md, a daily handover at the Europe/London day boundary and on `/shift end`, read at the next day's first cycle as data. The idle back-off already makes the loop sip. |
| Claude subscription vs Bedrock | Sip the shared plan; report what it drank | Per-model-minute billing, hourly call budget | Keep the budget and idle back-off; add the honest daily usage note (calls, tokens, cooldowns) to STATUS and the briefing. No dollars, no percentages. |
| Telegram front door vs web UI | Model-free bridge with an allowlist, PIN ceremony, egress screen, chunking | TLS web UI with a token, no egress screen | The UI is the doorbell. Take the bridge's *properties*: egress secret screen on every outbound payload, deterministic commands (`/status`, `/verify`, `/pause`, `/resume`, `/shift end`), deny is free and approve costs ceremony. A Telegram transport can come later as an optional relay. |
| Root process holding everything | Cage and yard; ledger daemon under its own uid holding the only key; credentials never in a model-executing unit | One root process holds the signing key, the token and the planner | Phase it: first the fences in code (done for the token and API), then a non-root `jarvis` user with systemd hardening and `chattr`, then the ledger writer split into its own unit and uid with a socket. |

## Already here, by the pack's own rules

The Glass Ledger v2 deployed as published; the S3 Object Lock witness the instance can only append
to; the off-box audit with a pinned key and a local pin; fail-closed recording (no record, no
action, no answer); the brain never reads the ledger and the shell policy denies the paths; a
deny ceiling that is additive only; containment of a confused planner (no blind retry, cooldowns,
the repeat guard); idle sipping; a torn tail reported as maintenance and BROKEN as a break.

## Take now (hours to a couple of days each)

Items 1 to 5 were each checked by an independent reviewer against the code before this list was
settled; all five were confirmed missing and worth taking (their notes are folded in below). Items
6 to 12 rest on the author's reading of the pack and the code.


1. **Upload intake gate** (spec/07). Gate every `POST /upload` before the brain hears of it: magic-byte
   type allowlist (pdf, docx, md, txt, html), size sanity, sha256 dedupe, secret-shape screen → hold,
   instruction-shaped text → flag and frame as data, PHI tripwire (NHS mod-11, DOB-plus-name
   proximity) → quarantine with a card naming file, class and counts, never the text. Only gated
   files reach `uploaded_files`. Files: `jarvis/security/shapes.py`, `jarvis/security/intake.py`,
   `cloud/headless.py` upload path, `agent/core.py record_upload`, a Files-tab card in the UI.
2. **Egress secret screen and ledger redaction** (spec/02, 05). One `shapes.py` definition of
   secret-shaped (private-key blocks, token shapes, JWTs, high-entropy runs, password-like
   assignments, and the literal runner token, API key and ledger seed); redact-and-deliver on every
   outbound payload, block the whole message at four or more matches, ledger `alert` with the SHA-256
   of the span never the span, a screen failure fails closed with a fixed notice; the same screen
   over every ledger body before it is hashed.
3. **Pause lever** (spec/02, 03, Article 15). `POST /pause` writes `/var/lib/jarvis/PAUSED`; while it
   exists plan, act, think and chat refuse and the runner only observes; `POST /resume` requires a
   confirm phrase; both ledgered as gates; buttons in the UI; `systemctl stop` is the backstop.
4. **Persistent yard: STATUS.md, goals, notes and the daily handover** (spec/01, 03). Today goals
   added through the UI, brain notes and the uploads register die with the process. Write them under
   `/var/lib/jarvis/yard/` (git-versioned nightly), render STATUS.md (one screen, observations only,
   header with model id and versions), write `handover/YYYY-MM-DD.md` at Europe/London midnight and
   on `/shift end`, load the newest handover as data at start.
5. **Memory rules in code** (spec/06). Lint the brain's `note` before it enters notes or the ledger:
   secret shapes, PHI, verdict-shaped statements about named people; tag every note with its source
   (`conversation` or `document`); document-derived notes never quiet-save, they become proposals;
   long-term memory as `memory/*.md` Paul can edit from the UI, one fact per bullet with a
   provenance tag, loaded into context bounded; "that is not in my notes" when recall is empty.
6. **Governance files** (CONSTITUTION.md, spec/01). Install an adapted CONSTITUTION.md (draft,
   awaiting ratification) and inject its standing rules and hard gates into the system prompt by
   code; create DECISIONS.log with the founding entries this deployment forces (001 the name,
   002 AWS and Bedrock as the processor of all model traffic, 003 the S3 witness in the same
   account); record the return-address fields (prompt sha256, config sha256, model id, package and
   dependency versions, AMI id) in `agent_start` and STATUS; `chattr +i` the constitution and
   config at provision, `chattr +a` the ledger.
7. **Fence the agent off its own control plane** (spec/04, 05). Done in this branch: `/run/jarvis`,
   the loopback API and UI ports and bearer headers are denied; `replace_deny_patterns` is gone.
8. **Honesty and voice rules in the prompt, and a hold** (Articles 4, 5, 8, 9, 22). Observations
   never verdicts about people; say "that is not in my notes"; cite or don't assert; disagree
   plainly and state uncertainty; inform, don't decide, on money, health, people, law; the peer
   voice and the report skeleton (Decision needed / Recommendation / Why / Risks / Next steps); a
   `hold` field in the plan schema so an instruction that conflicts with the ceiling, Paul's
   included, is recorded as a gate and raised, never attempted.
9. **Heartbeat and alerts** (spec/08). `WatchdogSec` on the unit and `sd_notify` each cycle so a
   wedged loop restarts; ledger `alert` entries when the brain is disabled, the ledger fails, the
   anchor keeps failing or auth lockouts fire; a heartbeat file the briefing reads.
10. **Incident and drills runbook for AWS** (runbooks/incident.md). Contain (`/pause`, then SSM
    stop), log (off-box if the ledger is the casualty), tell, grade (Red, Amber, near-miss), fix to
    a return address, feed the register; BROKEN versus torn tail; rotation drills for the runner
    token, the ledger key (a new chain, both heads pinned), Bedrock and Hugging Face secrets, the
    TLS certificate; the pause rehearsal; seeded-fault drills with time-to-detect.
11. **Morning briefing** (spec/08). 07:30 Europe/London: deterministic gather (decisions needed,
    open items with ages, pending proposals, health and gaps including routines that did not run,
    yesterday's usage), one bounded `ask` only if something changed, one message under a character
    budget written to `reviews/briefing/`, shown in the UI; nothing-new mornings send two lines
    without the model.
12. **Override with honour and dyad vitals** (Articles 13, 14; spec/08). `POST /override`
    (what Jarvis said, what Paul did, why) and `POST /disagree` as ledger `dyad` entries with zero
    friction; `ledger_audit.py --report` printing trailing four-week override and disagreement
    counts, the double zero flagged, gate-time medians once cards exist.

## Take later (each a phase of its own)

- **P1 The permission spine** (spec/04): an ask tier for shell commands that write outside the yard,
  touch the network or packages; write-ahead cards under `yard/proposals/pending/`; Approve and Deny
  with a PIN in the UI; ten-minute timeout to `held`; approval enacts a fresh action only on a
  verbatim match within 24 hours; then `autonomy.json` classes at rungs 0–3, promotion after ten
  clean approvals by Paul's ratification, tripwire demotion. Days to a week.
- **P2 The unit sandbox law** (spec/01, 05): run the agent as an unprivileged `jarvis` user with
  `ProtectSystem=strict`, `ProtectHome`, `NoNewPrivileges`; a `jarvis-ledgerd` unit under its own
  uid, sole holder of the signing key via `LoadCredential`, taking submissions over a socket with
  HMAC'd approval events; the ledger file 0640 to that uid and `chattr +a`; a model-free gate unit
  owning the UI token and PIN. A week.
- **P3 The Mine and recall** (spec/06, 07): conversion (pymupdf4llm, mammoth, OCR fallback with the
  converted-but-empty flag), collections with provenance, FTS5 plus a local embedding index,
  `jarvis-recall` with the citation format and the collection's own confession travelling with it.
  Days to a week after P1.
- **P4 The routine set and alert budget** (spec/08): weekly review with vitals and promotion drafts,
  nightly consolidation with the lint sweep and git commit, watchdogs with two-probe confirmation,
  PPV per alert type, the harm signature escalating to a named human. Days, after the briefing.
- **P5 The council gate** (DESIGN §8): before any external-send class exists, a review verdict from a
  genuinely different model or a human, structural not optional. Days, gated on P1.
- **P6 Outbound allowlist and backups** (spec/04, 08): security-group egress limited to Bedrock, S3,
  SSM and the package mirrors (VPC endpoints), nightly snapshots of the yard and ledger off the
  instance, a restore rehearsed once. Days.

## Leave, and why

- **Telegram bridge as specified**: the front door here is the web UI over TLS with a token; a
  Telegram relay can be added later as a transport, but its 20 MB upload limit, processor decision
  and PIN-over-chat are Telegram's problems, not this platform's.
- **Claude Code shifts via the Agent SDK and `claude setup-token`**: the planner here is an imported
  open-weight model on Bedrock under the instance role; the shift *files* transfer, the SDK
  plumbing does not.
- **WSL2 rehearsal and the VPS Day-1 ordering**: replaced by Packer plus Terraform, which already
  build and verify the host; the runbook's *checks* (findmnt for chattr, the cage holds, the smoke
  test) become the provision self-test.
- **The Arkin watchdog over SSH**: standing consent 003 was ratified for a host that no longer
  exists; nothing here should reach into the Arkin box until Paul re-ratifies it for this host.
- **Nothing model-side should ever read `docs/` witness notes, pins or audit copies**: the audit
  material stays in `build/` on the operator's machine, which the repo ignores.

## Naming

The pack is unambiguous: "a personal AI colleague named Jarvis", "explicitly firewalled from ARKIN
and kept strictly separate from Arkin Engine Ltd", "not the movie". The platform is now Jarvis in
package, paths, units, tags, buckets and UI. Two constraints carry: nothing here is Arkin's, and the
voice is a peer's, capable, calm and direct.

## Decisions the pack leaves to Paul, as this platform now forces them

1. **The processor.** AWS and Bedrock carry all model traffic and the model runs in Paul's own account
   under an instance role. Ratify as DECISIONS.log 002, or name the alternative.
2. **The witness.** The S3 Object Lock bucket is in the same AWS account as the writer; the paper asks
   for a different party where the stakes justify it. Ratify same-account for now, with a second copy
   to another account or an RFC 3161 timestamp as the named next step.
3. **The front door.** Web UI over TLS with a token (and, later, a PIN for approvals) versus a
   Telegram relay. The UI is the default.
4. **The day boundary.** Europe/London midnight for the handover, with `/shift end` on demand; and
   whether an idle close (60 minutes with nothing to do) should also write one.
5. **The alert channel.** Where sacred-tier alerts go when the UI is not open: SNS to email or SMS
   is the AWS-native answer; the alert budget starts at two tiers.
6. **The named human** for the harm-signature route (DESIGN open decision 3); until named, the
   finding goes to Paul carrying the marker that it is not allowed to stay inside the dyad.
7. **Memory residency.** The yard is git-versioned on the instance; whether `memory/` ever leaves the
   box (a mirror or backup) is a ratified decision, default no.
