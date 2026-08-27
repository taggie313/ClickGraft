#!/usr/bin/env bash
# Install (or update) the visitor watcher in the CT.
#
#   ./site/deploy/watch/install-watch.sh          # install / update
#   ./site/deploy/watch/install-watch.sh --test   # ... and send a [TEST] ping
#
# Reads the ntfy settings from site/deploy/deploy.env, which is gitignored —
# this directory is published, so the URL, topic and password must not be in it.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
. "$HERE/../_common.sh"

: "${NTFY_URL:?set NTFY_URL in site/deploy/deploy.env}"
: "${NTFY_TOPIC:?set NTFY_TOPIC in site/deploy/deploy.env}"
: "${NTFY_USER:?set NTFY_USER in site/deploy/deploy.env}"
: "${NTFY_PASS:?set NTFY_PASS in site/deploy/deploy.env}"

require_host

SCRIPT_B64="$(base64 < "$HERE/clickgraft-watch.sh" | tr -d '\n')"
DIGEST_B64="$(base64 < "$HERE/clickgraft-digest.sh" | tr -d '\n')"
DSVC_B64="$(base64 < "$HERE/clickgraft-digest.service" | tr -d '\n')"
DTMR_B64="$(base64 < "$HERE/clickgraft-digest.timer" | tr -d '\n')"
UNIT_B64="$(base64 < "$HERE/clickgraft-watch.service" | tr -d '\n')"
# The config carries the publish password, so it is built here and written with
# a restrictive umask rather than echoed into a world-readable file.
# LOG is written into the config rather than left to the script's default.
# The default was CT 117's own docker volume; on the shared edge host the file
# lives under edge_logs, and a watcher pointed at a path that no longer exists
# fails at startup instead of silently watching nothing.
WATCH_LOG="${LOG_VOL:-/var/lib/docker/volumes/edge_logs/_data}/clickgraft-access.log"
SUMMARY_PATH="${REMOTE_DIR:-/opt/edge/sites/clickgraft}/summary.sh"
CONF_B64="$(printf 'NTFY_URL=%s\nNTFY_TOPIC=%s\nNTFY_USER=%s\nNTFY_PASS=%s\nLOG=%s\nSUMMARY=%s\n' \
              "$NTFY_URL" "$NTFY_TOPIC" "$NTFY_USER" "$NTFY_PASS" "$WATCH_LOG" "$SUMMARY_PATH" | base64 | tr -d '\n')"

echo "==> installing into CT ${CT_ID}"
ct "
set -eu
umask 077
echo '$CONF_B64'   | base64 -d > /etc/clickgraft-watch.conf
umask 022
echo '$SCRIPT_B64' | base64 -d > /usr/local/bin/clickgraft-watch.sh
chmod 0755 /usr/local/bin/clickgraft-watch.sh
echo '$UNIT_B64'   | base64 -d > /etc/systemd/system/clickgraft-watch.service
mkdir -p /var/lib/clickgraft-watch
command -v curl >/dev/null || { apt-get update -qq && apt-get install -y -qq curl >/dev/null; }
echo '$DIGEST_B64' | base64 -d > /usr/local/bin/clickgraft-digest.sh
chmod 0755 /usr/local/bin/clickgraft-digest.sh
echo '$DSVC_B64' | base64 -d > /etc/systemd/system/clickgraft-digest.service
echo '$DTMR_B64' | base64 -d > /etc/systemd/system/clickgraft-digest.timer
systemctl daemon-reload
systemctl enable clickgraft-watch.service >/dev/null 2>&1 || true
# restart, not start: this script is also the update path.
systemctl restart clickgraft-watch.service
systemctl enable --now clickgraft-digest.timer >/dev/null 2>&1 || true
sleep 2
systemctl is-active clickgraft-watch.service
"

echo "==> status"
ct "systemctl --no-pager --lines=8 status clickgraft-watch.service | sed 's/^/    /'" || true

if [ "${1:-}" = "--test" ]; then
  echo "==> sending a [TEST] notification"
  ct "/usr/local/bin/clickgraft-watch.sh --selftest"
fi

echo
echo "Watching for real visitors now. Follow it with:"
echo "  ssh $PVE_HOST \"pct exec $CT_ID -- journalctl -fu clickgraft-watch\""
