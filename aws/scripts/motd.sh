#!/bin/sh
# /etc/profile.d/vigil.sh - shown on every interactive login.
# The agent is the primary process on this instance; humans are guests.
[ -t 1 ] || return 0 2>/dev/null || exit 0
case "$-" in *i*) ;; *) return 0 2>/dev/null || exit 0 ;; esac

printf '\n  Vigil agent-first instance\n'
printf '  -----------------------------\n'
if systemctl is-active --quiet vigil 2>/dev/null; then
    printf '  Agent:   running (systemctl status vigil)\n'
else
    printf '  Agent:   NOT running (journalctl -u vigil -e)\n'
fi
printf '  Status:  vigil --status   |  curl -s localhost:8471/status\n'
printf '  Logs:    journalctl -u vigil -f\n'
printf '  Config:  /etc/vigil/config.yaml  (+ cloud.yaml from user data)\n\n'
