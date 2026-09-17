#!/usr/bin/env bash
# OpenClaw AMI cleanup
# ====================
# Last Packer step.  Removes anything instance-specific so every launch
# from the AMI starts clean: cloud-init state, SSH host keys, logs,
# shell history, the build source tree and the build user's authorized
# keys are all regenerated or re-injected by EC2 on first boot.

set -euo pipefail
log() { printf '[cleanup] %s\n' "$*"; }

log "Removing build source"
rm -rf /tmp/openclaw-src /tmp/openclaw-src.tar.gz

log "Resetting cloud-init"
if command -v cloud-init >/dev/null 2>&1; then
    cloud-init clean --logs --seed 2>/dev/null || cloud-init clean --logs || true
fi
rm -rf /var/lib/cloud/instances/* /var/lib/cloud/instance 2>/dev/null || true

log "Removing SSH host keys (regenerated on first boot)"
rm -f /etc/ssh/ssh_host_*

log "Clearing logs and caches"
find /var/log -type f -name '*.gz' -delete 2>/dev/null || true
find /var/log -type f -name '*.[0-9]' -delete 2>/dev/null || true
for f in /var/log/messages /var/log/secure /var/log/cloud-init.log \
         /var/log/cloud-init-output.log /var/log/openclaw.log \
         /var/log/openclaw-security.log /var/log/dnf.log /var/log/dnf.rpm.log \
         /var/log/syslog /var/log/auth.log; do
    [ -f "$f" ] && : > "$f"
done
journalctl --rotate 2>/dev/null || true
journalctl --vacuum-time=1s 2>/dev/null || true
rm -rf /var/cache/dnf/* /var/cache/apt/archives/*.deb 2>/dev/null || true
rm -rf /tmp/* /var/tmp/* 2>/dev/null || true

log "Clearing shell history and machine identity"
rm -f /root/.bash_history /home/*/.bash_history
unset HISTFILE
: > /etc/machine-id
rm -f /var/lib/dbus/machine-id
ln -sf /etc/machine-id /var/lib/dbus/machine-id 2>/dev/null || true

log "Removing build user credentials (EC2 injects the launch key pair)"
for home in /home/ec2-user /home/ubuntu /home/admin; do
    [ -f "$home/.ssh/authorized_keys" ] && : > "$home/.ssh/authorized_keys"
done
rm -f /root/.ssh/authorized_keys

sync
log "Done. Image is ready to be snapshotted."
