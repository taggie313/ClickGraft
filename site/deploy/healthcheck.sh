#!/usr/bin/env bash
# Prove the whole public surface works, the way a user's Mac exercises it.
#
#   ./site/deploy/healthcheck.sh
#
# Written after a user's bug report was lost to a four-minute window where
# /report returned 404: the page was up, the download worked, the deploy said
# success, and the one endpoint nobody checks was broken. Anything a user
# depends on gets checked here.
set -uo pipefail
BASE="${BASE:-https://clickgraft.elusive.net}"
UA="ClickGraft-healthcheck/1.0 CFNetwork/1490.0.4 Darwin/24.0.0"
fail=0

check() { # name expected actual
  if [ "$2" = "$3" ]; then printf '  ✓ %-34s %s\n' "$1" "$3"
  else printf '  ✗ %-34s got %s, wanted %s\n' "$1" "$3" "$2"; fail=1; fi
}

# Every request carries the marker header so nginx leaves it out of the log.
# Without it, this script fetched the whole zip on each deploy and each fetch
# counted as a download — the one number you would most want to be real.
MARK=(-H "X-ClickGraft-Check: 1")
code() { curl -s -o /dev/null -w '%{http_code}' --max-time 30 -A "$UA" "${MARK[@]}" "$@"; }

echo "checking $BASE"
check "GET /"                200 "$(code "$BASE/")"
check "GET /ClickGraft.zip"  200 "$(code -I "$BASE/ClickGraft.zip")"
check "GET /appcast.json"    200 "$(code "$BASE/appcast.json")"
check "GET /ClickGraft.zip.sha256" 200 "$(code "$BASE/ClickGraft.zip.sha256")"
check "POST /report"         200 "$(code -X POST --data-binary 'healthcheck' "$BASE/report")"
check "GET /stats (must 404)" 404 "$(code "$BASE/stats/report.html")"

# The advertised version must match the download's actual hash, or the update
# check tells people to fetch something that isn't there.
adv=$(curl -s --max-time 30 -A "$UA" "${MARK[@]}" "$BASE/appcast.json" | sed -n 's/.*"sha256": "\([a-f0-9]*\)".*/\1/p')
pub=$(curl -s --max-time 30 -A "$UA" "${MARK[@]}" "$BASE/ClickGraft.zip.sha256" | cut -d' ' -f1)
real=$(curl -s --max-time 120 -A "$UA" "${MARK[@]}" "$BASE/ClickGraft.zip" | shasum -a 256 | cut -d' ' -f1)
check "appcast sha == published sha" "$pub" "$adv"
check "published sha == real bytes"  "$real" "$pub"

# The GitHub release must serve the same bytes. Compare the digest the API
# already publishes rather than downloading the asset: `gh release download`
# increments download_count, so verifying the release was itself faking two of
# the only non-zero numbers GitHub had.
if command -v gh >/dev/null 2>&1; then
  gh_digest=$(gh api "repos/${REPO:-taggie313/ClickGraft}/releases/latest" \
                --jq '.assets[] | select(.name=="ClickGraft.zip") | .digest' 2>/dev/null \
              | sed 's/^sha256://')
  if [ -n "$gh_digest" ]; then
    check "github release == site bytes" "$real" "$gh_digest"
  else
    printf '  - %-34s %s\n' "github release digest" "unavailable, skipped"
  fi
fi

if [ "$fail" = 0 ]; then
  echo "all good"
  exit 0
fi

# curl reports 000 when it never got a response at all. If EVERY check is 000
# the site is not necessarily down — this machine may simply be offline, and
# saying "the site is broken" then would send someone to fix the wrong thing.
if ! curl -s -o /dev/null --max-time 10 https://cloudflare.com/cdn-cgi/trace; then
  echo >&2
  echo "  Note: this machine cannot reach the wider internet either, so the" >&2
  echo "  failures above may be local. Check your own connection before" >&2
  echo "  concluding the site is down." >&2
fi
echo "FAILURES ABOVE" >&2
exit 1
