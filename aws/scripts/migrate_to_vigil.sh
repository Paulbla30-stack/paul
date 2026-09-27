#!/bin/bash
# Move a running Jarvis box to Vigil, the name the agent chose on 27 September
# 2026. Run as root on the instance, from an unpacked release (PKG) that holds
# vigil/, rootfs/ and aws/.
#
#   migrate_to_vigil.sh plan      PKG   # read-only: what would move, and whether it can
#   migrate_to_vigil.sh apply     PKG   # do it; rolls itself back if Vigil does not come up
#   migrate_to_vigil.sh rollback  PKG   # put the Jarvis layout back
#
# What moves: /etc/jarvis, /var/lib/jarvis, /var/lib/jarvis-browser and
# /opt/jarvis-browsers to their vigil names, each leaving a symlink at the old
# path; the browser's user and group; /etc/default/jarvis; the sysctl file; the
# systemd units (the old ones are disabled and masked, so cloudflared's Wants=
# cannot pull the old bootstrap back in). What does not: the ledger's contents
# and writer, the memory database's contents, the old code in /usr/lib/jarvis
# (kept for rollback), and anything on the AWS side.
#
# Never run with `set -x`: /etc/vigil holds the UI token and the ledger key.
set -u
MODE=${1:-plan}
PKG=${2:-}
STAMP=$(date +%s)
# The box's config.yaml as deployed from commit 80e9ed6. Anything else means
# someone changed it by hand, and this script will not guess what to keep.
EXPECTED_CONFIG=4f680405a99ed496027d06e2a9999c976d12eab48bd13452d3c9d15b97ec3a19
MOVES="/etc/jarvis:/etc/vigil /var/lib/jarvis:/var/lib/vigil /var/lib/jarvis-browser:/var/lib/vigil-browser /opt/jarvis-browsers:/opt/vigil-browsers"
OLD_UNITS="jarvis.service jarvis-bootstrap.service jarvis-browser.service jarvis-health.service jarvis-health.timer"
NEW_UNITS="vigil.service vigil-bootstrap.service vigil-browser.service vigil-health.service vigil-health.timer"

say() { echo "== $*"; }
die() { echo "STOP: $*"; exit 1; }

