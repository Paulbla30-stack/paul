#!/usr/bin/env bash
# Jarvis AMI provisioner
# ========================
# Runs as root on the temporary build instance.  Turns a stock Amazon
# Linux 2023 (or Ubuntu/Debian) image into an agent-first OS: the
# Jarvis agent is installed as a systemd service that starts before
# any human logs in, bootstraps itself from EC2 metadata, and logs to
# the serial console.
#
# Expects the repository either extracted at $SRC (default
# /tmp/jarvis-src) or uploaded as $SRC.tar.gz by the Packer "file"
# provisioner (see `make ami-bundle`).

set -euo pipefail

SRC="${SRC:-/tmp/jarvis-src}"
PREFIX="/usr/lib/jarvis"
CONF_DIR="/etc/jarvis"
JARVIS_VERSION="${JARVIS_VERSION:-1.0.0}"

log() { printf '[provision] %s\n' "$*"; }

if [ ! -d "$SRC/jarvis" ] && [ -f "$SRC.tar.gz" ]; then
    log "Extracting $SRC.tar.gz"
    mkdir -p "$SRC"
    tar -xzf "$SRC.tar.gz" -C "$SRC"
fi
[ -d "$SRC/jarvis" ] || { echo "[provision] source not found at $SRC" >&2; exit 1; }

# ---- 1. Packages ---------------------------------------------------------
# The anthropic SDK needs Python >= 3.10. Amazon Linux 2023's /usr/bin/python3
# is 3.9 for the life of the release, so on dnf systems we install 3.11 and
# point the agent at it via JARVIS_PYTHON (read by the launcher and units).
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
# pypdf is the one document format the standard library cannot reach: Word,
# Excel and PowerPoint files are zips full of XML and are read without a
# dependency, while a PDF is compressed streams and font encodings with no
# plain text anywhere. Pure Python, no system libraries, and without it every
# bill and statement the operator uploads comes back unread. The agent says
# so when it is missing rather than returning an empty document, so this is
# an improvement rather than a requirement -- but an unread bill is a useless
# assistant, so it goes in the image.
# pytest is not a development dependency here. The standing refusals are the
# boot gate: the unit runs them before the agent starts and the agent does not
# start if one fails. That makes the test runner part of the running system,
# and an image that cannot run its own refusals is an image whose refusals are
# a claim again.
log "Installing the anthropic SDK, boto3, the PDF reader and pytest"
$PY -m pip install --no-cache-dir --upgrade "anthropic>=1.6" "boto3>=1.34" "cryptography>=42" "pypdf>=4.0" "pytest>=7.0" \
    || $PY -m pip install --no-cache-dir --upgrade --break-system-packages "anthropic>=1.6" "boto3>=1.34" "cryptography>=42" "pypdf>=4.0" "pytest>=7.0"
$PY -c 'import anthropic, boto3, cryptography; print("[provision] anthropic", anthropic.__version__, "boto3", boto3.__version__, "cryptography", cryptography.__version__)'
$PY -c 'import pypdf; print("[provision] pypdf", pypdf.__version__)' \
    || log "WARNING: pypdf missing; PDFs will be reported as unreadable rather than read"
# Not a warning. Without pytest the boot gate cannot run, and a gate that
# cannot run has to stop the build rather than the box.
$PY -c 'import pytest; print("[provision] pytest", pytest.__version__)' \
    || { echo "[provision] pytest missing; the boot gate could not run" >&2; exit 1; }
command -v aws >/dev/null 2>&1 && log "aws cli: $(aws --version 2>&1 | head -1)" \
    || log "WARNING: aws cli missing; llm.api_key_ssm_parameter will not work"

# Tell the launcher and units which interpreter to use.
cat > /etc/default/jarvis << EOD
# Interpreter for the Jarvis agent (set by aws/scripts/provision.sh).
JARVIS_PYTHON=$PY
EOD

# ---- 2. Agent code -------------------------------------------------------
log "Installing Jarvis to $PREFIX"
rm -rf "$PREFIX"
mkdir -p "$PREFIX"
cp -r "$SRC/jarvis" "$PREFIX/jarvis"
cp -r "$SRC/ledgerd" "$PREFIX/ledgerd"
# The refusals ship with the code they are about. They are not a suite that
# lives in a repository and describes the box; they are what the box runs on
# itself before it starts.
cp -r "$SRC/tests/standing_refusals" "$PREFIX/refusals"
find "$PREFIX" -name '__pycache__' -type d -prune -exec rm -rf {} +
$PY -m compileall -q "$PREFIX/jarvis" || true
chown -R root:root "$PREFIX"
chmod -R go-w "$PREFIX"

# ---- 3. Configuration ----------------------------------------------------
log "Installing configuration to $CONF_DIR"
mkdir -p "$CONF_DIR"
install -m 0644 "$SRC/rootfs/etc/jarvis/config-aws.yaml" "$CONF_DIR/config.yaml"
# Placeholder overlay so the agent can start even if bootstrap is skipped.
cat > "$CONF_DIR/cloud.yaml" << 'YAML'
# Overwritten by jarvis-bootstrap.service on every boot.
cloud:
  enabled: false
goals: []
YAML

