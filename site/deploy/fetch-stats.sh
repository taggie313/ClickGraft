#!/usr/bin/env bash
# Read the traffic numbers. Nothing about the stats is reachable from the
# internet by design, so this pulls them over SSH on demand.
#
#   ./site/deploy/fetch-stats.sh          # the summary
#   ./site/deploy/fetch-stats.sh --html   # also pull GoAccess's report and open it
set -euo pipefail

# Local settings — host, container id, paths. Not in git; see deploy.env.example.
_ENV="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/deploy.env"
# shellcheck disable=SC1090
[ -f "$_ENV" ] && . "$_ENV"

PVE_HOST="${PVE_HOST:?set PVE_HOST in site/deploy/deploy.env}"
CT_ID="${CT_ID:-117}"
REMOTE_DIR="${REMOTE_DIR:-/opt/clickgraft}"
# The named volume as the CT sees it. Reading it directly rather than through
# `compose exec` means the summary still works when the web container is down,
# which is exactly when you want to look at it.
VOL="/var/lib/docker/volumes/clickgraft_logs/_data"

ssh "$PVE_HOST" "pct exec $CT_ID -- sh -lc '
  set -e
  cat $VOL/clickgraft-access.log $VOL/clickgraft-access.log.1 2>/dev/null > /tmp/cg-access.log || true
  sh $REMOTE_DIR/summary.sh /tmp/cg-access.log
'"

if [ "${1:-}" = "--html" ]; then
  OUT="/tmp/clickgraft-report.html"
  ssh "$PVE_HOST" "pct exec $CT_ID -- cat $REMOTE_DIR/stats/report.html" > "$OUT"
  echo; echo "wrote $OUT"
  command -v open >/dev/null && open "$OUT"
fi
