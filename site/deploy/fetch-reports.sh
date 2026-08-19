#!/usr/bin/env bash
# Read what users have sent. Like the traffic stats, these are never served —
# they come back over SSH on demand.
#
#   ./site/deploy/fetch-reports.sh          # counts
#   ./site/deploy/fetch-reports.sh --all    # print them
#
# Two kinds, filed apart on purpose. "problem" is a queue to work through.
# "result" is the denominator — without it the only signal is complaints, and
# silence reads identically whether the tool works perfectly or not at all.
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/_common.sh"
require_host

VOL="${REPORT_VOL:-/var/lib/docker/volumes/edge_clickgraft_reports/_data}"

if [ "${1:-}" = "--all" ]; then
  ct "
    n=0
    for f in $VOL/*.txt; do
      [ -f \"\$f\" ] || continue
      n=\$((n+1)); echo \"===== \$(basename \"\$f\") =====\"; cat \"\$f\"; echo
    done
    [ \"\$n\" = 0 ] && echo 'no reports yet'
    exit 0
  "
else
  ct "
    r=\$(ls -1 $VOL/result-*.txt 2>/dev/null | wc -l | tr -d ' ')
    p=\$(ls -1 $VOL/problem-*.txt 2>/dev/null | wc -l | tr -d ' ')
    echo \"  \${r:-0} working, \${p:-0} problem(s)\"
    ls -1 $VOL/*.txt 2>/dev/null | sed 's|.*/|    |' || true
    exit 0
  "
fi
