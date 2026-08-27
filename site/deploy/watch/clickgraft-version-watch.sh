#!/bin/sh
# Watch HP for a macOS build newer than the one ClickGraft supports.
#
#   clickgraft-version-watch.sh            # probe, notify on a find
#   clickgraft-version-watch.sh --dry-run  # probe, print, send nothing
#
# WHY THIS PROBES FILENAMES INSTEAD OF READING HP's PAGE
# HP's download page is an Angular app: plain curl sees no download URL at all,
# and there is no public JSON behind it that answers without a browser. The file
# host has no directory index either — every path, including /pub/, returns a
# bare "Not found", and only exact file URLs resolve. So the only thing that
# works from a cron job is asking for specific names.
#
# THE CONTROL IS THE POINT
# A sweep that finds nothing looks exactly like a sweep that can no longer reach
# HP. So every run first asks for the version we KNOW exists. If that stops
# answering, this reports that it is blind rather than reporting "no new
# version" — the same failure this project has already been bitten by twice.
set -eu

CONF="${CLICKGRAFT_WATCH_CONF:-/etc/clickgraft-watch.conf}"
[ -f "$CONF" ] || { echo "✗ no config at $CONF" >&2; exit 1; }
# shellcheck disable=SC1090
. "$CONF"
: "${NTFY_URL:?}" ; : "${NTFY_TOPIC:?}" ; : "${NTFY_USER:?}" ; : "${NTFY_PASS:?}"

STATE="${STATE:-/var/lib/clickgraft-watch}"
SEEN="$STATE/versions-seen"
BASE="https://ftp.hp.com/pub/softlib/software13/printers/hpdesignjetclick"
KNOWN="${KNOWN_MAC_VERSION:-4.8.117}"
UA='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15'
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

mkdir -p "$STATE"; touch "$SEEN"

probe() {  # probe <version> -> prints http code
  # Range request: this asks for one byte, so a hit costs nothing even though
  # the real file is over half a gigabyte.
  curl -s -o /dev/null -m 25 -L -r 0-0 -A "$UA" -w '%{http_code}' \
       "$BASE/HPClick-$1.dmg" 2>/dev/null || echo 000
}

say() {  # say <title> <priority> <tags> <body>
  if [ "$DRY" = "1" ]; then printf '  would send [%s] %s — %s\n' "$3" "$1" "$4"; return 0; fi
  curl -sS --max-time 20 -u "$NTFY_USER:$NTFY_PASS" \
       -H "Title: $1" -H "Priority: $2" -H "Tags: $3" -d "$4" \
       "$NTFY_URL/$NTFY_TOPIC" >/dev/null || echo "✗ publish failed: $1" >&2
}

# ---- control ---------------------------------------------------------------
ctl=$(probe "$KNOWN")
case "$ctl" in
  200|206) : ;;
  *)
    echo "✗ control failed: HPClick-$KNOWN.dmg returned $ctl" >&2
    say "ClickGraft: version watch is blind" 4 warning \
"Cannot see HP any more. The known-good build HPClick-$KNOWN.dmg returned HTTP $ctl, so a sweep finding nothing would mean nothing. Check whether HP moved the download path."
    exit 1
    ;;
esac

# ---- candidates ------------------------------------------------------------
# Windows is on 4.10.x while macOS sits on 4.8.117, so a Mac release could
# reasonably be a patch bump, a 4.9, or a jump to parity with Windows.
cands="4.9.118 4.10.38"
i=118; while [ $i -le 130 ]; do cands="$cands 4.8.$i"; i=$((i+1)); done
i=110; while [ $i -le 125 ]; do cands="$cands 4.9.$i";  i=$((i+1)); done
i=30;  while [ $i -le 45  ]; do cands="$cands 4.10.$i"; i=$((i+1)); done

found=""
for v in $cands; do
  grep -qx "$v" "$SEEN" 2>/dev/null && continue   # already reported
  code=$(probe "$v")
  case "$code" in
    200|206)
      found="$found $v"
      printf '%s\n' "$v" >> "$SEEN"
      echo "FOUND HPClick-$v.dmg"
      ;;
  esac
  sleep 1   # HP is doing us a favour hosting this; do not hammer it
done

if [ -n "$found" ]; then
  for v in $found; do
    say "HP released a new Mac build: $v" 5 tada \
"HPClick-$v.dmg is now on HP's server.
$BASE/HPClick-$v.dmg

ClickGraft supports $KNOWN. A new manifest is needed before it can graft this one."
  done
else
  echo "no new macOS build (control $KNOWN ok, $(echo "$cands" | wc -w | tr -d ' ') candidates checked)"
fi
