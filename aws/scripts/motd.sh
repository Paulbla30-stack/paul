#!/bin/sh
# /etc/profile.d/openclaw.sh - shown on every interactive login.
# The agent is the primary process on this instance; humans are guests.
[ -t 1 ] || return 0 2>/dev/null || exit 0
case "$-" in *i*) ;; *) return 0 2>/dev/null || exit 0 ;; esac

printf '\n  OpenClaw agent-first instance\n'
printf '  -----------------------------\n'
if systemctl is-active --quiet openclaw 2>/dev/null; then
    printf '  Agent:   running (systemctl status openclaw)\n'
else
    printf '  Agent:   NOT running (journalctl -u openclaw -e)\n'
fi
printf '  Status:  openclaw --status   |  curl -s localhost:8471/status\n'
printf '  Logs:    journalctl -u openclaw -f\n'
printf '  Config:  /etc/openclaw/config.yaml  (+ cloud.yaml from user data)\n\n'