preflight() {
  [ "$(id -u)" = 0 ] || die "must run as root"
  [ -n "$PKG" ] && [ -d "$PKG/vigil" ] && [ -d "$PKG/ledgerd" ] && [ -d "$PKG/tests/standing_refusals" ] \
    && [ -f "$PKG/rootfs/etc/vigil/config-aws.yaml" ] || die "no complete release at PKG=$PKG"
  for u in $NEW_UNITS cloudflared.service; do [ -f "$PKG/aws/systemd/$u" ] || die "release lacks $u"; done
  for m in $MOVES; do
    old=${m%%:*}; new=${m##*:}
    [ -d "$old" ] && [ ! -L "$old" ] || die "$old is not a plain directory (already migrated?)"
    [ ! -e "$new" ] || die "$new already exists"
  done
  getent passwd jarvis-browser >/dev/null || die "user jarvis-browser not found"
  ! getent passwd vigil-browser >/dev/null || die "user vigil-browser already exists"
  have=$(sha256sum /etc/jarvis/config.yaml | cut -d' ' -f1)
  [ "$have" = "$EXPECTED_CONFIG" ] || die "/etc/jarvis/config.yaml is not the known deployed version ($have)"
  free=$(df --output=avail -k /usr/lib | tail -1)
  [ "$free" -gt 204800 ] || die "less than 200 MB free"
  echo "preflight ok: dirs, user, config hash, disk (${free} KB free)"
}

plan() {
  preflight
  say "would move (each old path left as a symlink):"
  for m in $MOVES; do echo "   ${m%%:*} -> ${m##*:}  ($(du -sh "${m%%:*}" 2>/dev/null | cut -f1))"; done
  say "would install code to /usr/lib/vigil/vigil; /usr/lib/jarvis stays for rollback"
  say "would rename user/group jarvis-browser -> vigil-browser (uid $(id -u jarvis-browser) unchanged)"
  say "would stop, disable and mask: $OLD_UNITS"
  say "would install, enable and start: $NEW_UNITS; restart cloudflared with the new unit"
  say "would replace /etc/vigil/config.yaml with the release's (old copy kept beside it)"
}

install_code() {
  mkdir -p /usr/lib/vigil
  rm -rf /usr/lib/vigil/vigil.new
  cp -a "$PKG/vigil" /usr/lib/vigil/vigil.new
  find /usr/lib/vigil/vigil.new -name __pycache__ -type d -prune -exec rm -rf {} +
  [ -d /usr/lib/vigil/vigil ] && mv /usr/lib/vigil/vigil "/usr/lib/vigil/vigil.pre.$STAMP"
  mv /usr/lib/vigil/vigil.new /usr/lib/vigil/vigil
  # The same layout provision.sh makes: the vendored ledgerd beside the code,
  # and the standing refusals the boot gate runs.
  rm -rf /usr/lib/vigil/ledgerd /usr/lib/vigil/refusals
  cp -a "$PKG/ledgerd" /usr/lib/vigil/ledgerd
  cp -a "$PKG/tests/standing_refusals" /usr/lib/vigil/refusals
  find /usr/lib/vigil -name __pycache__ -type d -prune -exec rm -rf {} +
  chown -R root:root /usr/lib/vigil; chmod -R go-w /usr/lib/vigil
  for b in vigil vigil-health vigil-refusals; do install -m 0755 "$PKG/rootfs/usr/local/bin/$b" "/usr/local/bin/$b"; done
}

apply() {
  preflight
  say "install code"; install_code
  say "stop the old units"
  systemctl stop jarvis-health.timer jarvis.service jarvis-browser.service 2>/dev/null
  say "move directories"
  for m in $MOVES; do
    old=${m%%:*}; new=${m##*:}
    mv "$old" "$new" && ln -s "$new" "$old" && echo "   $old -> $new"
  done
  say "browser user"
  usermod -l vigil-browser -d /var/lib/vigil-browser jarvis-browser && groupmod -n vigil-browser jarvis-browser
  say "defaults, sysctl, release"
  [ -f /etc/default/jarvis ] && sed 's/JARVIS_/VIGIL_/g; s/Jarvis/Vigil/g' /etc/default/jarvis > /etc/default/vigil
  [ -f /etc/sysctl.d/90-jarvis.conf ] && mv /etc/sysctl.d/90-jarvis.conf /etc/sysctl.d/90-vigil.conf
  [ -f /etc/jarvis-release ] && cp -a /etc/jarvis-release /etc/vigil-release
  say "config"
  cp -a /etc/vigil/config.yaml "/etc/vigil/config.yaml.pre-vigil.$STAMP"
  install -m 0644 -o root -g root "$PKG/rootfs/etc/vigil/config-aws.yaml" /etc/vigil/config.yaml
  say "units"
  cp -a /etc/systemd/system/cloudflared.service "/etc/systemd/system/cloudflared.service.pre-vigil.$STAMP"
  for u in $NEW_UNITS cloudflared.service; do install -m 0644 "$PKG/aws/systemd/$u" /etc/systemd/system/; done
  systemctl disable $OLD_UNITS >/dev/null 2>&1
  for u in $OLD_UNITS; do mv "/etc/systemd/system/$u" "/etc/systemd/system/$u.pre-vigil.$STAMP"; done
  systemctl mask $OLD_UNITS >/dev/null 2>&1
  systemctl daemon-reload
  systemctl enable vigil-bootstrap.service vigil.service vigil-browser.service vigil-health.timer >/dev/null 2>&1
  say "bootstrap (writes /etc/vigil/cloud.yaml)"
  systemctl start vigil-bootstrap.service || { echo "bootstrap failed"; rollback_now; exit 1; }
  grep -q "writer: jarvis" /etc/vigil/cloud.yaml || { echo "cloud.yaml lacks the ledger writer"; rollback_now; exit 1; }
  if grep -qE "^\s+name: jarvis\s*$" /etc/vigil/cloud.yaml; then echo "cloud.yaml still names the agent jarvis"; rollback_now; exit 1; fi
  say "boot gate against the new code"
  if ! /usr/local/bin/vigil-refusals > "/tmp/vigil-gate.$STAMP" 2>&1; then
    tail -15 "/tmp/vigil-gate.$STAMP"; echo "GATE FAILED"; rollback_now; exit 1
  fi
  tail -1 "/tmp/vigil-gate.$STAMP"
  say "start"
  sysctl --system >/dev/null 2>&1
  systemctl start vigil-browser.service vigil.service vigil-health.timer
  systemctl restart cloudflared.service
  sleep 30
  ok=1
  for u in vigil vigil-browser cloudflared; do s=$(systemctl is-active $u); echo "   $u: $s"; [ "$s" = active ] || ok=0; done
  T=$(cat /run/vigil/token 2>/dev/null)
  name=$(curl -s -m 10 -H "Authorization: Bearer $T" http://127.0.0.1:8471/status | python3.11 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("name"))' 2>/dev/null)
  unset T
  echo "   status name: $name"
  [ "$name" = "Vigil" ] || ok=0
  if [ $ok != 1 ]; then journalctl -u vigil --since -60s --no-pager | tail -20; rollback_now; exit 1; fi
  say "done: Vigil is up. Old units masked; old code, unit files and config kept with .pre-vigil.$STAMP"
}

rollback_now() {
  say "ROLLING BACK"
  systemctl stop vigil.service vigil-browser.service vigil-health.timer 2>/dev/null
  systemctl disable vigil-bootstrap.service vigil.service vigil-browser.service vigil-health.timer >/dev/null 2>&1
  systemctl unmask $OLD_UNITS >/dev/null 2>&1
  for u in $OLD_UNITS; do
    b=$(ls -1t /etc/systemd/system/$u.pre-vigil.* 2>/dev/null | head -1)
    [ -n "$b" ] && mv "$b" "/etc/systemd/system/$u"
  done
  b=$(ls -1t /etc/systemd/system/cloudflared.service.pre-vigil.* 2>/dev/null | head -1)
  [ -n "$b" ] && cp -a "$b" /etc/systemd/system/cloudflared.service
  c=$(ls -1t /etc/vigil/config.yaml.pre-vigil.* 2>/dev/null | head -1)
  [ -n "$c" ] && cp -a "$c" /etc/vigil/config.yaml
  getent passwd vigil-browser >/dev/null && usermod -l jarvis-browser -d /var/lib/jarvis-browser vigil-browser \
    && groupmod -n jarvis-browser vigil-browser
  for m in $MOVES; do
    old=${m%%:*}; new=${m##*:}
    if [ -L "$old" ] && [ -d "$new" ]; then rm "$old" && mv "$new" "$old" && echo "   $new -> $old"; fi
  done
  [ -f /etc/sysctl.d/90-vigil.conf ] && mv /etc/sysctl.d/90-vigil.conf /etc/sysctl.d/90-jarvis.conf
  systemctl daemon-reload
  systemctl enable jarvis-bootstrap.service jarvis.service jarvis-browser.service jarvis-health.timer >/dev/null 2>&1
  systemctl start jarvis-browser.service jarvis.service jarvis-health.timer
  systemctl restart cloudflared.service
  sleep 15
  for u in jarvis jarvis-browser cloudflared; do echo "   $u: $(systemctl is-active $u)"; done
}

case "$MODE" in
  plan) plan ;;
  apply) apply ;;
  rollback) rollback_now ;;
  *) die "mode is plan, apply or rollback" ;;
esac
