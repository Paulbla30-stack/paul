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
