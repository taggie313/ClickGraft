#!/bin/sh
# One notification a day summarising yesterday, so the numbers can be read
# without asking anyone.
#
#   clickgraft-digest.sh            # yesterday, publish
#   clickgraft-digest.sh --dry-run  # print what it would send
#   clickgraft-digest.sh --day 27/Aug/2026
#
# It runs summary.sh over a one-day slice of the log rather than classifying
# anything itself. There is one answer to "is this a real visitor?" in this
# project and it lives in summary.sh; a second copy here would drift and then
# quietly disagree, which is worse than having no digest at all.
#
# It always sends, even on a day with nothing. A digest that only appears when
# something happened cannot be distinguished from a digest that has stopped
# working — and this project has already had one watcher sit "active" for four
# days while silently emitting nothing. A quiet day goes out at min priority so
# it lands without making a sound.
set -eu

CONF="${CLICKGRAFT_WATCH_CONF:-/etc/clickgraft-watch.conf}"
[ -f "$CONF" ] || { echo "✗ no config at $CONF" >&2; exit 1; }
# shellcheck disable=SC1090
. "$CONF"

: "${NTFY_URL:?set NTFY_URL in $CONF}"
: "${NTFY_TOPIC:?set NTFY_TOPIC in $CONF}"
: "${NTFY_USER:?set NTFY_USER in $CONF}"
: "${NTFY_PASS:?set NTFY_PASS in $CONF}"
LOG="${LOG:-/var/lib/docker/volumes/edge_logs/_data/clickgraft-access.log}"
SUMMARY="${SUMMARY:-/opt/edge/sites/clickgraft/summary.sh}"

DRY=0
DAY="$(date -u -d 'yesterday' '+%d/%b/%Y' 2>/dev/null || date -u -v-1d '+%d/%b/%Y')"
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --day) shift; DAY="${1:?--day needs a date like 27/Aug/2026}" ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

[ -f "$LOG" ]     || { echo "✗ access log not found: $LOG" >&2; exit 1; }
[ -f "$SUMMARY" ] || { echo "✗ summary.sh not found: $SUMMARY" >&2; exit 1; }

SLICE="$(mktemp)"; trap 'rm -f "$SLICE"' EXIT
grep "\[$DAY:" "$LOG" > "$SLICE" || true

if [ ! -s "$SLICE" ]; then
  VIEWS=0; UNIQ=0; DOWNLOADS=0; SRC=""; TAGS=""
else
  # NOT `2>/dev/null || true`. Swallowing summary.sh's exit status made a broken
  # summary.sh report a busy day as zero visitors and zero downloads — a wrong
  # number that looks exactly like a quiet day. If the thing that counts cannot
  # run, say so instead of publishing a figure nobody can trust.
  if ! OUT="$(sh "$SUMMARY" "$SLICE" 2>&1)"; then
    echo "✗ summary.sh failed; refusing to publish numbers from it:" >&2
    printf '%s\n' "$OUT" >&2
    exit 1
  fi
  # TOTAL row: DAY VIEWS UNIQUE DOWNLOADS
  VIEWS=$(printf '%s\n' "$OUT"     | awk '$1=="TOTAL"{print $2+0}')
  UNIQ=$(printf '%s\n' "$OUT"      | awk '$1=="TOTAL"{print $3+0}')
  DOWNLOADS=$(printf '%s\n' "$OUT" | awk '$1=="TOTAL"{print $4+0}')
  # $1 must be a NUMBER. Both sections print "(none recorded)" when empty, and a
  # trailing explanatory line follows the tag section — without this guard the
  # digest cheerfully reported "tagged: (none×recorded) (blank×means".
  SRC=$(printf '%s\n' "$OUT"  | awk '/^WHERE THEY CAME FROM/{f=1;next} /^$/{f=0} f && $1 ~ /^[0-9]+$/ {printf "%s×%s ", $1, $2}')
  TAGS=$(printf '%s\n' "$OUT" | awk '/^TAGGED LINKS/{f=1;next} /^$/{f=0} f && $1 ~ /^[0-9]+$/ {printf "%s×%s ", $1, $2}')
fi
VIEWS=${VIEWS:-0}; UNIQ=${UNIQ:-0}; DOWNLOADS=${DOWNLOADS:-0}

# New vs returning installs. The per-event notification cannot tell these apart
# — it suppresses repeats for 24h, so a machine coming back after a day looks
# exactly like a first install, which has caused real confusion. Here the whole
# log is available, so first-seen is knowable.
COUNTS=$(awk -F'"' -v day="$DAY" '
  $6 ~ /^ClickGraft\// {
    split($1, f, " "); pfx = f[1]; d = f[4]; sub(/^\[/, "", d); split(d, dd, ":"); dday = dd[1]
    if (!(pfx in firstday)) firstday[pfx] = dday
    if (dday == day) today[pfx] = 1
  }
  END {
    for (p in today) { if (firstday[p] == day) n++; else r++ }
    printf "%d %d", n+0, r+0
  }' "$LOG")
NEW=$(echo "$COUNTS" | cut -d' ' -f1)
RET=$(echo "$COUNTS" | cut -d' ' -f2)

PRETTY=$(echo "$DAY" | tr '/' ' ')
if [ "$((VIEWS + DOWNLOADS + NEW + RET))" -eq 0 ]; then
  TITLE="ClickGraft: quiet day"
  PRIO=1
  BODY="$PRETTY — nothing at all. (This digest still runs; silence here means silence, not a broken watcher.)"
else
  TITLE="ClickGraft: $UNIQ visitor(s), $DOWNLOADS download(s)"
  PRIO=3
  BODY="$PRETTY
visitors $UNIQ (from $VIEWS page view(s))
downloads $DOWNLOADS
installs $NEW new, $RET returning"
  # if/fi, not `[ ... ] && ...`: under set -e a false test makes that statement
  # return non-zero and kills the script, so a day with no referrers would send
  # nothing at all — the exact silent failure this digest exists to rule out.
  if [ -n "$SRC" ]; then BODY="$BODY
from: $SRC"; fi
  if [ -n "$TAGS" ]; then BODY="$BODY
tagged: $TAGS"; fi
fi

if [ "$DRY" = "1" ]; then
  printf 'would send [priority %s]\n  %s\n%s\n' "$PRIO" "$TITLE" "$BODY"
  exit 0
fi

if ! curl -sS --max-time 20 -u "$NTFY_USER:$NTFY_PASS" \
      -H "Title: $TITLE" -H "Priority: $PRIO" -H "Tags: bar_chart" \
      -d "$BODY" "$NTFY_URL/$NTFY_TOPIC" >/dev/null; then
  echo "✗ digest publish failed" >&2
  exit 1
fi
echo "digest sent for $DAY"
