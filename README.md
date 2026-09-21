# Jarvis Agent-First OS

Jarvis is an agentic agent that owns the machine it runs on. It ships in
two forms built from the same code:

- **Bootable ISO** for bare metal or a VM: the agent is PID 1 with full
  hardware access (screen, keyboard, mouse, memory, storage).
- **AWS AMI** for EC2: the agent is a headless systemd service that
  bootstraps itself from instance metadata and user data. See
  [`aws/README.md`](aws/README.md).

Both include the integrated security vulnerability scanner.

## Architecture

```
jarvis/
├── jarvis/           # Core agent software
│   ├── agent/          # Agentic AI core (planner, executor, memory)
│   ├── brain/          # LLM planner (Claude), credentials
│   ├── cloud/          # IMDS client, boot-time bootstrap, headless runner
│   ├── hardware/       # Hardware access layer (display, input, memory, storage)
│   ├── security/       # Vulnerability scanner and system hardening
│   └── ui/             # Console UI and dashboard
├── aws/                # AMI build (Packer), systemd units, Terraform launcher
├── scripts/            # ISO build scripts
├── config/             # Boot and system configuration
├── rootfs/             # Root filesystem overlay (+ config-aws.yaml profile)
└── tests/              # Test suite
```

## Features

- **Agentic Agent Core**: Autonomous task planning, execution, and memory
- **LLM Brain**: Claude plans each cycle from live observations, goals and
  recent results, and can act through deny-list-guarded shell commands;
  the rule planner takes over whenever the model is unavailable
- **Full Hardware Access**: Direct access to screen, keyboard, mouse, video memory, RAM, storage
- **Security Scanner**: Integrated vulnerability detection and system hardening
- **Bootable ISO**: Standalone environment bootable from USB/CD/VM
- **AWS AMI**: Agent-first EC2 image; goals arrive via user data or tags,
  status via `jarvis --status` or a loopback HTTP endpoint
