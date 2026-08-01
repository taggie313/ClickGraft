#!/bin/sh
# Rebuild the traffic report every 10 minutes from nginx's access log.
#
# Deliberately a loop rather than goaccess's --real-time-html: that mode opens
# a websocket and wants a browser pointed at it, which would mean exposing
# something. This just writes a file. Read it with deploy/fetch-stats.sh.
set -eu

LOG=/srv/logs/clickgraft-access.log
OUT=/srv/report/report.html
DB=/srv/report/db

mkdir -p "$DB"

# The log format has to match nginx's `privacy` format exactly, field for
# field, or goaccess silently parses nothing and reports an empty site.
LOGFMT='%h - - [%d:%t %^] "%m %U %H" %s %b "%R" "%u" "%^"'

while :; do
  if [ -f "$LOG" ]; then
    # --persist/--restore keep a running total across log rotations, so the
    # 30-day retention on the raw logs doesn't erase the history that matters.
    goaccess "$LOG" \
      --log-format="$LOGFMT" \
      --date-format='%d/%b/%Y' \
      --time-format='%H:%M:%S' \
      --persist --restore --db-path="$DB" \
      --agent-list \
      --ignore-crawlers \
      --html-report-title='ClickGraft — clickgraft.elusive.net' \
      -o "$OUT" 2>/dev/null || true
  fi
  sleep 600
done
