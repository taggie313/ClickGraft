#!/bin/sh
# Ping ntfy when a person — not a crawler — turns up at the site.
#
#   clickgraft-watch.sh                  # the service: watch the log, notify
#   clickgraft-watch.sh --once           # process new lines once and exit
#   clickgraft-watch.sh --selftest       # send one [TEST] notification
#   clickgraft-watch.sh --dry-run FILE   # print what a log WOULD have sent
#
# It POLLS the log rather than tailing it. The first version piped
# `tail -n0 -F` into awk into a read loop, and in this container that pipeline
# delivered nothing at all — it ran for four days across real visits and never
# emitted one event, while systemd cheerfully reported the service as active.
# Polling a byte offset has no such failure mode, is testable in one shot with
# --once, and a delay of POLL_SECONDS is irrelevant for "someone visited".
#
# Runs inside the CT as a systemd service; config lives in
# /etc/clickgraft-watch.conf (mode 600, because it holds the publish password).
#
# The classification is copied from summary.sh on purpose. Two different answers
# to "is this a real visitor?" in one project is how you end up trusting the
# wrong one; if the rule changes, change it in both.
#
# Dedup deliberately happens in the shell rather than in awk. awk's systime()
# and mktime() are gawk extensions and this container has mawk, so the obvious
# in-awk implementation would parse clean and then never suppress anything.
set -eu

CONF="${CLICKGRAFT_WATCH_CONF:-/etc/clickgraft-watch.conf}"
[ -f "$CONF" ] || { echo "✗ no config at $CONF" >&2; exit 1; }
# shellcheck disable=SC1090
. "$CONF"

: "${NTFY_URL:?set NTFY_URL in $CONF}"
: "${NTFY_TOPIC:?set NTFY_TOPIC in $CONF}"
: "${NTFY_USER:?set NTFY_USER in $CONF}"
: "${NTFY_PASS:?set NTFY_PASS in $CONF}"
LOG="${LOG:-/var/lib/docker/volumes/clickgraft_logs/_data/clickgraft-access.log}"
STATE="${STATE:-/var/lib/clickgraft-watch}"
DRY_RUN="${DRY_RUN:-0}"
# One ping per prefix per kind per window. A visit is several requests (page,
# icon, favicon) and a person who reads the page twice is not two people.
QUIET_VIEW="${QUIET_VIEW:-1800}"
QUIET_DOWNLOAD="${QUIET_DOWNLOAD:-1800}"
QUIET_APP="${QUIET_APP:-86400}"
# How long after a page view its confirming asset request may arrive. Real
# browsers do it in well under a second; this is slack for a slow connection.
CONFIRM_WINDOW="${CONFIRM_WINDOW:-120}"
POLL_SECONDS="${POLL_SECONDS:-20}"

publish() {  # publish <title> <priority> <tags> <body>
  if [ "$DRY_RUN" = "1" ]; then
    printf '  would send: [%s] %s — %s\n' "$3" "$1" "$4"
    return 0
  fi
  if ! curl -sS --max-time 20 -u "$NTFY_USER:$NTFY_PASS" \
        -H "Title: $1" -H "Priority: $2" -H "Tags: $3" \
        -d "$4" "$NTFY_URL/$NTFY_TOPIC" >/dev/null; then
    # Loud, and keep going: a failed ping must never take the watcher down and
    # leave the site looking quiet.
    echo "✗ ntfy publish failed (title: $1)" >&2
  fi
}