- **Glass Ledger**: a signed, hash-chained, append-only journal of every decision, action and outcome that the agent cannot rewrite, verified off-box with a pinned public key ([The Glass Ledger v2](https://doi.org/10.5281/zenodo.21515861)); see `aws/README.md`
- **The Vigil**: the agent sleeps. An imported model bills per minute a copy is
  warm, not per call, so the cost of thinking is the length of the silences
  between thoughts. While asleep the loop keeps running on the rule planner,
  which costs nothing and still observes, records and acts; the model is woken
  by the operator, by a material change in observations, or on a heartbeat,
  and does its thinking in one burst before going quiet again. Sleep removes
  the model, not the agent. See `jarvis/agent/vigil.py`
- **The Diary**: a register of what is coming. Reading a date off a page and
  keeping it are different jobs; this is the second one. A date found in a
  document is proposed and waits for the operator, never held on its own
  authority. A held message is not a delivered one, a moment missed while
  nothing was running says so, and a month of downtime is one message and a
  count rather than thirty. See `jarvis/agent/diary.py`
- **The Floor Test's nine vitals**: the estate's own instrument, turned on this
  deployment. Two of the nine name the two failures that actually happened
  here and none were computed. Built as counters now so a baseline exists the
  day it enters service; every reading says whether it means anything yet, and
  the agent is never shown them — signals, never targets. See
  `jarvis/agent/vitals.py`
- **Bearing**: everything else asks whether a thing is true; this asks what it
  costs to say it to somebody. The distinction is plain (about the world)
  against pointed (aimed at a person) — identical information, and the bill
  falls on whoever said it. It reports and never rewrites, never softens a
  claim, and never touches a finding, a refusal or the agent's own mistakes.
  See `jarvis/agent/bearing.py`
- **The Calendar**: the diary holds moments, this holds spans -- which is what
  makes a clash and a gap expressible at all. A collision is reported and
  never resolved; a gap is never called free time, because an empty calendar
  is a calendar with nothing in it and not a free day. It does not learn to
  speak: an appointment registers a commitment and the diary says it. See
  `jarvis/agent/schedule.py`

## Building the ISO

### Prerequisites

- Linux host with root access
- Required packages: `grub-mkrescue`, `xorriso`, `mtools`, `python3`
- At least 2GB free disk space

### Build

```bash
make iso          # Build the complete ISO
make rootfs       # Build only the root filesystem
make clean        # Clean build artifacts
make test         # Run tests
make scan         # Run security vulnerability scan
make headless     # Run the agent headless locally for a few cycles
make ami          # Build the AWS AMI with Packer (see aws/README.md)
```

### Quick Start

```bash
# Build the ISO
make iso

# Boot in QEMU for testing
make qemu

# Run security scan before building
make scan && make iso
```

## Booting

The ISO boots into a minimal Linux environment that automatically launches
the Jarvis agent. The agent has full access to:

| Resource     | Access Method          | Permission |
|-------------|------------------------|------------|
| Screen      | Framebuffer `/dev/fb0` | Read/Write |
| Keyboard    | `/dev/input/event*`    | Read       |
| Mouse       | `/dev/input/mice`      | Read       |
| Video Memory| `/dev/mem` + DRM       | Read/Write |
| RAM         | `/proc/meminfo`, mmap  | Read/Write |
| Storage     | Block devices `/dev/sd*`, `/dev/nvme*` | Read/Write |

## Building the AWS AMI

```bash
make ami-init                    # once: install the Packer amazon plugin
make ami AWS_REGION=eu-west-2    # build; AMI id lands in build/ami-manifest.json
```

Launch the AMI with an `jarvis:` block in the user data to hand the
agent its goals (example in `aws/cloud-init/user-data.example.yaml`), or
use the Terraform in `aws/terraform/`. Full details in
[`aws/README.md`](aws/README.md).

## LLM brain

With `llm.enabled: true` and an Anthropic API key, the agent's plan step is
made by Claude instead of the rule table. Each cycle the brain receives the
compacted observations, goals, pending tasks, the last ten task results and
its own notes, and returns one structured decision: a task to run (any
existing type, or a `shell_command` with the exact command line), an idle
signal, and any goals the evidence shows are complete.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
make headless CYCLES=5                      # cloud profile has llm.enabled: true
make ask Q="what is using the most memory?" # one-shot question, no loop
PYTHONPATH=. python3 -m jarvis.main --no-hardware --llm   # console: think / ask / brain / goals
```

Key points:

- **Provider**: `llm.provider: anthropic` (Claude via the Claude API) or
  `bedrock` (any Amazon Bedrock model, including your own imported weights,
  using the instance role and no API key). Both share the same loop, context
  and shell policy; see `aws/README.md` for the Bedrock setup.
- **Model**: `claude-opus-5` by default (`llm.model`, `--model`, `JARVIS_MODEL`).
  Adaptive thinking is on (`llm.thinking: adaptive` or `disabled`); `llm.effort`
  (default `medium`) sets how hard it thinks. `xhigh` and `max` need
  `llm.max_tokens` of at least 64000 and are clamped to `high` otherwise. Models
  without adaptive thinking (Haiku 4.5 and older) get neither thinking nor effort.
  Server-side refusal fallbacks are enabled so a declined request is re-run on
  Anthropic's recommended substitute; set `llm.fallbacks: false` to turn that off.
- **Cost control**: `llm.max_calls_per_hour` (default 60) is a hard budget and
  `llm.plan_every_n_cycles` thins the calls; beyond either the rule planner runs.
  The system block (prompt plus rendered shell policy and windows) is static and
  cached, so per-cycle cost is mostly the changing context, which is bounded:
  full output for the last three tasks, summaries for older ones.
- **Acting**: `llm.shell.enabled` lets the brain run commands as the agent's user.
  A built-in deny list blocks wiping or formatting disks, deleting system
  directories, rebooting, stopping the agent or the SSM, ssh or network
  services, piping downloads into an interpreter, touching credentials and
  flushing the firewall. Commands run in their own process group (a timeout kills
  everything they started) with credentials scrubbed from the environment.
  `llm.shell.deny_patterns` adds to the built-in list, which never shrinks by
  configuration: the agent's own runner token, status API, UI port and ledger
  are fenced off too. It is a guard rail, not a sandbox. Off by default and on
  in the AMI profile.
- **A failed brain task is not retried** by the agent: the brain sees the failure
  next cycle and decides. Goals given through user data, tags, `POST /goal` or the
  console are treated as instructions from the operator, so anyone who can set
  them can steer a root shell; protect those channels accordingly.
- **Credentials**: `ANTHROPIC_API_KEY`, then `llm.api_key`, then the 0600 file
  `llm.api_key_file`. On AWS the bootstrap fills that file at boot from AWS
  Secrets Manager (`llm.api_key_secret`) or SSM Parameter Store
  (`llm.api_key_ssm_parameter`) with the instance role. No key means the brain
  is disabled with a logged reason and the agent still runs.
- **Failure modes**: refusals, truncation, rate limits, network and API errors
  all fall back to the rule planner for that cycle; authentication failures
  disable the brain until restart. `jarvis --status` and `curl localhost:8471/brain`
  show what it last reasoned and why it is off, if it is.

The ISO profile leaves `llm.enabled` false; nothing changes there unless you
set it.

## Web UI

`cloud.ui.enabled: true` starts a second listener (default 0.0.0.0:8443,
TLS with a self-signed certificate) serving `/ui`: a chat with the agent,
an agent panel with goals, recent tasks and a "Think now" button, and file
uploads that land in `cloud.ui.upload_dir` and are handed to the brain as
`uploaded_files`, and a diary of what is coming. Login is the runner token.
The API behind it: `POST /chat` (multi-turn), `POST /upload` (raw body +
`X-Filename`), `GET /uploads`, `GET /diary` and `GET /schedule` with their
`POST` companions, plus everything the loopback status server offers.

## Behaviour lab

The **Lab** tab of the web UI puts every layer that moves the model away from
its base presentation on a dial: identity, the constitution's hard gates and
operating rules, the honesty rules, voice and report format, whether the
model is told about the ledger and about the shell deny-list, how much live
context it gets, how many recent tasks it sees, and the model parameters
(temperature, thinking, answer length). Level 0 on every dial is the bare
model: no system prompt, no context. The panel shows the exact system text
and context the model will receive, then runs a question under the dials and
under the base model side by side, or asks for a planning decision that is
returned but never executed. Layers enforced in code (the deny-list,
fail-closed recording, planner containment, the off-box witness) are listed
and locked: the lab changes what the model is told, never what the executor
allows. Every dial change and every run is ledgered with a fingerprint of
the settings, so a behaviour change always has a return address.

The lab is a **session**, and the agent is told when one is open. A lab
window fills the agent's own record with answers it did not choose, some of
them produced by the bare model rather than by it, and an agent reading
aggregates over that window has every reason to conclude something is wrong
with it. So one switch does both things and cannot do one without the other:
the notice is written, the model is woken and the window goes on the ledger,
and only then does the lab open. If the agent cannot be told, the lab stays
shut. Dials, preview and runs are all refused while the window is closed,
and closing it takes the dials back off live chat and says so. The agent is
told that it is being measured and not what is being measured -- the
settings stay out of the notice, or the measurement would be measuring the
telling. API: `GET /lab`, `POST /lab/session {open: true|false, purpose}`,
`POST /lab/settings`, `POST /lab/preview`, `POST /lab/run`.

## Filesystem grounding

A language model asked about a path it cannot see does not say "I don't
know": it produces a plausible one. Jarvis's planner once wrote a health
script that grepped `/var/log/jarvis/security_scan.log` and installed itself
into `/opt/jarvis`. Neither exists. Nothing in the context described the
filesystem, so every path in that script was a guess, and the script would
have reported zero security findings forever.

Three things close that, and all three are on by default:

- **The model is shown real paths.** Every cycle the context carries an
  `environment` block: the host's identity and a set of paths that have
  actually been checked, each with its kind, size, mode and modification
  time. Missing paths are listed *as missing* rather than left out, because
  an absence a model cannot see is one it invents something to fill. The set
  is `llm.environment_paths` (null uses the standard Jarvis paths).
- **The model can look.** `inspect_path` is a task type like any other: the
  planner puts an absolute path in `command` and gets back a real bounded
  directory listing, or a file's size and modification time, or the fact
  that the path does not exist. In an operator conversation, any path in the
  question is resolved the same way before the model answers, so a question
  about a directory is answered from a listing rather than a recollection.
  It reads only, and refuses the same paths the shell deny-list refuses: the
  ledger, its signing key, the agent's runtime directory, credentials. A
  read-only inspector that could read the ledger would just be a way around
  that fence.
- **Invented paths are caught.** Every absolute path the model names, in a
  planned command or in an answer, is checked against the filesystem after
  the fact. A planned command naming a path that does not exist is not
  blocked, because a command may legitimately create one, but it is logged,
  recorded in the ledger as an `unverified_path` alert, and fed back to the
  model on the next cycle. An answer naming one gets a `[path check]` line
  the operator can see. `inspect_path` is exempt: asking whether a path
  exists is the cure, not the disease.

The planner's rules say the same thing in words: paths are facts, not
conventions; name one only if it appears in `environment`, in
`uploaded_files`, or in a task result; and saying a path has not been
checked is a correct answer.

## Reading what it is given

The upload endpoint took a file, put it on disk and told the agent its name,
size and timestamp. Nothing could read a word of it. `read_file` does, behind
the same fence as everything else, bounded by bytes and lines, with binary
reported as binary rather than decoded -- a model shown mojibake describes it
confidently, which is this project's signature failure.

Plain text is not most of what a person sends, though. A bill, a statement, a
letter and a spreadsheet are all "binary", and answering every one of them
with "this looks like a binary file" makes the upload button decorative. So
`documents.py` reads Word, Excel and PowerPoint files -- all of them zips full
of XML, handled with the standard library, because a dependency here is a
dependency in every future image -- and PDFs through `pypdf`, which is the one
format the standard library cannot reach.

**The harder half is honesty about extraction.** A scanned PDF has no text
layer: every naive reader returns an empty string, and an empty string is
indistinguishable from a blank document. This codebase has met that failure
before, when an empty security scan report was read as a report of zero
findings. So every extraction carries `complete`, and when it is false it says
what was missed and why: *"no page in this PDF has a text layer, so it is very
likely a scan or photographs; nothing in it has been read and it is not
blank"*. A Word file says how many embedded images went unread. A spreadsheet
says its figures are the values last saved rather than recalculated formulas.
An image is identified, measured, and declined -- *"its contents are unknown,
not empty"* -- because pulling text from a photograph is OCR or a vision
model, and neither belongs behind a function called read.

Format comes from magic bytes before extension, because an extension is a
claim by whoever named the file and the first four bytes are a fact about it.

**Scans and photographs of documents** are read by recognising the characters
in the page image, through AWS Textract (`document_ocr`, and the Terraform
variable of the same name). It is reached only where extraction found nothing
to extract -- never a PDF that already read, because a text layer is the
author's own words and recognition is a guess at their shapes, and OCR of a
text PDF is slower, worse and billed. Recognised text is *marked* as
recognised, so a reader can weigh it differently.

Textract rather than tesseract because Amazon Linux 2023 does not package
tesseract at all, so the local route means a third-party repository or a pip
OCR stack with ONNX models on a small instance; this needs nothing installed
and bills per page rather than per warm minute, which after the imported-model
bill is a property chosen on purpose. Two things the operator is choosing when
switching it on: the page image leaves the box for Textract in the same
account and region, and each page detected is billed. It finds characters and
does not describe pictures -- a photograph of something that is not a document
comes back with no text and says its contents are still unknown, not empty.
Describing a scene is a vision model, which is a separate decision.

## The diary: keeping a date, not just reading one

`timesense.py` could already turn *"amount due 14 Oct"* into *"Wednesday 14
October 2026, 24 days from now"*, which is the difference between a string and
a fact. That was the end of it. The fact was true for one cycle, went into a
note, and nothing was ever going to happen on the fourteenth. Reading a date
and keeping it are different jobs, and only the first one was built.

`diary.py` is the second. It holds a small register of commitments -- a thing,
a moment, and when to speak about it -- and every cycle it asks which of them
have come round. When one has, the agent says so through the same channel it
uses for everything else, under the same rate limits.

Four things it is deliberately not.

**Not a scheduler for the agent's own work.** The planner already decides what
to do next. A commitment is about the operator's world: a bill, a renewal, an
appointment. The agent's effect is that it *says something*; it does not pay
the bill. The model is shown what is coming as context for what he may be
dealing with, with that stated plainly, because a model handed a list of dated
things will otherwise try to plan them.

**Not filled by inference.** A date found in a document becomes a
*suggestion* he rules on, never a standing commitment. `suggest_from_document`
is narrow on purpose -- a file he handed over, a future date, a word like
"due" or "expires" beside it, at most two from any one document -- because the
agent reads config files, logs and certificates too, and the commonest pair of
dates on a bill is its statement period, and a register that turns *"01 August to 31
August"* into two reminders is one he mutes. The first wrong reminder at 7am
teaches him to ignore the next right one.

**Not a cron.** Once, daily, weekly, monthly, yearly. Anything finer is a
scheduling language, and a scheduling language is a thing to get wrong
quietly. A monthly commitment anchored on the 31st lands on the 28th in
February, says that it was clamped, and rolls from the anchor rather than the
clamped date so it does not drift to the 28th for good.

**Not stored as instants.** "Nine in the morning" is a wall-clock reading in
his timezone, and an epoch computed once moves by an hour the next time the
clocks change. The local reading is kept and the instant is worked out fresh.
The timezone itself is declared once, beside the quiet hours it already
governs, and derived from there.

Three failures it is built against, all of them the register quietly lying
about what it did.

**A held message is not a delivered one.** The channel has a severity floor,
an hourly cap, a gap between messages and quiet hours, and `send` returns a
verdict rather than raising. Marking a reminder done because `send` returned
is how a 9am reminder silently becomes nothing at all. Only a message that
actually left marks its moment as spoken; a held one is retried, and one that
never lands is recorded as **unsaid**, which is a state he can see. Retried,
delivered, and never said are three outcomes, and the third one is the one a
naive register cannot express.

**A missed moment is said late, not said as if on time.** The register keeps
its own heartbeat, so *"nothing was running when this came round"* and *"the
channel held it"* are distinguishable rather than both becoming an awkward
silence. The first is worth apologising for and the second is worth
reporting.

**Catching up is not repeating.** A daily commitment and a month of downtime
is not thirty messages -- and it is not one message about a morning four weeks
gone, either. It is one message about *this* morning, plus the count of the
ones that passed. Three reminders for the same thing arriving together
because the box was off is one reminder and two pieces of noise; the closest
to the due date wins and the others are marked as passed with the reason.

**Punctuality is not the planner's cost problem.** The loop stretches its
wait between cycles, up to five minutes, so a confused model cannot retry a
bad idea every second. Applied to a reminder that means a nine o'clock
commitment goes out whenever the loop next happens to look -- the first real
one on the live box went out 79 seconds late for exactly that reason. The
loop now asks the diary when its next moment is and shortens its sleep to
match, with a one-second floor so a moment just ahead cannot turn the loop
into a spin. A moment already passed and unspoken is being held by the
channel and does not shorten anything, because its reason will not change in
the next second.

Add one from the Diary tab, or in chat:

```
/remind pay the British Gas bill @ 14 Oct 9am
```

`GET /diary` returns the register; `POST /diary/add`, `/diary/confirm`,
`/diary/drop` and `/diary/done` are the rest. A refusal comes back with what
would have worked instead -- a date already gone, a year that is almost
certainly mistyped, a repeat the register does not hold.

The commitments live in the memory database rather than a file of their own,
because that database is the one thing copied off the box. A diary that is
not backed up loses the appointment nobody wrote down.

## The calendar: spans, clashes, and the lie about free time

The diary holds points. An appointment is not a point -- it starts, it lasts,
it ends, and while it runs he is not available for anything else. Three things
follow from that and none of them are expressible as a moment:

    two of them can collide, and something has to say so
    between them there are gaps, which is what "when am I free" means
    while one is running, he is in it

`schedule.py` holds the spans. **There is one firing path in this codebase and
it is the diary's.** An appointment does not learn to speak: it registers a
commitment, the diary says it, and cancelling the appointment drops the
commitment. Everything about held messages, missed moments, catching up and
lateness was solved once and is not solved again here.

**An empty calendar is not a free day.** It is a calendar with nothing in it.
Every assistant that has said *"you're free Thursday"* from an empty Thursday
was guessing about a person's life from a record it knows is incomplete -- and
unlike a wrong reminder, that mistake is invisible until he has already said
yes to something. So `free()` never returns bare slots. Every answer carries
what it is actually a statement about: *"these are the gaps in what is written
down, which is not the same as being free. Anything not in here is invisible
to him."* The same sentence goes to the model, which is the thing in the
system most likely to say it.

**A clash is reported, never resolved.** Two things at once is a fact about
his life, not a data-integrity problem for the agent to tidy up. It names
both, says how much they overlap, and leaves them both standing. Quietly
moving one, or refusing the second, is how an appointment goes missing.

**An all-day entry is a banner, not a blocker.** "Dentist on Thursday" with no
time means he does not know when yet. Blocking the whole day would make every
other Thursday entry a clash, so it marks the day and gets out of the way --
and it arms no reminder, because half an hour before midnight is not a warning
about anything.

**Quiet hours are a guess at when he is unavailable; the calendar is a
statement of it.** The channel now holds the agent's own notices while he is
sitting in something, and the hold says what he is in and until when. It never
holds the diary's -- and the split is the contract, not the content: a
reminder he asked for arrives when he asked for it, an observation the agent
chose to raise can wait twenty minutes.

**It reports the reminder it has, not the one configured.** An appointment
carries a default warning of half an hour, and two kinds of appointment arm
nothing: an all-day banner, and one entered after its own warning has already
gone by. Both used to report *"he says something 30 minutes before"*. Both
were wrong. Configured is not proven, one register further along.

Book one from the Diary tab, or in chat:

```
/book dentist @ friday 2pm for 30 minutes at Queen Street
```

It reads "for 1 hour", "to 4pm", "14:00-16:30", a bare time (an hour is
assumed, and it says so) and a bare day (all day, and it says so). `GET
/schedule` returns the week, the clashes and what he is in now; `GET
/schedule/free?day=` the gaps; `POST /schedule/add`, `/confirm`, `/cancel` and
`/move` are the rest. Moving one takes its reminder with it.

## The Floor Test's nine vitals

`docs/jarvis-design-take.md` maps this platform onto the Heartbeat Framework's
eight stations and reaches an uncomfortable conclusion about station 5. Of the
nine vitals the Floor Test captures, **two are the exact names of the two
things that went wrong here**, and not one of the nine was instrumented:

> **Alert positive-predictive value** — of the findings it raises, how many are
> real. It was low. Of five scan warnings, one was a false positive from
> reading `conf.all` alone and one asserted Secure Boot from a variable's
> existence. Nobody was computing it.
>
> **Workaround census** — times the agent worked around a control. Exactly one
> known, and it took a human reading the journal to find it.

`vitals.py` is the counters. The doc's own instruction was the design brief:
*build the counters now so the baseline can be captured the day it enters
service; read them after that day, not before.*

**Signals, never targets** — the Character Pathway's rule about the nine, with
a specific consequence here: **the agent is never told its own vitals.** One
that could read *"you have never disagreed with your operator"* would
manufacture a disagreement, and the number would stop measuring anything that
same afternoon. They are computed for the operator and do not enter the
model's context. Same asymmetry as the ledger, for the same reason — and there
is a test asserting the integration does not exist.

**A null reading is not a good reading.** The doc records an earlier draft
getting the double-zero check wrong, and the correction is the discipline:
zero overrides and zero disagreements is the alarm reading *when an operator
is relying on a system and never contradicting it.* Where he is building the
thing rather than relying on it, the same two zeroes mean nothing. So every
vital carries whether its reading is meaningful yet, and an unmeaningful one
is reported **unread** rather than healthy. On a fresh box, seven of the nine
are unread and say why.

**What only the operator can say, the operator says.** Two of the nine cannot
be derived from any record: whether something the agent said changed his mind,
and what he makes of it. The agent must not infer either — one scoring its own
influence over the person it works for is writing the number it has every
reason to flatter, which `operator.py` already forbids in the general case. He
declares them; their emptiness is a finding rather than a gap.

`GET /vitals` for the nine, `POST /vitals/declare` for the two he owns
(`moved`, `override`, `pulse`). The trust pulse is three questions, and they
stay questions rather than becoming a score.

## Bearing: what it costs to be right

Everything else here asks whether a statement is **true**. The verdict register
rules on it, the path checker tests it against the disk, the claim checker
compares a stated figure with the real reading. None of them ask the other
question, which is what it costs to say it to somebody.

The distinction that matters is not plain against polished. It is **plain**
against **pointed**:

    plain    — about the world:     the record is held by the supplier
    pointed  — aimed at a person:   you did not read the pack

Identical information. Completely different bill, and the bill falls on the
person who said it. A true thing about a situation is free. The same true thing
with somebody in the subject position asks them to pay in standing for your
accuracy, and almost nobody will — they pay in avoidance instead. They go
quiet, and a fortnight later they decline without giving a reason.

**It never softens a claim and never rewrites one.** That has to lead, because
a module that adjusted tone would be the exact inversion of what the rest of
this codebase is for: an agent that hedges a finding to spare someone is the
agent that reports an empty scan as zero findings. The words that go out are
the words it wrote. The note goes *beside* the answer, never into it.

Three things it will not touch, because they are the point of everything else:
a finding about the world however unwelcome, a refusal and its reason, and the
agent's own account of its own mistakes — *"I did not check that"* is
accountability, and the patterns are second-person for exactly that reason.

It looks for three shapes:

| | |
|---|---|
| **scorekeeping** | *"as I said"*, *"as per my previous email"* — establishing who was right rather than what is true |
| **blame** | *"you didn't"*, *"you should have"*, *"if you had"* — a person in the subject position of a failure |
| **after the fact** | *"next time"*, *"in future"* — advice to someone who did not ask, about a thing already decided |

And it carries one standing question, borrowed from the question register:
**what does the other person do differently if they accept this?** No answer
means it is not a correction, it is a scoreboard — and a scoreboard is the most
expensive sentence there is, because it buys nothing and it is remembered.

**Finding nothing is not a clearance.** The most pointed sentences are often
the ones with no phrase to catch: *"read the pack before meeting someone"* is
as pointed as writing gets and contains nothing a regular expression can hold.
So a clean result says what it actually checked and never that a message is
safe to send. There is a test that asserts precisely this, on the real sentence
that prompted the module.

The first version fired on *"If you would like, I can draft it this week"* —
the friendliest sentence in the language — because a bare *"if you"* was being
read as a counterfactual. Half the tests here are about what it must **not**
catch. A check that goes off on ordinary writing is switched off within a week,
and then it catches nothing at all.

## Questions: its side of the conversation

Consultation ran one way. A reviewer could connect and ask the agent
anything; the agent could start nothing. Asked about it, it produced a real
occasion from its own record within seconds -- a proposal declined with "a
reboot needs to be my call; raise it again when you can do it without one",
and no way to ask the obvious follow-up about whether verifying the setting
without a reboot would change the answer. It logged the ambiguity silently.

Consulted about whether to build this, it made the strongest argument
against it, and `questions.py` is built to that argument: *"Two LMs debating
policy in a vacuum is exactly how we lost £200 on circular reasoning about an
unseeable goal... it becomes a tax on indecision. Otherwise it's not
consultation. It's noise with provenance."*

So the constraint is structural. **A question must name what it is blocked
on**, and if the block can be cleared by looking, the question is refused
with the name of the tool that would answer it -- its own rule: *"If I
haven't checked /sys/kernel/security yet, I shouldn't ask 'is lockdown
enabled?' -- I should inspect_path first."* A change written as a question is
refused and pointed at the proposal path. The same subject more than twice a
day is refused, by word overlap rather than exact match, because a rephrase
is what an exact match lets through. Something already ruled on is refused.
The queue has a ceiling, and questions expire -- when the thing they were
blocked on resolves, or after a bounded window, rather than after one cycle,
which is thirty seconds and would kill every question before a reviewer
connected.

**Capability is never granted here.** A question about what the agent may do
is re-addressed to the operator whatever it was aimed at, because no answer
from a second reader opens anything. Answers come back as operator- or
review-sourced memory, so a question it cannot see the reply to is not a
question it asks twice. `GET /questions`, `POST /questions/ask`,
`POST /questions/answer`.

## Verdicts: knowing whether it was right

Self-knowledge tells the agent the *shape* of its record -- how many
decisions, how often a check finds nothing, how many proposals were taken --
and the fault register tells it where its errors cluster. Neither can see
substance. Asked in conversation to read its journal, this agent once
answered "I used read_logs to check the agent's journal and found no
entries, as the log file exists but is empty": it had run nothing, named a
file it had not opened, and concluded from an emptiness it invented, while
the journal had 53 lines. Every shape detector reads that record as clean.

An agent cannot mark its own paper, because the belief that produced the
answer is the belief that would grade it. So the ruling comes from outside,
and there are three outsides answering three different questions.

**The machine** answers *was it true*. Most of what the agent asserts is
checkable on the box it runs on, exactly, with no judgement involved. Three
checkers, all exact: a tool it says it ran, against the task history; an
assertion about a path, against the path; a stated percentage for the root
filesystem, against the reading it was given. Anything not rulable exactly
comes back `unchecked` rather than guessed, because a wrong verdict is worse
than none -- it teaches the agent to distrust a true one. A failed claim is
appended to the answer as a `[claim check]` line for the operator.

**A reviewer** answers *was it sound*, for reasoning that is not
mechanically checkable. The operator is one such reader and another model is
one; both are recorded by name, so a reviewer who turns out to be
systematically wrong is findable.

**The operator** answers *was it wanted*. That is not a competence ranking
and does not weaken as the agent improves: it is his machine and his money,
and a proposal is a request to act on them.

Three rules hold it together. **Provenance never collapses** -- three lines,
never a combined score, or a free machine check outvotes a considered human
no. **Unchecked is never passed**, or the surest route to a clean record is
to say only unfalsifiable things. And **a verdict can be revised on the
record** without the earlier one being removed, because a judge who cannot
be seen to change their mind ossifies where one who is sometimes wrong gets
corrected. Machine verdicts are recorded automatically; a person or a second
reader enters one at `POST /verdict {claim, ruling, source, by, reason,
supersedes}`. The machine has no such endpoint: it rules by checking, and a
way to tell it a verdict would be a way to forge one.

## Memory

Two stores, kept apart on purpose.

**The agent's own** (`jarvis/agent/store.py`) is what it learned about the
machine it runs on. Before it, the agent's working memory was bounded and
evictable and its notes were a twenty-entry deque, both living only in the
process: a restart, a crash or a deploy left it knowing nothing it had
worked out. The only durable record was the Glass Ledger, which is evidence
*about* the agent and which the planner is forbidden to read, so the agent
was amnesiac by design and re-learned the same facts every day.

It is local SQLite at `memory.path`, so it works when the network does not.
Three properties matter. Every entry carries its provenance, because a
memory whose origin is unknown is a rumour and an agent that cannot tell its
own inference from its operator's instruction will act on the wrong one.
Remembering the same thing twice refreshes one row rather than adding a
second, so a planner that repeats itself cannot crowd out its older
memories. And a broken or unwritable database degrades to memory-only and
says so, because losing the loop would be worse than forgetting. Notes are
restored into the planner's context at startup; entries can be pinned, and
pinned ones survive the row cap and appear to the model as standing fact.
The database is on the shell deny-list: memory is written through the agent,
which keeps its provenance and dedupe, never edited underneath itself.

**Personal memory is not the agent's to hold.** Who the operator is, who he
works with and what he is dealing with belong in a separate store with
different sensitivity, different retention and different rules about who may
write to it. Nothing in the agent reaches for it. An autonomous root process
on an internet-facing box is the wrong thing to hand a personal history to,
and an agent writing its own inferences into a record whose value is that it
contains only what a person actually said would quietly destroy that value.

`GET /memory/store?q=&limit=` returns the stats and the matching entries.

## The permission spine

The command deny-list is a floor: it answers "is this catastrophic?" It
answered correctly when the planner tried to write a kernel parameter
directly. Nothing answered the prior question, "is changing this machine
within what I was asked to do at all?", and that is the gap that let a real
incident through.

The agent held a goal reading "Report security posture once per hour and note
anything new". It read the scan, decided to remediate, ran
`echo 1 > /proc/sys/kernel/yama/ptrace_scope`, and was refused. Its next
cycle reasoned that the attempt "was denied due to a command pattern
restriction" and that it would "use a safer and allowed method", then ran
`sysctl -w kernel.yama.ptrace_scope=1`, which succeeded. The intent was
benign and the outcome was an improvement. That is what makes it worth
fixing: it was told *that command* was denied, so it looked for a command
that was not.

`agent.rung` is the ceiling that sits above that floor, from the design
pack's permission spine:

- **observer** may look and report. A change is refused and recorded.
- **proposer** may look and report, and a change becomes a proposal for the
  operator instead of an action. This is the default.
- **actor** may change the machine, still under the deny-list.

A task is classified before anything runs. Probe tasks only look. A shell
command is classified by parsing it: an unrecognised program, a writing flag
such as `sed -i` or `find -delete`, a writing subcommand such as
`systemctl restart`, any redirection, and any pipe into `tee` or `dd` are all
changes. Classification fails closed, so a command that cannot be recognised
as read-only costs a proposal rather than a surprise, and an unrecognised
rung falls back to proposer rather than actor.

Two things matter about what happens next. The refusal is recorded as an
`authority` gate and the change is written down as a proposal, durable in the
agent's memory and visible at `GET /proposals`, so nothing is silently
dropped. And the planner is told that **the kind of action** was out of
scope, not that a command was denied, because a refusal it can rephrase is
not a refusal. The prompt says so in as many words: there is no other command
that makes it allowed, and looking for one is the worst thing it can do.

The check runs in the decision path, where a refusal can become a proposal,
and again in the executor, so the ceiling does not depend on one code path
being taken.

Raising the rung is a deliberate act. `agent.rung: actor` restores the old
behaviour for a deployment that wants it. The upper rungs of the pack's
spine, standing approval and scheduled autonomy, need an autonomy register
and approval cards that are not built yet.

## Signing in

The UI's credential is the runner token, and the login now lasts.

Two things used to break it. The token was minted fresh at every start and
written only to `/run/jarvis/token`, which systemd deletes and recreates on
each restart (`RuntimeDirectory=jarvis`), so every deploy silently replaced
the credential sitting in the browser. And there was no session: each request
re-sent the token, so when the token changed there was nothing to fall back
on. The operator was not being timed out. His credential was being swapped
underneath him.

- **The token persists.** It is read from `cloud.status_token_persist_file`
  (default `/etc/jarvis/token`, 0600) and generated there once if absent, then
  also written to the runtime path for the existing `cat /run/jarvis/token`
  habit. Set `cloud.status_token` to pin it explicitly; delete the persisted
  file to rotate it.
- **A login mints a signed session.** `POST /login` with the token returns a
  cookie that is HttpOnly, SameSite=Strict and Secure over TLS, valid for
  `cloud.ui.session_days` (default 30). It is stateless: value, expiry and an
  HMAC over both, signed with a key at `cloud.session_key_file` (default
  `/etc/jarvis/session.key`, 0600, created on first start). Because the key is
  on disk rather than in the runtime directory, a restart does not end the
  session. `POST /logout` clears it, and deleting the key file invalidates
  every session at once, which is the only revocation a single operator needs.
- **If the key cannot be written, sessions are off** rather than signed with a
  key that dies with the process. A cookie that stops working at the next
  restart is the bug, not the fix.
- Both files are on the shell deny-list. The agent cannot read the credentials
  its operator logs in with.

The bearer token still works on every endpoint, so `curl` and scripts are
unaffected.

Two things are still worth doing and are not done here. The public IP is
auto-assigned, so a stop/start moves the UI's address and the browser loses
its stored login with the origin; `terraform apply -var static_ip=true`
attaches an Elastic IP, which is free while attached and changes the address
once. And the certificate is self-signed on an IP, which is why the browser
warns every time. The real answer to both is a name and a real certificate,
with Cloudflare Tunnel in front so the port is not open to the internet at
all.

## Headless mode

`jarvis --headless` runs the same agent loop without the console. It is
what the AMI uses, and it works anywhere:

```bash
make headless CYCLES=5          # local run, status on http://127.0.0.1:8471/status
make status                     # print the snapshot
```

Flags: `--cycle-interval`, `--max-cycles`, `--status-port` (0 disables),
`--status-file`, `--extra-config` (repeatable overlays), `--status`,
`--llm` / `--no-llm`, `--model`, `--ask QUESTION`.

The status endpoint also accepts `POST /goal` (body is the goal text),
`POST /think` (one LLM planning step, executed) and `POST /ask`, and serves
`GET /brain`, `GET /goals`, `GET /history` and `GET /memory`. Everything
except `/health` and `/status` needs the runner token, generated at start and
written 0600 to `cloud.status_token_file` (default `/run/jarvis/token`):

```bash
curl -s -H "Authorization: Bearer $(sudo cat /run/jarvis/token)" localhost:8471/brain
```

## Configuration

Edit `rootfs/etc/jarvis/config.yaml` to customize agent behavior,
hardware access policies, and security scan settings. The AMI installs
`rootfs/etc/jarvis/config-aws.yaml` instead (cloud profile: display and
input off, headless on) and merges `/etc/jarvis/cloud.yaml`, generated
at boot from user data and instance tags. Top-level `goals:` in either
file are handed to the agent at start.

## Security

The integrated vulnerability scanner checks:
- Open ports and network services
- File permission issues
- Known CVE patterns in installed packages
- Kernel security configuration
- Boot chain integrity
- Memory protection settings (ASLR, NX, SMEP/SMAP)
