# Jarvis on AWS: the agent-first AMI

This directory turns Jarvis into an Amazon Machine Image where the agent
is the operating system's primary process. The instance boots, works out
who it is from the EC2 metadata service, reads its goals from user data,
and starts its observe-plan-act-reflect loop before any human logs in.
People are guests on the box; the agent is the tenant.

```
aws/
├── packer/jarvis-ami.pkr.hcl   # builds the AMI (Amazon Linux 2023 or Ubuntu 24.04)
├── scripts/provision.sh          # installs the agent + units inside the build instance
├── scripts/cleanup.sh            # scrubs instance identity before the snapshot
├── scripts/motd.sh               # login banner pointing humans at the agent
├── systemd/jarvis-bootstrap.service   # IMDS + user data -> /etc/jarvis/cloud.yaml
├── systemd/jarvis.service             # the agent, headless, restart=always
├── cloud-init/user-data.example.yaml    # how to hand the agent goals at launch
└── terraform/                    # optional: launch an instance from the AMI
```

## What happens at boot

1. **cloud-init** runs as usual (SSH keys, hostname, packages).
2. **jarvis-bootstrap.service** asks IMDSv2 for the instance identity,
   tags and user data, then writes `/etc/jarvis/cloud.yaml`:
   instance facts under `cloud.instance`, operator overrides, and a
   normalised `goals` list.
3. **jarvis.service** starts `jarvis --headless --extra-config
   /etc/jarvis/cloud.yaml`. The agent seeds its goals, runs the cloud
   boot tasks (IMDS probe, memory, storage, security scan) and then keeps
   cycling every `cloud.cycle_interval` seconds. Its log goes to the
   journal and the EC2 serial console.
4. If an Anthropic API key is available, the **LLM brain** plans each cycle
   (see below); otherwise the rule planner runs and the journal says why.
5. A status endpoint listens on `127.0.0.1:8471` and a snapshot is kept at
   `/run/jarvis/status.json`. `jarvis --status` reads either.

## Building the AMI

