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
FEED_ETAG="$STATE/feed-etag"
FEED_BODY="$STATE/feed-body.json"
DISCOVER_SEEN="$STATE/discover-base"

# TWO directories, not one. HP publishes DMGs here...
BASE="https://ftp.hp.com/pub/softlib/software13/printers/hpdesignjetclick"
# ...and ships auto-update payloads from a DIFFERENT directory entirely, as
# zipped .app bundles. That second one is a strict superset: 4.8.118 and
# 4.10.38 exist there and have NEVER existed as a DMG. Probing filenames in
# the first directory alone was structurally blind to every version HP
# delivers only by auto-update, which is most of the ones users actually
# report having.
ZIPBASE="https://ftp.hp.com/pub/softlib/software13/printers/hpclick/darwin"

# What the app itself asks on every launch. This NAMES a version instead of
# making us guess one, and it is the only source in the whole chain that does.
FEED="https://lfp-downloads.hpcloud.hp.com/hpclick/darwin/update-darwin.json"
# Windows feeds. x64 has run ahead of macOS before, so it is an early signal.
WIN64="https://lfp-downloads.hpcloud.hp.com/hpclick/x64/RELEASES"
WIN32="https://lfp-downloads.hpcloud.hp.com/hpclick/x86/RELEASES"
# HP can repoint the whole update channel at a different host at any launch,
# via this discovery service. If it ever stops naming lfp-downloads, every
# URL above silently goes stale and this watcher would report "no change"
# forever while being pointed at nothing.
DISCOVER="https://us1.api.ws-hp.com/url-retrieval/discover/hpclick/2"

KNOWN="${KNOWN_MAC_VERSION:-4.8.117}"
UA='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15'
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

mkdir -p "$STATE"; touch "$SEEN"

probe() {  # probe <version> -> prints http code for the DMG
  # Range request: this asks for one byte, so a hit costs nothing even though
  # the real file is over half a gigabyte.
  curl -s -o /dev/null -m 25 -L -r 0-0 -A "$UA" -w '%{http_code}' \
       "$BASE/HPClick-$1.dmg" 2>/dev/null || echo 000
}

probe_zip() {  # probe_zip <version> -> prints "<http code> <bytes>"
  # Size matters here, not just presence. HP's own HPClick-4.10.38.zip is
  # 7,221,248 bytes next to 572MB siblings -- a truncated upload that would
  # strand anyone who downloaded it. A status-only probe would have called
  # that a healthy release.
  curl -sI -m 25 -L -A "$UA" "$ZIPBASE/HPClick-$1.zip" 2>/dev/null \
    | awk 'BEGIN{c="000";n="?"}
           /^HTTP\//{c=$2}
           tolower($1)=="content-length:"{n=$2}
           END{gsub(/\r/,"",n); print c, n}'
}

get_feed() {  # -> prints body, or nothing on failure
  curl -sS -m 25 -A "$UA" "$FEED" 2>/dev/null
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

# ---- the feed the app itself asks -----------------------------------------
# Checked BEFORE the filename sweep, because it names a version outright while
# the sweep can only confirm a guess. It is not a replacement though: the feed
# advertised 4.8.118 continuously through the whole period in which 4.10.42
# was published, so on its own it would have missed 4.10.42 entirely. Both.
feed_body="$(get_feed)"
if [ -z "$feed_body" ]; then
  say "ClickGraft: update feed unreachable" 3 warning \
"Could not read HP's update feed at $FEED. The DMG sweep below still ran, but
the channel that delivers versions HP never publishes is not being watched
right now."
else
  prev_body=""
  [ -f "$FEED_BODY" ] && prev_body="$(cat "$FEED_BODY")"
  if [ "$feed_body" != "$prev_body" ]; then
    # Report the whole body. It is ~360 bytes and the interesting part is
    # sometimes WHICH version was withdrawn, not just which was added.
    if [ -n "$prev_body" ]; then
      say "HP changed the update feed" 4 satellite \
"HP's auto-update feed changed.

NOW:
$feed_body

WAS:
$prev_body

Payloads ship from $ZIPBASE -- a different directory from the DMGs, and one
that carries builds never published as a DMG."
    else
      echo "recording feed baseline (first run)"
    fi
    printf '%s' "$feed_body" > "$FEED_BODY"
  fi
  echo "feed: $(printf '%s' "$feed_body" | tr -d ' \n' | cut -c1-80)"
fi

# ---- canary: is the channel still where we think it is? --------------------
# HEAD returns 500 on this endpoint; it must be GET.
disc="$(curl -sS -m 25 -A "$UA" "$DISCOVER" 2>/dev/null | tr -d ' \n')"
case "$disc" in
  *lfp-downloads.hpcloud.hp.com/hpclick*)
    [ -f "$DISCOVER_SEEN" ] || printf 'lfp-downloads\n' > "$DISCOVER_SEEN"
    ;;
  "")
    echo "discover endpoint unreachable (not fatal; the feed URL is hardcoded too)" >&2
    ;;
  *)
    say "HP moved the update channel" 5 rotating_light \
