# OpenClaw on AWS: the agent-first AMI

This directory turns OpenClaw into an Amazon Machine Image where the agent
is the operating system's primary process. The instance boots, works out
who it is from the EC2 metadata service, reads its goals from user data,
and starts its observe-plan-act-reflect loop before any human logs in.
People are guests on the box; the agent is the tenant.

```
aws/
├── packer/openclaw-ami.pkr.hcl   # builds the AMI (Amazon Linux 2023 or Ubuntu 24.04)
├── scripts/provision.sh          # installs the agent + units inside the build instance
├── scripts/cleanup.sh            # scrubs instance identity before the snapshot
├── scripts/motd.sh               # login banner pointing humans at the agent
├── systemd/openclaw-bootstrap.service   # IMDS + user data -> /etc/openclaw/cloud.yaml
├── systemd/openclaw.service             # the agent, headless, restart=always
├── cloud-init/user-data.example.yaml    # how to hand the agent goals at launch
└── terraform/                    # optional: launch an instance from the AMI
```

## What happens at boot

1. **cloud-init** runs as usual (SSH keys, hostname, packages).
2. **openclaw-bootstrap.service** asks IMDSv2 for the instance identity,
   tags and user data, then writes `/etc/openclaw/cloud.yaml`:
   instance facts under `cloud.instance`, operator overrides, and a
   normalised `goals` list.
3. **openclaw.service** starts `openclaw --headless --extra-config
   /etc/openclaw/cloud.yaml`. The agent seeds its goals, runs the cloud
   boot tasks (IMDS probe, memory, storage, security scan) and then keeps
   cycling every `cloud.cycle_interval` seconds. Its log goes to the
   journal and the EC2 serial console.
4. If an Anthropic API key is available, the **LLM brain** plans each cycle
   (see below); otherwise the rule planner runs and the journal says why.
5. A status endpoint listens on `127.0.0.1:8471` and a snapshot is kept at
   `/run/openclaw/status.json`. `openclaw --status` reads either.

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
               -backend-config="key=openclaw/terraform.tfstate" \
               -backend-config="region=eu-west-2"
terraform apply -var ami_id=ami-0123456789abcdef0 -var region=eu-west-2
```

State is kept in S3 (create a private, versioned bucket once) so the
instance can be managed from any machine later.

It creates an IAM role with SSM Session Manager access, a security group
with no inbound rules, and an instance with IMDSv2 enforced and tags
exposed to metadata. The bundled example user data is attached so the
agent starts with goals. Pass `-var agent_goal="..."` to add a goal via
the `openclaw:goal` tag, or `-var ssh_cidr=203.0.113.4/32 -var key_name=mykey`
if you want SSH as well.

Or launch by hand from the console or CLI. The only things that matter:

- **User data**: a cloud-config with an `openclaw:` block
  (see `cloud-init/user-data.example.yaml`). cloud-init will log a schema
  warning about the unknown key; that is expected.
- **Tags** (optional, needs "allow tags in instance metadata"):
  `openclaw:name` renames the agent, `openclaw:goal` adds one goal.
- **IAM role** with `AmazonSSMManagedInstanceCore` if you want to reach
  the box without opening port 22.

## Giving the agent a brain

The AMI profile has `llm.enabled: true` with `claude-opus-5`; the only thing
missing at build time is the Anthropic API key. Store it once in AWS
Secrets Manager:

```bash
aws secretsmanager create-secret --name openclaw/anthropic-api-key \
    --secret-string "$ANTHROPIC_API_KEY"
```

The value can be the bare key or a JSON object with an `ANTHROPIC_API_KEY`
(or `api_key`) field. The AMI profile already points at
`llm.api_key_secret: openclaw/anthropic-api-key`; override it in user data
or with an `openclaw:llm-key-secret` tag (name or ARN). SSM Parameter Store
works the same way through `llm.api_key_ssm_parameter` and the
`openclaw:llm-key-parameter` tag, and is tried second.

The Terraform example grants `secretsmanager:GetSecretValue` on that secret
to the instance role (`-var anthropic_api_key_secret=...`; set
`anthropic_api_key_ssm_parameter` instead or as well for SSM; empty skips
the grant). At every boot the bootstrap service reads the secret with the
instance role and writes `/etc/openclaw/anthropic.key` (0600). The key never
appears in user data, cloud.yaml or the journal. An inline `llm.api_key` in
user data also works for quick tests, but user data is readable by anyone on
the instance.

What the brain may do is set by `llm.shell` in `/etc/openclaw/config.yaml`
(on in the AMI profile, deny-list guarded) and by the goals you give it.
Budget it with `llm.max_calls_per_hour` (60 by default, so at the default
30 s cycle it plans at most every minute when busy) and `llm.effort`.

```bash
openclaw --status                       # includes brain model, budget and last reasoning
T="Authorization: Bearer $(sudo cat /run/openclaw/token)"   # 0600, root only
curl -s -H "$T" localhost:8471/brain    # full brain status + last thought
curl -s -H "$T" -X POST localhost:8471/goal -d 'Find out why disk fills up nightly'
curl -s -H "$T" -X POST localhost:8471/think    # plan one step now and run it
curl -s -H "$T" -X POST localhost:8471/ask -d 'What have you changed today?'
sudo openclaw --ask 'Is anything wrong with this box?' --no-hardware   # key file is root-only
```

A goal is an instruction the root agent will act on with its shell, so the
token is required for every call except `/health` and `/status`, and the
goal channels (user data, the `openclaw:goal` tag, `POST /goal`) should be
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

To serve **your own model**, use Bedrock Custom Model Import: Llama, Mistral
or gpt-oss architecture weights in Hugging Face safetensors format,
uploaded to S3 and imported once (currently us-east-1 and us-west-2):

```bash
aws s3 sync ./my-model/ s3://my-bucket/my-model/
aws bedrock create-model-import-job --job-name openclaw-brain \
    --imported-model-name openclaw-brain \
    --role-arn arn:aws:iam::123456789012:role/BedrockImportRole \
    --model-data-source '{"s3DataSource":{"s3Uri":"s3://my-bucket/my-model/"}}'
