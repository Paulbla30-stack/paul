#!/usr/bin/env bash
# OpenClaw AMI provisioner
# ========================
# Runs as root on the temporary build instance.  Turns a stock Amazon
# Linux 2023 (or Ubuntu/Debian) image into an agent-first OS: the
# OpenClaw agent is installed as a systemd service that starts before
# any human logs in, bootstraps itself from EC2 metadata, and logs to
# the serial console.
#
# Expects the repository either extracted at $SRC (default
# /tmp/openclaw-src) or uploaded as $SRC.tar.gz by the Packer "file"
# provisioner (see `make ami-bundle`).

set -euo pipefail

SRC="${SRC:-/tmp/openclaw-src}"
PREFIX="/usr/lib/openclaw"
CONF_DIR="/etc/openclaw"
OPENCLAW_VERSION="${OPENCLAW_VERSION:-1.0.0}"

log() { printf '[provision] %s\n' "$*"; }

if [ ! -d "$SRC/openclaw" ] && [ -f "$SRC.tar.gz" ]; then
    log "Extracting $SRC.tar.gz"
    mkdir -p "$SRC"
    tar -xzf "$SRC.tar.gz" -C "$SRC"
fi
[ -d "$SRC/openclaw" ] || { echo "[provision] source not found at $SRC" >&2; exit 1; }

# ---- 1. Packages ---------------------------------------------------------
if command -v dnf >/dev/null 2>&1; then
    log "Installing packages with dnf (Amazon Linux / Fedora family)"
    dnf -y update --security || true
    dnf -y install python3 python3-pip python3-pyyaml pciutils usbutils util-linux \
                   amazon-ssm-agent awscli 2>/dev/null || \
    dnf -y install python3 python3-pip python3-pyyaml pciutils usbutils util-linux
    systemctl enable amazon-ssm-agent 2>/dev/null || true
elif command -v apt-get >/dev/null 2>&1; then
    log "Installing packages with apt (Ubuntu / Debian family)"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y --no-install-recommends python3 python3-pip python3-yaml \
                       pciutils usbutils util-linux awscli
    # SSM agent is a snap on Ubuntu cloud images and already present.
else
    echo "[provision] unsupported distro: need dnf or apt-get" >&2
    exit 1
fi

# Make sure PyYAML really is importable (fall back to pip if the distro
# package name differed).
if ! python3 -c 'import yaml' 2>/dev/null; then
    log "PyYAML missing from packages, installing with pip"
    python3 -m ensurepip --upgrade 2>/dev/null || true
    python3 -m pip install --no-cache-dir pyyaml
fi

# The LLM planner uses the official Anthropic SDK.
log "Installing the anthropic SDK"
python3 -m pip install --no-cache-dir --upgrade "anthropic>=1.6" \
    || python3 -m pip install --no-cache-dir --upgrade --break-system-packages "anthropic>=1.6"
python3 -c 'import anthropic; print("[provision] anthropic", anthropic.__version__)'
command -v aws >/dev/null 2>&1 && log "aws cli: $(aws --version 2>&1 | head -1)" \
    || log "WARNING: aws cli missing; llm.api_key_ssm_parameter will not work"

# ---- 2. Agent code -------------------------------------------------------
log "Installing OpenClaw to $PREFIX"
rm -rf "$PREFIX"
mkdir -p "$PREFIX"
cp -r "$SRC/openclaw" "$PREFIX/openclaw"
find "$PREFIX" -name '__pycache__' -type d -prune -exec rm -rf {} +
python3 -m compileall -q "$PREFIX/openclaw" || true
chown -R root:root "$PREFIX"
chmod -R go-w "$PREFIX"

# ---- 3. Configuration ----------------------------------------------------
log "Installing configuration to $CONF_DIR"
mkdir -p "$CONF_DIR"
install -m 0644 "$SRC/rootfs/etc/openclaw/config-aws.yaml" "$CONF_DIR/config.yaml"
# Placeholder overlay so the agent can start even if bootstrap is skipped.
cat > "$CONF_DIR/cloud.yaml" << 'YAML'
# Overwritten by openclaw-bootstrap.service on every boot.
cloud:
  enabled: false
goals: []
YAML

# ---- 4. Launcher, service units, login banner ---------------------------
log "Installing launcher and systemd units"
install -m 0755 "$SRC/rootfs/usr/local/bin/openclaw" /usr/local/bin/openclaw
install -m 0644 "$SRC/aws/systemd/openclaw-bootstrap.service" /etc/systemd/system/
install -m 0644 "$SRC/aws/systemd/openclaw.service" /etc/systemd/system/
install -m 0644 "$SRC/aws/scripts/motd.sh" /etc/profile.d/openclaw.sh
mkdir -p /var/log
touch /var/log/openclaw.log /var/log/openclaw-security.log

systemctl daemon-reload
systemctl enable openclaw-bootstrap.service openclaw.service

# ---- 5. Agent-first OS tweaks -------------------------------------------
log "Applying agent-first OS settings"
# Identify the image.
cat > /etc/openclaw-release << EOR
OPENCLAW_VERSION=$OPENCLAW_VERSION
OPENCLAW_PROFILE=cloud
OPENCLAW_BUILT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOR
# Serial console gets the agent's output: keep a getty there too so the
# EC2 serial console can still be used for recovery.
systemctl enable serial-getty@ttyS0.service 2>/dev/null || true
# Sensible sysctls: the security scanner audits these on boot.
cat > /etc/sysctl.d/90-openclaw.conf << 'SYSCTL'
kernel.randomize_va_space = 2
kernel.kptr_restrict = 1
kernel.dmesg_restrict = 1
net.ipv4.ip_forward = 0
SYSCTL

# ---- 6. Sanity check inside the build instance --------------------------
log "Running self-test (3 headless cycles, no IMDS or API key required)"
PYTHONPATH="$PREFIX" python3 -m openclaw.main \
    --config "$CONF_DIR/config.yaml" --no-hardware --headless --no-llm \
    --max-cycles 3 --cycle-interval 0 --status-port 0 \
    --status-file /tmp/openclaw-selftest.json >/tmp/openclaw-selftest.log 2>&1 \
    || { cat /tmp/openclaw-selftest.log; echo "[provision] self-test failed" >&2; exit 1; }
python3 -c 'import json,sys; d=json.load(open("/tmp/openclaw-selftest.json")); sys.exit(0 if d["cycle_count"]==3 else 1)'
rm -f /tmp/openclaw-selftest.json /tmp/openclaw-selftest.log

log "Done. OpenClaw $OPENCLAW_VERSION installed; agent starts on first boot."
