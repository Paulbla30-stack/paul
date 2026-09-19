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
`uploaded_files`. Login is the runner token. The API behind it: `POST
/chat` (multi-turn), `POST /upload` (raw body + `X-Filename`), `GET
/uploads`, plus everything the loopback status server offers.

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
the settings, so a behaviour change always has a return address. API:
`GET /lab`, `POST /lab/settings`, `POST /lab/preview`, `POST /lab/run`.

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
