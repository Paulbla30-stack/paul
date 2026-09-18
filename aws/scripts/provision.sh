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
# The anthropic SDK needs Python >= 3.10. Amazon Linux 2023's /usr/bin/python3
# is 3.9 for the life of the release, so on dnf systems we install 3.11 and
# point the agent at it via OPENCLAW_PYTHON (read by the launcher and units).
PY=python3
if command -v dnf >/dev/null 2>&1; then
    log "Installing packages with dnf (Amazon Linux / Fedora family)"
    dnf -y update --security || true
    dnf -y install pciutils util-linux
    dnf -y install usbutils || true               # not packaged on AL2023; ISO-only tool
    dnf -y install amazon-ssm-agent || true      # preinstalled on AL2023
    dnf -y install awscli-2 || dnf -y install awscli || true   # preinstalled on AL2023
    systemctl enable amazon-ssm-agent 2>/dev/null || true
    if dnf -y install python3.11 python3.11-pip; then
        PY=/usr/bin/python3.11
        dnf -y install python3.11-pyyaml || true  # else pip installs PyYAML below
    else
        log "python3.11 not available; falling back to system python3"
        dnf -y install python3 python3-pip
        dnf -y install python3-pyyaml || true
    fi
elif command -v apt-get >/dev/null 2>&1; then
    log "Installing packages with apt (Ubuntu / Debian family)"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y --no-install-recommends python3 python3-pip python3-yaml \
                       pciutils usbutils util-linux
    apt-get install -y --no-install-recommends awscli || true
    # SSM agent is a snap on Ubuntu cloud images and already present.
else
    echo "[provision] unsupported distro: need dnf or apt-get" >&2
    exit 1
fi
log "Agent interpreter: $PY ($($PY --version 2>&1))"
$PY -c 'import sys; assert sys.version_info >= (3, 10), sys.version' \
    || { echo "[provision] $PY is older than 3.10; the anthropic SDK needs 3.10+" >&2; exit 1; }

# Make sure PyYAML really is importable (fall back to pip if the distro
# package name differed).
if ! $PY -c 'import yaml' 2>/dev/null; then
    log "PyYAML missing from packages, installing with pip"
    $PY -m ensurepip --upgrade 2>/dev/null || true
    $PY -m pip install --no-cache-dir pyyaml \
        || $PY -m pip install --no-cache-dir --break-system-packages pyyaml
fi

# The LLM planner uses the official Anthropic SDK (llm.provider: anthropic)
# or boto3 for Amazon Bedrock (llm.provider: bedrock). Install both so the
# provider is a config choice at launch time.
log "Installing the anthropic SDK and boto3"
$PY -m pip install --no-cache-dir --upgrade "anthropic>=1.6" "boto3>=1.34" \
    || $PY -m pip install --no-cache-dir --upgrade --break-system-packages "anthropic>=1.6" "boto3>=1.34"
$PY -c 'import anthropic, boto3; print("[provision] anthropic", anthropic.__version__, "boto3", boto3.__version__)'
command -v aws >/dev/null 2>&1 && log "aws cli: $(aws --version 2>&1 | head -1)" \
    || log "WARNING: aws cli missing; llm.api_key_ssm_parameter will not work"

# Tell the launcher and units which interpreter to use.
cat > /etc/default/openclaw << EOD
# Interpreter for the OpenClaw agent (set by aws/scripts/provision.sh).
OPENCLAW_PYTHON=$PY
EOD

# ---- 2. Agent code -------------------------------------------------------
log "Installing OpenClaw to $PREFIX"
rm -rf "$PREFIX"
mkdir -p "$PREFIX"
cp -r "$SRC/openclaw" "$PREFIX/openclaw"
find "$PREFIX" -name '__pycache__' -type d -prune -exec rm -rf {} +
$PY -m compileall -q "$PREFIX/openclaw" || true
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
mkdir -p /var/log /var/lib/openclaw/uploads /etc/openclaw/tls
chmod 750 /var/lib/openclaw/uploads
chmod 700 /etc/openclaw/tls
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
PYTHONPATH="$PREFIX" $PY -m openclaw.main \
    --config "$CONF_DIR/config.yaml" --no-hardware --headless --no-llm \
    --max-cycles 3 --cycle-interval 0 --status-port 0 \
    --status-file /tmp/openclaw-selftest.json >/tmp/openclaw-selftest.log 2>&1 \
    || { cat /tmp/openclaw-selftest.log; echo "[provision] self-test failed" >&2; exit 1; }
$PY -c 'import json,sys; d=json.load(open("/tmp/openclaw-selftest.json")); sys.exit(0 if d["cycle_count"]==3 else 1)'
rm -f /tmp/openclaw-selftest.json /tmp/openclaw-selftest.log

log "Done. OpenClaw $OPENCLAW_VERSION installed; agent starts on first boot."
