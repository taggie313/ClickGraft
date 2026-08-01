#!/usr/bin/env bash
# Read bug reports users have sent. Like the traffic stats, these are never
# served — they come back over SSH on demand.
#
#   ./site/deploy/fetch-reports.sh          # list them
#   ./site/deploy/fetch-reports.sh --all    # print them
set -euo pipefail
_ENV="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/deploy.env"
# shellcheck disable=SC1090
[ -f "$_ENV" ] && . "$_ENV"
PVE_HOST="${PVE_HOST:?set PVE_HOST in site/deploy/deploy.env}"
CT_ID="${CT_ID:-117}"
VOL="/var/lib/docker/volumes/clickgraft_reports/_data"

if [ "${1:-}" = "--all" ]; then
  ssh "$PVE_HOST" "pct exec $CT_ID -- sh -lc '
    for f in $VOL/*.txt; do
      [ -f \"\$f\" ] || { echo \"no reports\"; exit 0; }
      echo \"===== \$(basename \"\$f\") =====\"; cat \"\$f\"; echo
    done'"
else
  ssh "$PVE_HOST" "pct exec $CT_ID -- sh -lc '
    ls -1 $VOL/*.txt 2>/dev/null | wc -l | tr -d \" \" | sed \"s|\$| report(s)|\"
    ls -lh $VOL 2>/dev/null | tail -n +2 | awk \"{print \\\$9, \\\$5}\"'"
fi
