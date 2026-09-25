#!/bin/sh
# /etc/profile.d/jarvis.sh - shown on every interactive login.
# The agent is the primary process on this instance; humans are guests.
[ -t 1 ] || return 0 2>/dev/null || exit 0
case "$-" in *i*) ;; *) return 0 2>/dev/null || exit 0 ;; esac

printf '\n  Jarvis agent-first instance\n'
printf '  -----------------------------\n'
if systemctl is-active --quiet jarvis 2>/dev/null; then
    printf '  Agent:   running (systemctl status jarvis)\n'
else
    printf '  Agent:   NOT running (journalctl -u jarvis -e)\n'
fi
printf '  Status:  jarvis --status   |  curl -s localhost:8471/status\n'
printf '  Logs:    journalctl -u jarvis -f\n'
printf '  Config:  /etc/jarvis/config.yaml  (+ cloud.yaml from user data)\n\n'
