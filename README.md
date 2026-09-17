# OpenClaw Agent-First OS

OpenClaw is an agentic agent that owns the machine it runs on. It ships in
two forms built from the same code:

- **Bootable ISO** for bare metal or a VM: the agent is PID 1 with full
  hardware access (screen, keyboard, mouse, memory, storage).
- **AWS AMI** for EC2: the agent is a headless systemd service that
  bootstraps itself from instance metadata and user data. See
  [`aws/README.md`](aws/README.md).

Both include the integrated security vulnerability scanner.

## Architecture

```
openclaw/
├── openclaw/           # Core agent software
│   ├── agent/          # Agentic AI core (planner, executor, memory)
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
- **Full Hardware Access**: Direct access to screen, keyboard, mouse, video memory, RAM, storage
- **Security Scanner**: Integrated vulnerability detection and system hardening
- **Bootable ISO**: Standalone environment bootable from USB/CD/VM
- **AWS AMI**: Agent-first EC2 image; goals arrive via user data or tags,
  status via `openclaw --status` or a loopback HTTP endpoint

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
the OpenClaw agent. The agent has full access to:

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

Launch the AMI with an `openclaw:` block in the user data to hand the
agent its goals (example in `aws/cloud-init/user-data.example.yaml`), or
use the Terraform in `aws/terraform/`. Full details in
[`aws/README.md`](aws/README.md).

## Headless mode

`openclaw --headless` runs the same agent loop without the console. It is
what the AMI uses, and it works anywhere:

```bash
make headless CYCLES=5          # local run, status on http://127.0.0.1:8471/status
make status                     # print the snapshot
```

Flags: `--cycle-interval`, `--max-cycles`, `--status-port` (0 disables),
`--status-file`, `--extra-config` (repeatable overlays), `--status`.

## Configuration

Edit `rootfs/etc/openclaw/config.yaml` to customize agent behavior,
hardware access policies, and security scan settings. The AMI installs
`rootfs/etc/openclaw/config-aws.yaml` instead (cloud profile: display and
input off, headless on) and merges `/etc/openclaw/cloud.yaml`, generated
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
