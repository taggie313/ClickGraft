#!/usr/bin/env bash
# Read the traffic numbers. Nothing about the stats is reachable from the
# internet by design, so this pulls them over SSH on demand.
#
#   ./site/deploy/fetch-stats.sh          # the summary
#   ./site/deploy/fetch-stats.sh --html   # also pull the GoAccess report and open it
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/_common.sh"
require_host

# Both files: one rotation happened before rotation was turned off, and those
# lines are real history rather than something to tidy away.
VOL="${LOG_VOL:-/var/lib/docker/volumes/edge_logs/_data}"

ct "
  set -e
  cat $VOL/clickgraft-access.log.1 $VOL/clickgraft-access.log 2>/dev/null > /tmp/cg-access.log || true
  [ -s /tmp/cg-access.log ] || { echo \"the access log exists but is empty — nginx may not be writing\" >&2; exit 3; }
  sh $REMOTE_DIR/summary.sh /tmp/cg-access.log
"

if [ "${1:-}" = "--html" ]; then
  OUT="/tmp/clickgraft-report.html"
  ct "cat $REMOTE_DIR/stats/report.html" > "$OUT"
  [ -s "$OUT" ] || die "the GoAccess report came back empty"
  echo; echo "wrote $OUT"
  command -v open >/dev/null && open "$OUT"
fi