# access-log lines on stdin -> "kind|prefix|referrer|user-agent" on stdout
events() {
  awk -F'"' '
    function class(ua) {
      # tolower(): "Claude-SearchBot" and "ClaudeBot" only matched /bot/ because
      # they happen to carry a lowercase contact address (+searchbot@...). A
      # crawler that names itself Bot with no email would have been announced as
      # a real visitor, and one of them downloads the zip.
      if (tolower(ua) ~ /bot|crawler|spider|slurp|facebookexternalhit|recordedfuture|trendiction/) return "bot"
      if (ua ~ /^ClickGraft\//)                                    return "app"
      if (ua ~ /^(curl|Wget|Python-urllib|Go-http|libwww|ClickGraft-healthcheck)/) return "tool"
      if (ua ~ /Mozilla|AppleWebKit|Gecko|Safari|Chrome|Firefox/)  return "browser"
      return "other"
    }
    {
      split($1, f, " "); pfx = f[1]
      split($2, r, " "); path = r[2]
      # s[1], not s[2] — awk splitting on " " collapses whitespace runs and
      # drops the leading blank, so field 3 " 200 6529 " gives s[1]=status.
      split($3, s, " "); status = s[1]
      ref = $4; ua = $6
      c = class(ua)
      if (c != "browser" && c != "app") next

      kind = ""
      if (c == "browser" && path == "/ClickGraft.zip" && status == "200")          kind = "download"
      else if (c == "browser" && (path == "/" || path == "/index.html") &&
               (status == "200" || status == "304"))                               kind = "view"
      # A page asset is what separates a browser that RENDERED the page from a
      # scanner that only grabbed the HTML. See the note on "view" in notify().
      else if (c == "browser" && (status == "200" || status == "304") &&
               path ~ /^\/(icon\.svg|favicon\.ico|icon-1024\.png|apple-touch-icon\.png)$/) kind = "asset"
      else if (c == "app" && path ~ /appcast/)                                     kind = "app"
      if (kind == "") next

      gsub(/\|/, " ", ref); gsub(/\|/, " ", ua)
      print kind "|" pfx "|" ref "|" ua
      fflush()
    }
  '
}

# events on stdin -> notifications
notify() {
  while IFS='|' read -r kind prefix ref ua; do
    pfxkey=$(printf '%s' "$prefix" | tr -c 'A-Za-z0-9.' '_')

    # A page view is held back until the same address also fetches one of the
    # page's assets. Every real visitor in the log does that within a second;
    # the scanner that hits this site twice a day has requested /index.html and
    # nothing else, 13 times out of 13. Filtering on its user-agent instead
    # would be a rule about one scanner — and it lies anyway, claiming
    # "rv:109.0 ... Firefox/120.0", a pairing real Firefox never sends. This
    # test is about behaviour, so it also catches the next one.
    #
    # Cost of being wrong: a visitor who blocks images is never announced. That
    # beats crying wolf twice a day, which is how people learn to ignore a
    # notification channel.
    case "$kind" in
      view)
        printf '%s|%s|%s\n' "$(date +%s)" "$ref" "$ua" > "$STATE/pending-$pfxkey"
        # Unconfirmed views are never read again; do not let them pile up.
        find "$STATE" -name 'pending-*' -mmin +1440 -delete 2>/dev/null || true
        continue
        ;;
      asset)
        pend="$STATE/pending-$pfxkey"
        [ -f "$pend" ] || continue
        IFS='|' read -r pend_at ref ua < "$pend" || continue
        rm -f "$pend"
        # if/fi rather than `[ ... ] && continue`: under set -e a false test
        # makes that statement return non-zero and kills the whole script.
        if [ "$(( $(date +%s) - pend_at ))" -gt "$CONFIRM_WINDOW" ]; then continue; fi
        kind=view
        ;;
    esac

    case "$kind" in
      download) quiet=$QUIET_DOWNLOAD; title="ClickGraft downloaded";     prio=4; tags="inbox_tray" ;;
      view)     quiet=$QUIET_VIEW;     title="ClickGraft visitor";        prio=2; tags="eyes" ;;
      app)      quiet=$QUIET_APP;      title="ClickGraft app checked in"; prio=2; tags="satellite" ;;
      *)        continue ;;
    esac

    key="$kind-$pfxkey"
    now=$(date +%s)
    last=0
    if [ -f "$STATE/$key" ]; then last=$(cat "$STATE/$key" 2>/dev/null || echo 0); fi
    case "$last" in ''|*[!0-9]*) last=0 ;; esac
    if [ "$((now - last))" -lt "$quiet" ]; then continue; fi
    printf '%s\n' "$now" > "$STATE/$key"

    # A coarse platform is genuinely useful ("someone on a Mac") and costs no
    # more privacy than the user-agent line already in the log. The address was
    # truncated to /24 or /48 by nginx before it was written, so there is no
    # full IP here to leak.
    case "$ua" in
      *Macintosh*)     plat="Mac" ;;
      *iPhone*|*iPad*) plat="iOS" ;;
      *Android*)       plat="Android" ;;
      *Windows*)       plat="Windows" ;;
      *Linux*)         plat="Linux" ;;
      *)               plat="unknown" ;;
    esac
    if [ "$kind" = "app" ]; then
      ver=$(printf '%s' "$ua" | sed -n 's|^ClickGraft/\([0-9.]*\).*|\1|p')
      plat="v${ver:-?}"
    fi

    body="$prefix · $plat"
    case "$ref" in
      -|""|*clickgraft.elusive.net*) : ;;
      # The scheme pattern allows +.- because referrers are not all http: the
      # Reddit app sends android-app://com.reddit.frontpage/, and a scheme rule
      # of [a-z]* left the hyphen behind and reported the source as
      # "android-app:" instead of naming the app.
      *) src=$(printf '%s' "$ref" | sed -e 's|^[a-zA-Z][a-zA-Z0-9+.-]*://||' -e 's|/.*||')
         [ -n "$src" ] && body="$body · from $src" ;;
    esac

    publish "$title" "$prio" "$tags" "$body"
  done
}

