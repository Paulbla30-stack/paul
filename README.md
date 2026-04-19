# OpenClaw Bootable ISO

A bootable ISO virtual environment containing the OpenClaw agentic agent software
with full system hardware access and integrated security vulnerability scanning.

## Architecture

```
openclaw-iso/
├── openclaw/           # Core agent software
│   ├── agent/          # Agentic AI core (planner, executor, memory)
│   ├── hardware/       # Hardware access layer (display, input, memory, storage)
│   ├── security/       # Vulnerability scanner and system hardening
│   └── ui/             # Console UI and dashboard
├── scripts/            # ISO build scripts
├── config/             # Boot and system configuration
├── rootfs/             # Root filesystem overlay
└── tests/              # Test suite
```

## Features

- **Agentic Agent Core**: Autonomous task planning, execution, and memory
- **Full Hardware Access**: Direct access to screen, keyboard, mouse, video memory, RAM, storage
- **Security Scanner**: Integrated vulnerability detection and system hardening
- **Bootable ISO**: Standalone environment bootable from USB/CD/VM

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

## Configuration

Edit `rootfs/etc/openclaw/config.yaml` to customize agent behavior,
hardware access policies, and security scan settings.

## Security

The integrated vulnerability scanner checks:
- Open ports and network services
- File permission issues
- Known CVE patterns in installed packages
- Kernel security configuration
- Boot chain integrity
- Memory protection settings (ASLR, NX, SMEP/SMAP)
