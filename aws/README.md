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
4. A status endpoint listens on `127.0.0.1:8471` and a snapshot is kept at
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

The build uploads `git archive HEAD`, so commit before building; the
image always corresponds to a commit. Provisioning ends with a three-cycle
headless self-test, and the build fails if the agent cannot start.

## Launching an instance

Either use the Terraform example:

```bash
cd aws/terraform
terraform init
terraform apply -var ami_id=ami-0123456789abcdef0 -var region=eu-west-2
```

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

## Talking to the agent

```bash
aws ssm start-session --target i-0123456789abcdef0   # or ssh
openclaw --status                                     # summary
curl -s localhost:8471/status | python3 -m json.tool  # full snapshot
curl -s localhost:8471/history                        # last 20 task results
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

Both profiles run the same `openclaw` package; the ISO build is untouched.