# Read whatever has been appended since last time and run it through the
# pipeline. Offset is a byte count in $STATE/offset; a file smaller than the
# offset means it was rotated or truncated, so start again from zero.
#
# The offset advances to the file size, so a line still being written could in
# principle be split across two passes. nginx writes an access-log line in a
# single write, so this does not happen in practice, and the worst case is one
# malformed event rather than silence — which is the failure mode that matters.
process_new() {
  [ -f "$LOG" ] || { echo "✗ access log not found: $LOG" >&2; return 1; }
  size=$(wc -c < "$LOG" | tr -d ' ')
  off=0
  [ -f "$STATE/offset" ] && off=$(cat "$STATE/offset" 2>/dev/null || echo 0)
  case "$off" in ''|*[!0-9]*) off=0 ;; esac
  if [ "$size" -lt "$off" ]; then
    echo "log shrank ($size < $off) — rotated or truncated, restarting from 0" >&2
    off=0
  fi
  if [ "$size" -gt "$off" ]; then
    tail -c "+$((off + 1))" "$LOG" | events | notify
    printf '%s\n' "$size" > "$STATE/offset"
  fi
}

mkdir -p "$STATE"

case "${1:-}" in
  --selftest)
    publish "[TEST] ClickGraft watcher" 3 test \
      "Self-test from $(hostname). If this arrived, the watcher can publish."
    echo "sent a [TEST] notification to $NTFY_URL/$NTFY_TOPIC"
    exit 0
    ;;
  --dry-run)
    src="${2:-}"
    [ -n "$src" ] && [ -f "$src" ] || { echo "usage: $0 --dry-run <logfile>" >&2; exit 2; }
    DRY_RUN=1
    echo "dry run over $src (no notifications will be sent)"
    events < "$src" | notify
    exit 0
    ;;
esac

# Fail loudly at startup rather than sitting there looking healthy while the
# log it is watching does not exist.
[ -f "$LOG" ] || { echo "✗ access log not found: $LOG" >&2; exit 1; }

# Prove the credential works now, so a bad password shows up in the journal at
# startup instead of silently swallowing the first real visitor.
#
# /v1/account rather than a publish, because a probe that publishes sends a real
# notification on every service restart. Checking the status code is not enough:
# ntfy answers an unauthenticated request with 200 and username "*", so a
# missing credential would look perfectly healthy right up until it did not.
who=$(curl -sS --max-time 20 -u "$NTFY_USER:$NTFY_PASS" "$NTFY_URL/v1/account" 2>/dev/null |
        sed -n 's/.*"username" *: *"\([^"]*\)".*/\1/p')
if [ "$who" != "$NTFY_USER" ]; then
  echo "✗ cannot authenticate to $NTFY_URL as $NTFY_USER (server said: ${who:-no answer})" >&2
  exit 1
fi

# Start from the end of the log, so a first run does not announce every visitor
# in the file's history.
if [ ! -f "$STATE/offset" ]; then
  wc -c < "$LOG" | tr -d ' ' > "$STATE/offset"
fi

if [ "${1:-}" = "--once" ]; then
  process_new
  exit 0
fi

echo "watching $LOG -> $NTFY_URL/$NTFY_TOPIC (polling every ${POLL_SECONDS}s)"
while :; do
  process_new || true    # a transient read error must not end the service
  sleep "$POLL_SECONDS"
done
