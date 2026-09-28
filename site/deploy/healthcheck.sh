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

# The versioned name is the one the appcast advertises and the one people
# actually download; the bare name is only an alias kept alive for old links.
# Take it from the appcast rather than composing it here, so this checks the
# URL that is really being published rather than the one we assume is.
ZIPURL=$(curl -s --max-time 30 -A "$UA" "${MARK[@]}" "$BASE/appcast.json" \
         | sed -n 's/.*"download": "\([^"]*\)".*/\1/p')
ZIPFILE="${ZIPURL##*/}"
check "GET /$ZIPFILE"        200 "$(code -I "$BASE/$ZIPFILE")"
check "GET /$ZIPFILE.sha256" 200 "$(code "$BASE/$ZIPFILE.sha256")"
# The interpreter a Mac fetches when it has none of its own, and the pin that
# names it. Both are checked because a Mac in that position cannot open
# ClickGraft at all without them, and nothing else on this page would notice:
# the download, the appcast and the page itself would all still be perfect.
#
# The pin is asked for first and the name taken from it, so this checks the file
# that is really being fetched rather than a name composed here.
# ...but only once a release needs it. ClickGraft started fetching an interpreter
# in 1.8.0; before that it ran on Apple's developer tools, so demanding the file
# while the site still serves 1.7.0 would print a red ✗ for a thing no released
# app has ever asked for -- and a healthcheck that is always a bit red is one
# nobody reads, which is the failure this whole script exists to prevent.
# head -1: the FIRST "version", which is the one being advertised. The appcast
# carries the whole release history under "releases", so without it this is every
# version ClickGraft has ever had and the comparison below is made against a
# dozen lines of them. (`p;q` does not work here -- sed would quit on line 1,
# which is the opening brace.)
ADVVER=$(curl -s --max-time 30 -A "$UA" "${MARK[@]}" "$BASE/appcast.json" \
         | sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' | head -1)
NEEDS_PIN=$(awk -v v="$ADVVER" 'BEGIN {
  n = split(v, a, "."); print (n >= 2 && (a[1] > 1 || (a[1] == 1 && a[2] >= 8))) ? 1 : 0 }')

# The version is a floor, not the authority: whether an app needs an interpreter
# is settled by whether its release committed a pin, which only the artifact gate
# can see. So a version below the floor is reported as "not required" rather than
# "not needed", and the elif below still hashes a pin the site serves early.
if [ "$NEEDS_PIN" != 1 ]; then
  printf '  - %-34s %s\n' "interpreter" \
    "not required by ${ADVVER:-an unreadable version}; checked below if served"
fi

PIN=$(curl -s --max-time 30 -A "$UA" "${MARK[@]}" "$BASE/python-pin.json")
PYVER=$(printf '%s' "$PIN" | sed -n 's/.*"version": *"\([^"]*\)".*/\1/p')
PYSHA=$(printf '%s' "$PIN" | sed -n 's/.*"payload_zip_sha256": *"\([^"]*\)".*/\1/p')
if [ "$NEEDS_PIN" = 1 ]; then
  check "GET /python-pin.json" 200 "$(code "$BASE/python-pin.json")"
fi
if [ "$NEEDS_PIN" = 1 ] && [ -n "$PYVER" ]; then
  PYFILE="ClickGraft-python-$PYVER.zip"
  check "GET /$PYFILE"       200 "$(code -I "$BASE/$PYFILE")"
  check "GET /$PYFILE.sha256" 200 "$(code "$BASE/$PYFILE.sha256")"
  # Fetched whole and hashed, not just HEADed. A truncated or replaced archive
  # answers 200 and is then refused by every app that downloads it, which is a
  # failure only the user would ever see.
  got=$(curl -s --max-time 300 -A "$UA" "${MARK[@]}" "$BASE/$PYFILE" | shasum -a 256 | cut -d' ' -f1)
  check "interpreter matches its pin" "$PYSHA" "$got"
fi

# EVERY retained runtime, not only the one this release advertises. That is the
# whole point of the inventory: an app shipped against an older payload still
# asks for it by name, and the site has to answer. Checking only the current
# pin is exactly how the previous behaviour looked correct while older apps
# got a 404.
INV=$(curl -s --max-time 30 -A "$UA" "${MARK[@]}" "$BASE/runtime-inventory.json")
if printf '%s' "$INV" | grep -q '"artifacts"'; then
  check "GET /runtime-inventory.json" 200 "$(code "$BASE/runtime-inventory.json")"
  printf '%s' "$INV" \
    | tr '{' '\n' | grep '"path"' \
    | sed -n 's/.*"path": *"\([^"]*\)".*"sha256": *"\([^"]*\)".*/\1 \2/p' \
    | while read -r rname rsha; do
        [ -n "$rname" ] || continue
        rgot=$(curl -s --max-time 300 -A "$UA" "${MARK[@]}" "$BASE/$rname" \
               | shasum -a 256 | cut -d' ' -f1)
        if [ "$rgot" = "$rsha" ]; then printf '  ✓ %-34s %s\n' "retained $rname" "${rsha:0:16}…"
        else printf '  ✗ %-34s got %s, wanted %s\n' "retained $rname" "$rgot" "$rsha"
             echo "RETAINED_FAILURE" >> /tmp/cg-health-retained.$$
        fi
      done
  # The while loop runs in a subshell because of the pipe, so `fail=1` inside it
  # would be lost when the pipeline ends. The marker file crosses that boundary.
  if [ -f /tmp/cg-health-retained.$$ ]; then rm -f /tmp/cg-health-retained.$$; fail=1; fi
fi

# The two remaining cases for the ADVERTISED pin, now that the retained set is
# checked above on its own terms.
if [ "$NEEDS_PIN" = 1 ] && [ -z "$PYVER" ]; then
  printf '  ✗ %-34s %s\n' "python-pin.json" "unreadable: no version in it"; fail=1
elif [ "$NEEDS_PIN" != 1 ] && [ -n "$PYVER" ]; then
  # Published early, before the release that needs it. Not a failure -- it is
  # how the payload gets onto the site ahead of the release -- but still
  # checked, because a wrong one published now is a wrong one served later.
  got=$(curl -s --max-time 300 -A "$UA" "${MARK[@]}" "$BASE/ClickGraft-python-$PYVER.zip" \
        | shasum -a 256 | cut -d' ' -f1)
  check "interpreter published early" "$PYSHA" "$got"
fi

check "POST /report"         200 "$(code -X POST --data-binary 'healthcheck' "$BASE/report")"
check "GET /stats (must 404)" 404 "$(code "$BASE/stats/report.html")"

# The Spanish page. It had never been checked here, and three deploys in one day
# passed while it went unrequested -- the same shape of gap as the one in the
# header comment, where everything reported success and the one unchecked thing
# was broken. It is a separate file with its own stylesheet and its own templated
# placeholders, so it can break on its own, and it is half of what is published.
check "GET /es/"             200 "$(code "$BASE/es/")"
# A page that 200s while still holding {{VERSION}} is worse than one that 404s:
# it looks fine and tells the reader to download a file called {{ZIP_NAME}}.
for page in / /es/; do
  left=$(curl -s --max-time 30 -A "$UA" "${MARK[@]}" "$BASE$page" | grep -c '{{[A-Z_]*}}')
  check "no placeholders left in $page" 0 "$left"
done

# The advertised version must match the download's actual hash, or the update
# check tells people to fetch something that isn't there.
adv=$(curl -s --max-time 30 -A "$UA" "${MARK[@]}" "$BASE/appcast.json" | sed -n 's/.*"sha256": "\([a-f0-9]*\)".*/\1/p')
pub=$(curl -s --max-time 30 -A "$UA" "${MARK[@]}" "$BASE/ClickGraft.zip.sha256" | cut -d' ' -f1)
real=$(curl -s --max-time 120 -A "$UA" "${MARK[@]}" "$BASE/$ZIPFILE" | shasum -a 256 | cut -d' ' -f1)
# And the alias must be the same bytes. It is a symlink, so this can only fail
# if the deploy left a stale file behind or a cache is serving an old release
# under the old name -- which is exactly what happened on 8 Sep 2026.
alias_real=$(curl -s --max-time 120 -A "$UA" "${MARK[@]}" "$BASE/ClickGraft.zip" | shasum -a 256 | cut -d' ' -f1)
check "appcast sha == published sha" "$pub" "$adv"
check "published sha == real bytes"  "$real" "$pub"
check "ClickGraft.zip alias == same" "$real" "$alias_real"

# The GitHub release must serve the same bytes. Compare the digest the API
# already publishes rather than downloading the asset: `gh release download`
# increments download_count, so verifying the release was itself faking two of
# the only non-zero numbers GitHub had.
#
# The release for the version the appcast ADVERTISES, not "latest". Comparing
# against latest failed on every release: the checklist deploys (step 6) before
# creating the release (step 7), so redeploy's own healthcheck always compared
# the new zip with the previous release and printed FAILURES ABOVE. A failure
# that is expected every time teaches people to skim past this output, which is
# how the real one CLAUDE.md warns about gets missed.
#
# So a release that does not exist yet is named for what it is. redeploy.sh sets
# RELEASE_PENDING=1, because in that one run the missing release is the next
# step rather than a fault; run on its own, this still fails, so a skipped
# step 7 cannot go unnoticed. A release that exists with different bytes fails
# either way.
if command -v gh >/dev/null 2>&1; then
  ver=$(curl -s --max-time 30 -A "$UA" "${MARK[@]}" "$BASE/appcast.json" \
        | sed -n 's/.*"version": "\([^"]*\)".*/\1/p' | head -1)
  # stdout and stderr together, and only a real SHA-256 counts as a digest: on a
  # 404, gh prints GitHub's error JSON to STDOUT, which an emptiness test takes
  # for a digest and then reports as a mismatch.
  gh_out=$(gh api "repos/${REPO:-taggie313/ClickGraft}/releases/tags/v$ver" \
             --jq ".assets[] | select(.name | endswith(\".zip\")) | .digest" 2>&1)
  gh_digest=$(printf '%s\n' "$gh_out" | sed -n 's/^sha256:\([0-9a-f]\{64\}\)$/\1/p' | head -1)
  if [ -n "$gh_digest" ]; then
    check "github release v$ver == site" "$real" "$gh_digest"
  elif printf '%s' "$gh_out" | grep -q '"status":"404"\|HTTP 404'; then
    if [ "${RELEASE_PENDING:-0}" = 1 ]; then
      printf '  - %-34s %s\n' "github release v$ver" \
        "not created yet; next: gh release create v$ver dist/ClickGraft.zip"
    else
      check "github release v$ver exists" "yes" "no"
    fi
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