# ---- 4. Launcher, service units, login banner ---------------------------
log "Installing launcher and systemd units"
install -m 0755 "$SRC/rootfs/usr/local/bin/jarvis" /usr/local/bin/jarvis
install -m 0755 "$SRC/rootfs/usr/local/bin/jarvis-health" /usr/local/bin/jarvis-health
install -m 0755 "$SRC/rootfs/usr/local/bin/jarvis-refusals" /usr/local/bin/jarvis-refusals
install -m 0644 "$SRC/aws/systemd/jarvis-bootstrap.service" /etc/systemd/system/
install -m 0644 "$SRC/aws/systemd/jarvis.service" /etc/systemd/system/
install -m 0644 "$SRC/aws/systemd/jarvis-health.service" /etc/systemd/system/
install -m 0644 "$SRC/aws/systemd/jarvis-health.timer" /etc/systemd/system/
install -m 0644 "$SRC/aws/systemd/cloudflared.service" /etc/systemd/system/
install -m 0644 "$SRC/aws/scripts/motd.sh" /etc/profile.d/jarvis.sh
mkdir -p /var/log /var/lib/jarvis/uploads /var/lib/jarvis/documents \
         /etc/jarvis/tls /etc/jarvis/ledger
chmod 750 /var/lib/jarvis /var/lib/jarvis/uploads /var/lib/jarvis/documents
chmod 700 /etc/jarvis/tls /etc/jarvis/ledger
touch /var/log/jarvis.log /var/log/jarvis-security.log

# ---- 4b. Cloudflare Tunnel ----------------------------------------------
# How the operator reaches the UI without an inbound port. cloudflared dials
# out to Cloudflare and holds the connection open; the security group needs
# no ingress rule at all. It runs as its own unprivileged user, and the unit
# starts only when a token has been provisioned, so an instance without a
# tunnel is unaffected.
log "Installing cloudflared"
id -u cloudflared >/dev/null 2>&1 || \
    useradd --system --no-create-home --shell /sbin/nologin cloudflared
case "$(uname -m)" in
    aarch64|arm64) CF_ARCH=arm64 ;;
    *)             CF_ARCH=amd64 ;;
esac
# "latest" is what the build resolves by default; set CLOUDFLARED_VERSION to a
# tag (e.g. 2026.8.1) to pin the image to a known binary. Either way the
# version and the SHA-256 go in the build log, so the AMI's provenance is a
# matter of record rather than a matter of trust.
CF_VER="${CLOUDFLARED_VERSION:-latest}"
if [ "$CF_VER" = "latest" ]; then
    CF_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}"
else
    CF_URL="https://github.com/cloudflare/cloudflared/releases/download/${CF_VER}/cloudflared-linux-${CF_ARCH}"
fi
if curl -fsSL --retry 3 -o /usr/local/bin/cloudflared.new "$CF_URL"; then
    chmod 0755 /usr/local/bin/cloudflared.new
    mv /usr/local/bin/cloudflared.new /usr/local/bin/cloudflared
    log "cloudflared $(/usr/local/bin/cloudflared --version 2>&1 | head -1)"
    log "cloudflared sha256 $(sha256sum /usr/local/bin/cloudflared | cut -d" " -f1)"
else
    rm -f /usr/local/bin/cloudflared.new
    log "WARNING: could not download cloudflared; the tunnel unit will not start"
fi

systemctl daemon-reload
systemctl enable jarvis-bootstrap.service jarvis.service
systemctl enable jarvis-health.timer
# Conditioned on the token file, so it is a no-op without a tunnel.
systemctl enable cloudflared.service

# ---- 5. Agent-first OS tweaks -------------------------------------------
log "Applying agent-first OS settings"
# Identify the image.
cat > /etc/jarvis-release << EOR
JARVIS_VERSION=$JARVIS_VERSION
JARVIS_PROFILE=cloud
JARVIS_BUILT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOR
# Serial console gets the agent's output: keep a getty there too so the
# EC2 serial console can still be used for recovery.
systemctl enable serial-getty@ttyS0.service 2>/dev/null || true
# Sensible sysctls: the security scanner audits these on boot.
cat > /etc/sysctl.d/90-jarvis.conf << 'SYSCTL'
kernel.randomize_va_space = 2
kernel.kptr_restrict = 1
kernel.dmesg_restrict = 1
net.ipv4.ip_forward = 0
# The distro ships yama ptrace_scope = 0; the scanner expects 1, so set it
# here in the image rather than leaving the agent to find it every boot and
# try to change a running kernel. Root is exempt at scope 1, so the agent's
# own work is unaffected.
kernel.yama.ptrace_scope = 1
SYSCTL

# ---- 6. Sanity check inside the build instance --------------------------
log "Running self-test (3 headless cycles, no IMDS or API key required)"
PYTHONPATH="$PREFIX" $PY -m jarvis.main \
    --config "$CONF_DIR/config.yaml" --no-hardware --headless --no-llm \
    --max-cycles 3 --cycle-interval 0 --status-port 0 \
    --status-file /tmp/jarvis-selftest.json >/tmp/jarvis-selftest.log 2>&1 \
    || { cat /tmp/jarvis-selftest.log; echo "[provision] self-test failed" >&2; exit 1; }
$PY -c 'import json,sys; d=json.load(open("/tmp/jarvis-selftest.json")); sys.exit(0 if d["cycle_count"]==3 else 1)'
rm -f /tmp/jarvis-selftest.json /tmp/jarvis-selftest.log

# The gate the unit will run on every boot, run once here. An image that
# cannot pass its own standing refusals must not become an image: the
# alternative is an instance that boots into a failing gate and stops, and
# the first anyone hears of it is an agent that is not there.
log "Running the standing refusals (the boot gate the unit runs)"
/usr/local/bin/jarvis-refusals \
    || { echo "[provision] the standing refusals do not hold; no image" >&2; exit 1; }

log "Done. Jarvis $JARVIS_VERSION installed; agent starts on first boot."