Prerequisites: [Packer](https://developer.hashicorp.com/packer) 1.9+ and
AWS credentials that can launch an instance and register an AMI.

```bash
make ami-init                     # once: downloads the amazon plugin
make ami-validate                 # bundles HEAD and validates the template
make ami AWS_REGION=eu-west-2     # builds; ~6-8 minutes
cat build/ami-manifest.json       # the new AMI id
```

Options (all `make` variables or `-var` flags):

| Variable       | Default    | Notes                                   |
|----------------|------------|-----------------------------------------|
| `AWS_REGION`   | eu-west-2  | Region to build and register in          |
| `AMI_BASE`     | al2023     | `al2023` or `ubuntu` (24.04)             |
| `AMI_ARCH`     | x86_64     | `x86_64` or `arm64`                      |
| `AMI_INSTANCE` | t3.small   | Use `t4g.small` for arm64                |
| `AMI_SSH`      | session_manager | How Packer reaches the build instance: `session_manager` (SSM over HTTPS, no port 22, needs the [session-manager-plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) locally) or `public_ip` (plain SSH) |

The build uploads `git archive HEAD`, so commit before building; the
image always corresponds to a commit. Provisioning ends with a three-cycle
headless self-test, and the build fails if the agent cannot start.

## Launching an instance

Either use the Terraform example:

```bash
cd aws/terraform
terraform init -backend-config="bucket=<your-state-bucket>" \
               -backend-config="key=jarvis/terraform.tfstate" \
               -backend-config="region=eu-west-2"
terraform apply -var ami_id=ami-0123456789abcdef0 -var region=eu-west-2
```

State is kept in S3 (create a private, versioned bucket once) so the
instance can be managed from any machine later.

It creates an IAM role with SSM Session Manager access, a security group
with no inbound rules, and an instance with IMDSv2 enforced and tags
exposed to metadata. The bundled example user data is attached so the
agent starts with goals. Pass `-var agent_goal="..."` to add a goal via
the `jarvis:goal` tag, or `-var ssh_cidr=203.0.113.4/32 -var key_name=mykey`
if you want SSH as well.

Or launch by hand from the console or CLI. The only things that matter:

- **User data**: a cloud-config with an `jarvis:` block
  (see `cloud-init/user-data.example.yaml`). cloud-init will log a schema
  warning about the unknown key; that is expected.
- **Tags** (optional, needs "allow tags in instance metadata"):
  `jarvis:name` renames the agent, `jarvis:goal` adds one goal.
- **IAM role** with `AmazonSSMManagedInstanceCore` if you want to reach
  the box without opening port 22.

## Giving the agent a brain

The AMI profile has `llm.enabled: true` with `claude-opus-5`; the only thing
missing at build time is the Anthropic API key. Store it once in AWS
Secrets Manager:

```bash
aws secretsmanager create-secret --name jarvis/anthropic-api-key \
    --secret-string "$ANTHROPIC_API_KEY"
```

The value can be the bare key or a JSON object with an `ANTHROPIC_API_KEY`
(or `api_key`) field. The AMI profile already points at
`llm.api_key_secret: jarvis/anthropic-api-key`; override it in user data
or with an `jarvis:llm-key-secret` tag (name or ARN). SSM Parameter Store
works the same way through `llm.api_key_ssm_parameter` and the
`jarvis:llm-key-parameter` tag, and is tried second.

The Terraform example grants `secretsmanager:GetSecretValue` on that secret
to the instance role (`-var anthropic_api_key_secret=...`; set
`anthropic_api_key_ssm_parameter` instead or as well for SSM; empty skips
the grant). At every boot the bootstrap service reads the secret with the
instance role and writes `/etc/jarvis/anthropic.key` (0600). The key never
appears in user data, cloud.yaml or the journal. An inline `llm.api_key` in
user data also works for quick tests, but user data is readable by anyone on
the instance.

What the brain may do is set by `llm.shell` in `/etc/jarvis/config.yaml`
(on in the AMI profile, deny-list guarded) and by the goals you give it.
Budget it with `llm.max_calls_per_hour` (60 by default, so at the default
30 s cycle it plans at most every minute when busy) and `llm.effort`.

```bash
jarvis --status                       # includes brain model, budget and last reasoning
T="Authorization: Bearer $(sudo cat /run/jarvis/token)"   # 0600, root only
curl -s -H "$T" localhost:8471/brain    # full brain status + last thought
curl -s -H "$T" -X POST localhost:8471/goal -d 'Find out why disk fills up nightly'
curl -s -H "$T" -X POST localhost:8471/think    # plan one step now and run it
curl -s -H "$T" -X POST localhost:8471/ask -d 'What have you changed today?'
sudo jarvis --ask 'Is anything wrong with this box?' --no-hardware   # key file is root-only
```

A goal is an instruction the root agent will act on with its shell, so the
token is required for every call except `/health` and `/status`, and the
goal channels (user data, the `jarvis:goal` tag, `POST /goal`) should be
treated as root-equivalent: anyone with `ec2:CreateTags` on the instance can
hand it work.

### Using Amazon Bedrock, or your own model

Set `llm.provider: bedrock` and the brain calls Amazon Bedrock with the
instance role instead of the Claude API. No API key, no secret to store.
`llm.model` is any of:

| Kind | Example |
|------|---------|
| Catalog model | `meta.llama3-3-70b-instruct-v1:0`, `openai.gpt-oss-120b-1:0` |
| Inference profile | `eu.meta.llama3-3-70b-instruct-v1:0` or its ARN |
| Your own imported model | `arn:aws:bedrock:eu-west-2:123456789012:imported-model/abc123` |

Launch with `terraform apply -var llm_provider=bedrock` (which grants the
Bedrock invoke actions and skips the key grant) and put the block from the
user data example in place of the `anthropic` one. Enable model access for
the catalog model once in the Bedrock console for your region.

To serve **your own model**, use Bedrock Custom Model Import: Llama,
Mistral/Mixtral, gpt-oss or Qwen architecture weights in Hugging Face
safetensors format, in S3, imported once (us-east-1 and us-west-2 only).
`aws/scripts/bedrock_import.py` does the whole thing from a Hugging Face
repo id: it launches a temporary instance with a big disk, downloads the
weights straight into `s3://jarvis-models-<account>/<name>/`, terminates
the instance, runs the import job with the `jarvis-bedrock-import` role
and prints the ARN.

```bash
pip install boto3
python3 aws/scripts/bedrock_import.py --hf-repo Qwen/Qwen3-32B --name jarvis-qwen3-32b --instance-type m6i.2xlarge
# gated repo (Llama, Mistral): store a HF read token in Secrets Manager first
python3 aws/scripts/bedrock_import.py --hf-repo meta-llama/Llama-3.1-8B-Instruct \
    --name jarvis-llama31-8b --hf-token-secret jarvis/hf-token
# weights already in S3
python3 aws/scripts/bedrock_import.py --s3-uri s3://my-bucket/my-model/ --name my-model
```

It needs the bucket and the two roles (`jarvis-bedrock-import` for
Bedrock, `jarvis-model-fetcher` for the temporary instance); create them
once with the snippet in the script's docstring or let an operator with
IAM rights run it first.

Put that ARN in `llm.model`. Bedrock's Converse API does not serve
imported models, so the brain switches to InvokeModel for any
`imported-model` ARN and renders the conversation with the model's chat
template: `chatml` for Qwen (the default), `llama3`, or `mistral`, guessed
from the model name or set with `llm.bedrock.chat_template`. Imported
models are unloaded when idle and the first call after a pause returns
`ModelNotReadyException` while it loads (about a minute for a 7B model);
the brain treats that as a short backoff (`llm.bedrock.not_ready_backoff`)
and the rule planner covers the gap. Use an instruction-tuned checkpoint:
the planner asks for a JSON decision and a base model that has not been
instruction-tuned will not reliably produce one.

Reasoning checkpoints such as Qwen3 think out loud in a `<think>` block
before answering. The brain strips that block from every reply, always
asks for a plan with thinking pre-filled off (the empty think block Qwen3's
own template uses for `enable_thinking=False`, so the JSON fits the token
budget), and by default (`llm.bedrock.thinking: auto`) lets chat and
`--ask` think until the first think block is seen, then turns it off for
those too. Set `on` to keep chat thinking, `off` to never allow it.

Open models do not have Claude's structured outputs, adaptive thinking or
refusal fallbacks, so the brain asks for JSON in the prompt, extracts and
validates it, and re-asks once with the parse error (`llm.bedrock.json_retries`).
Expect planning quality to track the model: a 70B-class instruct model is
fine with `llm.shell.enabled: true`; with a small model, keep shell off.

### The Glass Ledger: a journal the agent cannot rewrite

Any system that keeps its own log can rewrite its own log. The AMI ships
the Glass Ledger (Blatherwick, *The Glass Ledger v2*,
[doi:10.5281/zenodo.21515861](https://doi.org/10.5281/zenodo.21515861)):
an append-only journal at `/var/lib/jarvis/ledger.jsonl` where every
entry carries the SHA-256 fingerprint of the entry before it and an
Ed25519 signature over its own fingerprint, with length-prefix framing,
canonical JSON and domain-separated signing (`jarvis/ledger/chain.py`
documents the exact bytes). The agent records:

| kind | what |
|---|---|
| `genesis` | seq 0: commits the public key and the writer's name |
| `decision` | every brain decision: reasoning, chosen task, completed goals, note, or the repeat it was refused |
| `action` | every task about to run (type, command, goal, source), operator goals and uploads (with the file's SHA-256), agent start |
| `outcome` | success, error, duration and a digest of the output |
| `gate` | a shell command refused by policy, a brain cooldown |
| `thought` | a chat or `--ask` exchange (question, answer digest) |

The action entry is written **before** the task runs. With
`ledger.fail_closed: true` (the default) the agent does not plan, act or
answer while it cannot record: no record, no action, no answer. The
reason (a torn tail after a power loss, a locked file) shows under
`ledger` in `/status` and on the UI; the one documented repair is an
operator command, `python -m jarvis.ledger repair <file>`, never the
agent's. The shell policy denies the agent any command touching the
ledger, its key or the verifier, and the planner prompt says why: the
ledger is evidence about the agent, not context for it.

**The witness never lives with the writer.** A verdict from the box that
wrote the file proves nothing, so Terraform creates an S3 bucket with
Object Lock (`ledger_anchor = true`, COMPLIANCE retention
`ledger_retention_days`, default 30). The instance role may only
`s3:PutObject` under `ledger/` and is explicitly denied reading,
deleting, or changing retention; every version it writes is kept until
the retention expires, and nobody, root included, can shorten that. The
agent anchors a checkpoint pin `(seq, entry_hash)` and a copy of the file
at most every `ledger.anchor.every_s` seconds (300). The audit runs from
your machine:

```bash
# once, when the agent first starts: record its public key somewhere the
# instance cannot reach (it is also logged at start and at /ledger/pubkey)
aws ssm send-command --instance-ids <id> --document-name AWS-RunShellScript \
    --parameters commands='cat /etc/jarvis/ledger/ed25519.pub'

# then, whenever you like: verify the witness copy, check your pin, advance it
python3 aws/scripts/ledger_audit.py --bucket $(terraform -chdir=aws/terraform output -raw ledger_bucket) \
    --writer jarvis-agent --region us-west-2 --pubkey <hex> --pin build/ledger.pin
```

The audit checks the four chain rules on every entry, that genesis
commits the key you pinned (a rewrite under a fresh key dies at seq 0),
that the chain still extends the pin you kept (a rolled-back or
regenerated history is BROKEN), and that the instance's own checkpoint
agrees with the chain. `python -m jarvis.ledger verify <copy> --pubkey
<hex> --pin <file>` does the same on any copy; `tail` prints entries.
Verdicts, never tracebacks: hostile input comes back as `BROKEN at seq N`.
A partial final line is `INTACT (torn tail)` with a repair instruction,
so an accident is not reported as an attack.

What it is not: not tamper-proof (edits are detectable, not impossible),
not confidentiality (entries are plaintext; encrypt the volume if the
content is sensitive), not trusted time (timestamps are the writer's
clock). And the bucket is in the same AWS account as the writer; for
stakes that justify it the paper asks for a seal held by a different
party, which is where a second copy to another account or an RFC 3161
timestamp belongs.

### Interpreter on Amazon Linux 2023

The Anthropic SDK needs Python 3.10 or newer and AL2023's system `python3` is
3.9, so provisioning installs `python3.11` and records it in
`/etc/default/jarvis` as `JARVIS_PYTHON`. The launcher and both units read
that file; on Ubuntu 24.04 the system Python is used.

## The web UI

The agent serves its own UI: a chat grounded in what it can see and has
done, an agent panel (goals, recent tasks with output, brain state, a
"Think now" button), and file uploads the brain is told about. It listens
on port 8443 with a self-signed certificate and logs you in with the
runner token, so it is safe to reach from a phone without a tunnel.

```bash
terraform apply ... -var ui_cidr=203.0.113.4/32       # your public IP
sudo cat /run/jarvis/token                          # via SSM; paste into the login box
```

Then open `https://<public ip>:8443/ui`, accept the certificate warning
once, and paste the token. In chat, `/goal text` adds a goal and `/think`
runs one planning step. Uploaded files go to `/var/lib/jarvis/uploads`
and appear in the brain's context as `uploaded_files`, so "look at the CSV
I just uploaded" works.

Keep `ui_cidr` narrow: the token is the only login and anyone who has it
can hand the root agent work. `ui_cidr = "0.0.0.0/0"` puts the login page
in front of every scanner on the internet. The listener holds the line —
every path but `/ui` and `/health` needs the token or a signed session,
`/health` answers only `{"ok": true}`, and five bad tokens from one address
lock it out for five minutes — but a narrow CIDR, or a tunnel in front, is
the wall. Only the loopback API on 127.0.0.1:8471 serves `/status` without
a token, because nothing off the box can reach it.

## The current image

`ami-085e3acea2b1ff080` (us-west-2, built from the tree at `71516d7`, 8GiB
encrypted gp3). It carries everything the
previous image did -- the permission spine, filesystem grounding, durable memory
with consolidation, ledger-derived self-knowledge, the operator channel, estate
reporting, the standing system goals, the responsive UI and `cloudflared` -- plus
the vigil and the work of 20 September:

- **the vigil** (`jarvis/agent/vigil.py`): the agent sleeps, and is woken by the
  operator, by a material change in its observations, or on a heartbeat;
- **filesystem usage in the planner's context**, which it had never been able to
  see despite holding a standing goal about it;
- **the catalog planner** `qwen.qwen3-235b-a22b-2507-v1:0` in the Bedrock
  user-data example, in place of an imported-model ARN;
- **repeat pacing**, so a task that merely repeats backs off like an idle one;
- **proportional banding** of large numbers, so a disk moving by a fraction of a
  percent is not mistaken for a disk filling up;
- **the tool register** (`jarvis/agent/tools.py`): every capability declared in
  one place, with the rung that opens it, so a tool cannot ship half-wired;
- **the fault register** (`jarvis/agent/faults.py`): where this agent's errors
  cluster, as counted facts rather than rules;
- **the lab session** (`jarvis/agent/lab.py`): the behaviour lab cannot open
  without the agent being told it is open;
- **the verdict register** (`jarvis/agent/verdicts.py`): whether what it said was
  true, ruled on by the machine, by a second reader, or by the operator, and
  never merged into one figure;
- **an empty system prompt sends no system block**, without which the lab's base
  variant failed as a transport error on any catalog model;
- **`read_file`** (`jarvis/agent/environment.py`, `documents.py`): plain text,
  PDF, Word, Excel and PowerPoint, plus scans and photographs of documents
  through AWS Textract, with everything it could not read named rather than
  returned as empty. **pypdf is in this image**; the one before it reported
  every PDF unreadable;
- **a widened path fence**, which had not covered the runner token, the UI
  session key, the tunnel token, the notify destination or the memory
  database, and a test that keeps it in step with the shell deny-list -- which
  immediately found `/etc/jarvis/tls` readable by `shell_command`;
- **`proven` on the operator channel**, so a channel that has never delivered
  anything stops reporting itself ready;
- **a question register** (`jarvis/agent/questions.py`): the agent can raise
  one, and cannot use it to think out loud, rephrase a refusal or ask for
  capability;
- **a sense of time** (`jarvis/agent/timesense.py`): the operator's clock
  beside the machine's, durations measured from its own record rather than
  estimated from a human prior, and dates in a document read as distances
  from today;
- **durable goals**, so an instruction given through the API survives a
  restart instead of vanishing silently;
- **an operator profile** (`jarvis/agent/operator.py`): what it knows about
  the person it works for, given rather than gathered, and structurally
  unable to hold a contact detail.

**The image carries the code for all of that and none of the contents.** The
operator profile, the goals, the questions and everything the agent has
learned live in `/var/lib/jarvis/memory.db` on the instance, not in the AMI --
which is right, because personal data does not belong in a machine image, and
worth saying because a fresh instance launched from here starts not knowing
anyone.

It also carries **the memory's own off-box copy** (`jarvis/agent/backup.py`)
and **measured task durations** -- the executor was already clocking every
task for the ledger and the figure never reached the history the agent reads,
so it was inferring durations from the gap between entries, which is the
cycle interval whenever the loop idles. It reported a goal_step as taking 45
seconds. It is 31ms, 15ms, 24ms and 1.2s for the probes now, measured.

And **the diary** (`jarvis/agent/diary.py`), which is the other half of the
sense of time. Reading a date and keeping it are different jobs: "due 14 Oct"
was true for one cycle, went into a note, and nothing was ever going to
happen on the fourteenth. The register holds a thing, a moment and when to
speak about it, is checked on the idle path beside the backup, and therefore
fires **while the model is asleep** -- a reminder does not wait on the
planner and does not cost a call. A date found in a document he handed over
is proposed and waits for him; only he makes one stand. The commitments live
in `memory.db`, so the off-box copy already carries them.

Run it with `terraform apply -var ami_id=ami-085e3acea2b1ff080`; `ami_id` has no
default on purpose.

The previous image is `ami-026c2e8648225fe36`, built from `e671ebe`, kept as
the rollback. This one has been verified by the self-test inside the Packer
build and by the same code running on `i-016f9f37fe6ca2ba8`, where it has
been observed sleeping, waking on a question, reading the real disk figure,
refusing a lab run until the window was opened, and ruling on its own claims
against the box.

The diary was exercised there too, and two of the three things worth knowing
about it were found that way rather than in a test. A commitment added
through the API fired 79 seconds after its moment, because the loop stretches
its wait between cycles to protect the planner's call budget -- a trade that
is right for thinking and wrong for a reminder; the loop now shortens its
sleep to the diary's next moment, and the second one went out 1.5 seconds
late. Asked to read a real bill the agent chose `read_file` on its own, took
the two dates that had a reason beside them, left the statement period and
the previous payment alone -- and wrote both proposals under the upload
folder's path rather than the name of the file, with "by 14 October" where
"payable by 14 October" was meant. Both fixed. What it does now:

    octopus.txt: payable by 28 October 2026
    octopus.txt: appointment is on 9 October 2026

The third thing needed no fix. Seeing a commitment two minutes out, the
planner reasoned: *"The next punctuality check in the diary is in 21 seconds,
which will trigger the operator's notification independently. No further
action is required this cycle."* It read the register, understood the job was
not its own, and stood down.

It also carries **the calendar** (`jarvis/agent/schedule.py`), which is the
diary in spans rather than moments -- and therefore the first thing here that
can say *two of these collide* and *this is where the gaps are*. Exercised on
the box: two appointments booked half an hour apart reported their thirty
minutes of overlap and both stayed standing; the gaps for that day came back
as 09:00-14:00 and 15:30-18:00, carrying the sentence that is the point of
the module -- *"these are the gaps in what is written down, which is not the
same as being free"*. An appointment's reminder fired 1.9 seconds after its
moment, through the diary, while he was inside another appointment, because a
reminder he asked for is time-critical and the calendar's hold is only for
notices the agent raised itself. Cancelling four appointments took all four
reminders with them.

Two images and two snapshots, which is the whole estate: the current one and
one rollback. Anything older is deregistered with its snapshot when a new
image lands.

**Rebuilding the image is not the same as replacing the running instance.**
Changing `ami_id` makes Terraform destroy and recreate `aws_instance.jarvis`,
which means a new volume: the agent's durable memory, its ledger chain and its
signing key, the runner token and the session key all go with the old one. The
ledger's *evidence* survives in the witness bucket, but the new writer starts a
new chain. Decide deliberately whether to carry the signing key across (same
writer identity, the chain continues against your existing pin) or to stop the
old writer cleanly, audit its complete ledger INTACT, and let the new one
begin. Copy `/var/lib/jarvis/memory.db` and the uploads either way.

## Reaching the UI through a Cloudflare Tunnel

Opening 8443 in the security group is the wrong shape for a phone: narrow it
to one address and a carrier moving you locks you out, leave it at
`0.0.0.0/0` and the login page is in front of every scanner on the internet.

A tunnel inverts it. `cloudflared` dials **out** to Cloudflare on 7844 and
holds the connection open; requests come back down it. The security group
then needs no inbound rule at all, Cloudflare terminates TLS with a real
certificate so the self-signed warning goes away, and Cloudflare Access can
ask who you are before a request ever reaches the box.

**In the Cloudflare dashboard** (Zero Trust > Networks > Tunnels), once for
the account:

1. Create a tunnel, name it `jarvis`, and copy the token it shows you. It is
   a credential: whoever holds it can re-point the hostname at their own
   machine.
2. Add a public hostname on the tunnel: your subdomain (for example
   `jarvis.<your-domain>`), service `HTTPS`, URL `127.0.0.1:8443`, and under
   *Additional application settings > TLS* turn **No TLS Verify** on, because
   the agent's certificate is self-signed and only ever seen over loopback.
3. Zero Trust > Access > Applications: add a self-hosted application for that
   hostname with a policy allowing your own email. Without this the tunnel is
   just a nicer front door with the same lock behind it.

**Then, on your side:**

```bash
aws secretsmanager create-secret --name jarvis/tunnel-token \
    --secret-string '<the token from step 1>'

terraform apply ... \
    -var tunnel_token_secret=jarvis/tunnel-token \
    -var tunnel_hostname=jarvis.<your-domain> \
    -var ui_cidr=""                              # close the port
```

The instance fetches the token at boot with its own role, writes it 0600 to
`/etc/jarvis/cloudflared.env` owned by the `cloudflared` user, and starts the
tunnel. The overlay at `/etc/jarvis/cloud.yaml` never carries it, the agent's
deny-list refuses that path and the `cloudflared` binary the same way it
refuses the ledger's signing key, and `jarvis-health` reports whether the
tunnel is up without reading the token.

`terraform output ui_exposure` says who can reach the listener at the network
level, and says so loudly when the answer is everyone.

Inbound rules are `aws_vpc_security_group_ingress_rule` resources rather than
inline `ingress` blocks, deliberately. On `aws_security_group` that attribute
is Optional *and* Computed, so a `dynamic` block whose `for_each` goes to zero
renders it unset rather than empty, and an unset Computed attribute keeps
whatever is already on the group: `ui_cidr = ""` produced a plan saying "no
changes" while the port stayed open to the internet. Separate rule resources
are deleted when their count reaches zero, so closing the port is something
the plan shows you. Egress stays inline because it is never empty.

The Jarvis token login still sits behind Access, deliberately: two
independent locks, and the inner one is what the agent itself enforces.

## Outbound

`lock_egress` (on by default) restricts the instance to HTTPS on 443, DNS to
the VPC resolver, NTP to the Amazon Time Sync service, and — when a tunnel is
configured — 7844 to Cloudflare's published edge ranges, instead of every
protocol to the whole internet:

```bash
terraform apply ... -var lock_egress=false   # back to wide open, if you must
```

That kills a reverse shell, an `scp` out, a DNS tunnel and anything on a
non-standard port at the network, underneath whatever the agent's own
deny-list does. It does not stop an HTTPS POST to an arbitrary host — only
VPC interface endpoints for Bedrock, SSM and S3, with 443 narrowed to the
VPC, would do that, at roughly $36/month.

## Talking to the agent

```bash
aws ssm start-session --target i-0123456789abcdef0   # or ssh
jarvis --status                                     # summary
curl -s localhost:8471/status | python3 -m json.tool  # full snapshot
curl -s -H "Authorization: Bearer $(sudo cat /run/jarvis/token)" localhost:8471/history   # last 20 task results
journalctl -u jarvis -f                             # live log
sudo systemctl restart jarvis                       # re-read config + user data
```

The bootstrap unit runs on every boot, so changing user data and
rebooting is enough to give the agent new goals. Editing
`/etc/jarvis/config.yaml` changes the baseline; `cloud.yaml` is
regenerated and should not be edited by hand.

## Differences from the bootable ISO

| Concern            | ISO (bare-metal profile)          | AMI (cloud profile)                    |
|--------------------|-----------------------------------|----------------------------------------|
| Process model      | Agent is PID 1 via `scripts/init.sh` | systemd service, `Restart=always`    |
| Console            | Interactive `ConsoleUI` on tty0   | Headless loop; journal + serial console |
| Display / input    | Framebuffer and evdev enabled     | Disabled (no such devices on EC2)      |
| Boot tasks         | Hardware enumeration first        | IMDS identity first, then memory, storage, scan |
| Configuration      | `config.yaml` + kernel cmdline    | `config.yaml` + `cloud.yaml` from user data and tags |
| Status             | Console commands                  | `jarvis --status`, HTTP on loopback  |
| LLM brain          | Off unless `llm.enabled` is set   | On; Claude with a key from Secrets Manager, or any Bedrock model with the instance role |

Both profiles run the same `jarvis` package; the ISO build is untouched.