"$DISCOVER no longer points the Releases service at lfp-downloads.hpcloud.hp.com.
Every URL this watcher checks is derived from that base, so it is now watching
a channel HP may have abandoned. Re-read app-updater.js and update FEED/ZIPBASE."
    ;;
esac

# ---- Windows feeds: an early signal ---------------------------------------
# x64 sat on 4.10.42 while the macOS feed still said 4.8.118, so Windows leads.
for pair in "x64:$WIN64" "x86:$WIN32"; do
  arch="${pair%%:*}"; url="${pair#*:}"
  rel="$(curl -sS -m 25 -A "$UA" "$url" 2>/dev/null | tr -d '\r')"
  if [ -n "$rel" ]; then
    prev="$STATE/win-$arch"
    if [ -f "$prev" ] && [ "$rel" != "$(cat "$prev")" ]; then
      say "HP shipped a new Windows build ($arch)" 3 window \
"$url changed:

NOW: $rel
WAS: $(cat "$prev")

Windows has run ahead of macOS before. A macOS build often follows."
    fi
    printf '%s' "$rel" > "$prev"
  fi
done

# ---- candidates ------------------------------------------------------------
# Windows is on 4.10.x while macOS sits on 4.8.117, so a Mac release could
# reasonably be a patch bump, a 4.9, or a jump to parity with Windows.
cands="4.9.118 4.10.38"
i=118; while [ $i -le 130 ]; do cands="$cands 4.8.$i"; i=$((i+1)); done
i=110; while [ $i -le 125 ]; do cands="$cands 4.9.$i";  i=$((i+1)); done
i=30;  while [ $i -le 45  ]; do cands="$cands 4.10.$i"; i=$((i+1)); done

found=""
found_zip=""
truncated=""
for v in $cands; do
  grep -qx "$v" "$SEEN" 2>/dev/null && continue   # already reported
  hit=0
  code=$(probe "$v")
  case "$code" in
    200|206) found="$found $v"; hit=1; echo "FOUND HPClick-$v.dmg" ;;
  esac
  # The zip directory is checked even when the DMG answered, because the two
  # are independent: a version can appear in either, or in one and not the
  # other, and knowing which is how we tell people where to get it.
  set -- $(probe_zip "$v")
  zcode="${1:-000}"; zsize="${2:-0}"
  case "$zcode" in
    200|206)
      hit=1
      # 50MB floor. Real builds are ~572MB; HP has published a 7.2MB
      # "release" before, which unzips to nothing and helps nobody.
      if [ "${zsize:-0}" -lt 52428800 ] 2>/dev/null; then
        truncated="$truncated $v"
        echo "FOUND HPClick-$v.zip -- but only $zsize bytes, TRUNCATED"
      else
        found_zip="$found_zip $v"
        echo "FOUND HPClick-$v.zip ($zsize bytes)"
      fi
      ;;
  esac
  [ "$hit" = "1" ] && printf '%s\n' "$v" >> "$SEEN"
  sleep 1   # HP is doing us a favour hosting this; do not hammer it
done

if [ -n "$found" ] || [ -n "$found_zip" ] || [ -n "$truncated" ]; then
  for v in $found; do
    say "HP released a new Mac build: $v" 5 tada \
"HPClick-$v.dmg is now on HP's server.
$BASE/HPClick-$v.dmg

ClickGraft supports $KNOWN. A new manifest is needed before it can graft this one."
  done
  for v in $found_zip; do
    # Only worth a separate alert if the DMG did NOT also appear -- otherwise
    # it is the same release seen twice.
    case " $found " in *" $v "*) continue ;; esac
    say "HP pushed $v by auto-update only" 5 satellite \
"HPClick-$v.zip is in the update directory but there is NO DMG for it:
$ZIPBASE/HPClick-$v.zip

This is how 4.8.118 reached people who then could not reinstall or patch it.
The zip is the .app itself -- unzip it, no installer. A manifest is needed
before ClickGraft can graft this one."
  done
  for v in $truncated; do
    say "HP published a broken $v" 4 warning \
"$ZIPBASE/HPClick-$v.zip exists but is far too small to be a real build.
HP has done this before with 4.10.38. Do not offer this file to anyone;
it will not unzip."
  done
else
  echo "no new macOS build (control $KNOWN ok, $(echo "$cands" | wc -w | tr -d ' ') candidates checked)"
fi