aws bedrock get-imported-model --model-identifier openclaw-brain --query modelArn
```

Put that ARN in `llm.model`. Imported models are unloaded when idle and the
first call after a pause returns `ModelNotReadyException` while it loads;
the brain treats that as a short backoff (`llm.bedrock.not_ready_backoff`)
and the rule planner covers the gap. Use an instruction-tuned checkpoint:
the planner asks for a JSON decision and a base model that has not been
instruction-tuned will not reliably produce one.

Open models do not have Claude's structured outputs, adaptive thinking or
refusal fallbacks, so the brain asks for JSON in the prompt, extracts and
validates it, and re-asks once with the parse error (`llm.bedrock.json_retries`).
Expect planning quality to track the model: a 70B-class instruct model is
fine with `llm.shell.enabled: true`; with a small model, keep shell off.

### Interpreter on Amazon Linux 2023

The Anthropic SDK needs Python 3.10 or newer and AL2023's system `python3` is
3.9, so provisioning installs `python3.11` and records it in
`/etc/default/openclaw` as `OPENCLAW_PYTHON`. The launcher and both units read
that file; on Ubuntu 24.04 the system Python is used.

## The web UI

The agent serves its own UI: a chat grounded in what it can see and has
done, an agent panel (goals, recent tasks with output, brain state, a
"Think now" button), and file uploads the brain is told about. It listens
on port 8443 with a self-signed certificate and logs you in with the
runner token, so it is safe to reach from a phone without a tunnel.

```bash
terraform apply ... -var ui_cidr=203.0.113.4/32       # your public IP
sudo cat /run/openclaw/token                          # via SSM; paste into the login box
```

Then open `https://<public ip>:8443/ui`, accept the certificate warning
once, and paste the token. In chat, `/goal text` adds a goal and `/think`
runs one planning step. Uploaded files go to `/var/lib/openclaw/uploads`
and appear in the brain's context as `uploaded_files`, so "look at the CSV
I just uploaded" works.

Keep `ui_cidr` narrow: the token is the only login and anyone who has it
can hand the root agent work.

## Talking to the agent

```bash
aws ssm start-session --target i-0123456789abcdef0   # or ssh
openclaw --status                                     # summary
curl -s localhost:8471/status | python3 -m json.tool  # full snapshot
curl -s -H "Authorization: Bearer $(sudo cat /run/openclaw/token)" localhost:8471/history   # last 20 task results
journalctl -u openclaw -f                             # live log
sudo systemctl restart openclaw                       # re-read config + user data
```

The bootstrap unit runs on every boot, so changing user data and
rebooting is enough to give the agent new goals. Editing
`/etc/openclaw/config.yaml` changes the baseline; `cloud.yaml` is
regenerated and should not be edited by hand.

## Differences from the bootable ISO

| Concern            | ISO (bare-metal profile)          | AMI (cloud profile)                    |
|--------------------|-----------------------------------|----------------------------------------|
| Process model      | Agent is PID 1 via `scripts/init.sh` | systemd service, `Restart=always`    |
| Console            | Interactive `ConsoleUI` on tty0   | Headless loop; journal + serial console |
| Display / input    | Framebuffer and evdev enabled     | Disabled (no such devices on EC2)      |
| Boot tasks         | Hardware enumeration first        | IMDS identity first, then memory, storage, scan |
| Configuration      | `config.yaml` + kernel cmdline    | `config.yaml` + `cloud.yaml` from user data and tags |
| Status             | Console commands                  | `openclaw --status`, HTTP on loopback  |
| LLM brain          | Off unless `llm.enabled` is set   | On; Claude with a key from Secrets Manager, or any Bedrock model with the instance role |

Both profiles run the same `openclaw` package; the ISO build is untouched.
