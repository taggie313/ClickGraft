#!/bin/sh
# Delete reports older than RETAIN_DAYS, keeping only a count.
#
# A report is the only thing this project stores that can point at a person: the
# free text someone wrote, and an optional email address they typed in. The site
# promises they go after 90 days, so something has to actually do it.
#
# The COUNT is kept deliberately. collector.py files "result" apart from
# "problem" because result is the denominator -- without it the only signal is
# complaints, and silence reads identically whether the tool works perfectly or
# not at all. Deleting the files without recording them would quietly destroy
# that denominator. The tally keeps the number and drops the text.
set -eu

# Follow the same config every other tool here follows. install-watch.sh writes
# REPORTS= into it from REPORT_VOL, precisely so a moved docker volume does not
# leave something pointed at a path that no longer exists -- which has already
# happened once on this project, when CT 117's volume became the shared edge
# host's. A sweep that enforces a PUBLISHED promise is the worst possible place
# to rediscover that.
CONF="${CLICKGRAFT_WATCH_CONF:-/etc/clickgraft-watch.conf}"
# shellcheck disable=SC1090
[ -f "$CONF" ] && . "$CONF"

REPORTS="${REPORTS:-/var/lib/docker/volumes/edge_clickgraft_reports/_data}"
RETAIN_DAYS="${RETAIN_DAYS:-90}"
TALLY="$REPORTS/.tally"
LIST="/tmp/clickgraft-retention.$$"

# exit 1, not 0. The page says reports are deleted after 90 days; if this cannot
# see them it is not keeping that promise, and the only way anyone finds out is
# the unit going red. Exiting 0 here would make silence mean both "swept" and
# "never ran".
[ -d "$REPORTS" ] || { echo "✗ no reports directory at $REPORTS" >&2; exit 1; }

# -mtime +N is "more than N*24h ago", so +90 first matches on the 91st day.
# Off by a day in the safe direction: nothing goes before the page says it does.
# The tally is a dotfile, so *.txt cannot match it and the sweep cannot eat its
# own record.
find "$REPORTS" -maxdepth 1 -type f -name '*.txt' -mtime "+$RETAIN_DAYS" > "$LIST"

while IFS= read -r f; do
  [ -f "$f" ] || continue
  base=$(basename "$f")
  # problem-20260908-064328-523330-ID.txt -> kind=problem, cc=ID
  kind=${base%%-*}
  cc=${base##*-}; cc=${cc%.txt}
  printf '%s %s %s\n' "$(date -u +%Y-%m-%d)" "$kind" "$cc" >> "$TALLY"
  rm -f "$f"
  echo "retired $base"
done < "$LIST"

n=$(wc -l < "$LIST" | tr -d ' ')
rm -f "$LIST"
echo "retention sweep: ${n} report(s) older than ${RETAIN_DAYS} days removed"
exit 0
