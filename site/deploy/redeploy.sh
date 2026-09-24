#!/usr/bin/env bash
# Push the site's CONTENT to the shared edge host.
#
#   ./site/deploy/redeploy.sh
#   CLICKGRAFT_ALLOW_LEGACY_ARTIFACT=1.5.9 ./site/deploy/redeploy.sh
#
# The second form is for a ZIP of 1.5.9 or earlier, built before ZIPs carried a
# record of their sources. packaging/check_release.py refuses those unless told
# the one version to let through, so until 1.6.0 ships it is the only way to
# redeploy at all, page-only changes included. That ZIP's files are still
# checked against its tag.
#
# Content only. ClickGraft no longer owns a container: nginx, the tunnel and the
# routing belong to edge (CT 136) and are deployed from ~/JoshCode/elusive-edge.
# This ships files into sites/clickgraft/ and restarts nothing but ClickGraft's
# own collector — a site deploy must never be able to take the other projects
# sharing that nginx offline.
#
# House pattern: go THROUGH the PVE host, never ssh into the CT directly.
set -euo pipefail

# Local settings — host, container id, paths — and the shared preflight, which
# loads deploy.env, checks the host is reachable, and follows the container if
# it has been migrated to another node.
#
# This used to be a second copy of that preflight, inlined here. The copy drifted:
# when require_host was taught to verify the container rather than just the node,
# redeploy.sh — the one script where a late failure costs the most, since it runs
# after release.sh has already published — kept the old check and kept failing
# halfway through. One preflight, one place.
# shellcheck source=/dev/null
. "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/_common.sh"

# Fail before staging a gigabyte and half-writing a deploy.
require_host

REMOTE_DIR="${REMOTE_DIR:-/opt/edge/sites/clickgraft}"
HEALTH_URL="${HEALTH_URL:-https://clickgraft.elusive.net/}"
BUILD="$(mktemp -d /tmp/cg-build.XXXXXXXX)"
DEPLOY_ID="$(basename "$BUILD")"
STAGE="/tmp/clickgraft-stage-$DEPLOY_ID"
STAGED=
cleanup() {
  rm -rf "$BUILD"
  # The copy on the PVE node, if this run made one and stopped before the push
  # that removes it. A failed rsync is exactly when it was being left behind.
  [ -z "$STAGED" ] || ssh -o BatchMode=yes -o ConnectTimeout=8 "$PVE_HOST" "rm -rf '$STAGE'" \
    || echo "✗ could not remove $STAGE on $PVE_HOST; remove it by hand" >&2
}
trap cleanup EXIT

HERE="$(cd "$(dirname "$0")" && pwd)"          # site/deploy
SITE="$(cd "$HERE/.." && pwd)"                 # site
ROOT="$(cd "$SITE/.." && pwd)"                 # repo root
ZIP="${ZIP:-$ROOT/dist/ClickGraft.zip}"

if [ ! -f "$ZIP" ]; then
  echo "✗ $ZIP is missing. Run packaging/sign_and_notarize.sh first." >&2
  echo "  The download must be the STAPLED zip that script re-creates after" >&2
  echo "  notarizing; a hand-made zip carries no ticket and the page's promise" >&2
  echo "  about no security warnings becomes false." >&2
  exit 1
fi

# Checks the ZIP against the tag its own version names, v<version>, not HEAD:
# a page-only commit after a release still deploys, and a ZIP that was not
# built from the tagged sources does not. A 1.5.9-era ZIP needs the variable
# shown at the top of this file; the gate's ✗ line names it.
python3 "$ROOT/packaging/check_release.py" --artifact "$ZIP"

echo "==> staging"
mkdir -p "$BUILD/html" "$BUILD/collector"

# Version first, because the download is named after it.
#
# The canonical file carries the version: ClickGraft-1.5.0.zip. A fixed name
# whose CONTENTS change on every release is a URL that can be stale, and on
# 8 Sep 2026 it was -- Cloudflare served 1.4.0 for hours while appcast.json
# advertised 1.5.0 and published the new file's sha256, so anyone following
# this site's own instructions to check the fingerprint got a mismatch. A URL
# nobody has ever requested cannot be served from anyone's cache.
#
# ClickGraft.zip stays, as a SYMLINK to the versioned file rather than a 302.
# Both keep old links working -- forum posts, the Reddit thread, anyone's
# bookmarks -- but a redirect would put TWO lines in the access log for one
# download, a 302 on the old path and a 200 on the new one, and the download
# counters in summary.sh and clickgraft-watch.sh count 200s on a path. The
# symlink is one request, one log line, whichever name was asked for.
VERSION="$(python3 -c 'import plistlib,sys,zipfile; print(plistlib.loads(zipfile.ZipFile(sys.argv[1]).read("ClickGraft.app/Contents/Info.plist"))["CFBundleShortVersionString"])' "$ZIP")"
APP_VERSION="$(/usr/bin/defaults read "$ROOT/dist/ClickGraft.app/Contents/Info.plist" CFBundleShortVersionString)"
[ "$VERSION" = "$APP_VERSION" ] || { echo "✗ app and distributed ZIP versions disagree" >&2; exit 1; }
ZIPNAME="ClickGraft-$VERSION.zip"
cp "$ZIP"                    "$BUILD/html/$ZIPNAME"
ln -s "$ZIPNAME"             "$BUILD/html/ClickGraft.zip"

# The page quotes the download's SHA-256. Substituting it at deploy time from
# the very file being shipped is the only way that number cannot drift: a hash
# typed into the HTML would silently go stale the first time the app is rebuilt,
# and a wrong fingerprint is worse than none — it teaches people the check is
# meaningless.
SHA="$(shasum -a 256 "$ZIP" | cut -d' ' -f1)"

# Version comes from the app being shipped, and the date from that zip's own
# mtime — not from `date` at deploy time. Redeploying the page without changing
# the download must not advance "last updated": a date that moves when nothing
# was released is worse than no date, because it is the thing people check to
# decide whether to bother re-downloading.
UPDATED="$(date -r "$ZIP" '+%-d %B %Y')"
# ISO form of the same date for the sitemap, so the two can never disagree.
UPDATED_ISO="$(date -r "$ZIP" '+%Y-%m-%d')"
# Spanish form of the same date, for /es/. Built from the month number rather
# than a locale, because the CT and this Mac need not have an es_ES locale
# installed and a missing one silently yields English month names.
# cut, not a shell array: arrays index from 0 in bash and from 1 in zsh, and a
# script read in the wrong shell would silently name the month before.
ES_MONTH="$(echo 'enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre' \
            | cut -d' ' -f"$(( 10#$(date -r "$ZIP" '+%m') ))")"
UPDATED_ES="$(date -r "$ZIP" '+%-d') de $ES_MONTH de $(date -r "$ZIP" '+%Y')"

sed -e "s|{{ZIP_SHA256}}|$SHA|g" \
    -e "s|{{VERSION}}|$VERSION|g" \
    -e "s|{{ZIP_NAME}}|$ZIPNAME|g" \
    -e "s|{{UPDATED}}|$UPDATED|g" \
    "$SITE/index.html" > "$BUILD/html/index.html"
# Both names get a checksum file, naming the file the reader actually has.
printf '%s  %s\n' "$SHA" "$ZIPNAME" > "$BUILD/html/$ZIPNAME.sha256"
printf '%s  %s\n' "$SHA" "$ZIPNAME" > "$BUILD/html/ClickGraft.zip.sha256"
cp "$SITE/clickgraft-icon.svg" "$SITE/clickgraft-og.jpg" "$SITE/clickgraft-apple-touch-icon.png" \
   "$SITE/clickgraft-favicon.ico" "$BUILD/html/"
# Keeps crawlers off the download. Cloudflare serves a managed robots.txt of its
# own and merges this into it; without an origin file there is nothing telling
# anyone to leave the half-megabyte binary alone.
cp "$SITE/robots.txt" "$BUILD/html/"
# The Spanish page now carries the same version, download name and date as the
# English one (24 Sep 2026: a Spanish reader could not tell which build the
# button handed them), so it goes through the same substitution. The guard
# below covers it, and caught exactly this when the placeholders were added.
mkdir -p "$BUILD/html/es"
sed -e "s|{{VERSION}}|$VERSION|g" \
    -e "s|{{ZIP_NAME}}|$ZIPNAME|g" \
    -e "s|{{UPDATED_ES}}|$UPDATED_ES|g" \
    -e "s|{{UPDATED_ISO}}|$UPDATED_ISO|g" \
    "$SITE/es/index.html" > "$BUILD/html/es/index.html"
# Crawlers ask for /sitemap.xml by name and were getting a 404.
sed -e "s|{{UPDATED_ISO}}|$UPDATED_ISO|g" "$SITE/sitemap.xml" > "$BUILD/html/sitemap.xml"

# Any surviving placeholder means the page would ship with {{...}} visible.
if grep -ho '{{[A-Z_]*}}' "$BUILD/html/index.html" "$BUILD/html/sitemap.xml" \
     "$BUILD/html/es/index.html" | sort -u | grep .; then
  echo "✗ the placeholders above were not substituted" >&2; exit 1
fi
echo "    version $VERSION, updated $UPDATED"
echo "    sha256 $SHA"

# Advertised version comes from the app itself, never from a hand-edited file.
sh "$HERE/make-appcast.sh" "$ROOT/dist/ClickGraft.app" \
   "$BUILD/html/appcast.json" "$SHA" "$ZIP"
echo "    appcast $(/usr/bin/defaults read "$ROOT/dist/ClickGraft.app/Contents/Info.plist" CFBundleShortVersionString)"
# The collector script and summary.sh live under the site directory so the code
# has ONE home: edge mounts the collector directory rather than keeping a second
# copy, and fetch-stats.sh runs summary.sh from REMOTE_DIR — it prints nothing at
# all if that file is missing, which reads exactly like "no traffic".
cp "$HERE/collector/collector.py" "$BUILD/collector/"
cp "$HERE/summary.sh" "$HERE/visitor-classify.awk" "$BUILD/"
printf '%s\n' "$(cd "$ROOT" && git rev-parse --short HEAD)" > "$BUILD/html/.build"

echo "    site $(du -h "$BUILD/html/index.html" | cut -f1), download $(du -h "$BUILD/html/$ZIPNAME" | cut -f1)"

cp "$HERE/publish_site.py" "$BUILD/publish_site.py"
# The same check the container will run, here, before anything is uploaded —
# and through publish_site.py's own main, so a refusal is one ✗ line like every
# other refusal in this script rather than a Python traceback mid-deploy.
python3 "$HERE/publish_site.py" --check "$BUILD/html"

echo "==> rsync to ${PVE_HOST}:${STAGE}"
STAGED=1
ssh "$PVE_HOST" "mkdir -p '$STAGE'"
rsync -az --delete "$BUILD/" "$PVE_HOST:$STAGE/"

echo "==> push into CT ${CT_ID} (content only)"
# Unquoted heredoc on purpose: the vars expand here and arrive as literals.
ssh "$PVE_HOST" bash -s <<EOF
set -euo pipefail
# Until publish_site.py starts, .incoming-$DEPLOY_ID is only an upload, and goes
# if the upload fails. After that it is publish_site.py's: it removes it when it
# refuses or puts the old site back, and keeps it when it may hold the only
# copy of the previous site, which nothing here may delete.
handed_over=
cleanup() {
  rm -rf '$STAGE'
  [ -n "\$handed_over" ] || pct exec $CT_ID -- rm -rf '$REMOTE_DIR/.incoming-$DEPLOY_ID'
}
trap cleanup EXIT
pct exec $CT_ID -- mkdir -p '$REMOTE_DIR/.incoming-$DEPLOY_ID'
tar -C '$STAGE' -cf - . | pct exec $CT_ID -- tar -C '$REMOTE_DIR/.incoming-$DEPLOY_ID' -xf -
handed_over=1
pct exec $CT_ID -- python3 '$REMOTE_DIR/.incoming-$DEPLOY_ID/publish_site.py' \
  '$REMOTE_DIR/.incoming-$DEPLOY_ID' '$REMOTE_DIR'
# The collector is a long-running python process: replacing the file on disk
# does not reload the code. Its DIRECTORY is mounted, so a restart is enough —
# no --force-recreate, and nothing else in the shared stack is touched.
pct exec $CT_ID -- sh -lc 'cd /opt/edge && docker compose restart clickgraft-report'
EOF
STAGED=

echo "==> verify inside the CT"
# Through edge's nginx with an explicit Host: one nginx serves several sites and
# picks the server block by name, so a request without it proves nothing. From
# inside the CT there is no CF-Connecting-IP, so nginx leaves these requests out
# of the access log and the ZIP fetched below is not counted as a download.
#
# What was just published, not merely "a page". Until 22 Sep 2026 this looked
# for the word ClickGraft, which the page it replaced contains too, so an nginx
# still serving the old html/ would have passed. publish_site.py switches html/
# by name, which edge sees only because it mounts the whole sites/ directory.
#
# Hashes rather than captures, and never `| grep -q`: under pipefail grep exits
# at the first match, wget dies on EPIPE and the pipeline "fails", which is how
# this check once started failing the day the page grew past a pipe buffer.
served() {
  ssh "$PVE_HOST" "pct exec $CT_ID -- docker exec edge-nginx-1 wget -qO- --header='Host: clickgraft.elusive.net' 'http://localhost/$1'"
}
sha_of() { shasum -a 256 | cut -d' ' -f1; }
# "version sha256" from an appcast on stdin, or nothing if it cannot be read.
offered() {
  python3 -c 'import json, sys
try:
    appcast = json.load(sys.stdin)
    print(appcast["version"], appcast["sha256"])
except Exception:
    pass'
}
if [ "$(served '' | sha_of || true)" != "$(sha_of < "$BUILD/html/index.html")" ]; then
  echo "✗ edge is not serving the page just published." >&2
  echo "  publish_site.py switched $REMOTE_DIR/html. nginx sees that only while edge" >&2
  echo "  mounts the whole sites/ directory; mounting html/ itself leaves it on the" >&2
  echo "  old one. The site it replaced is the newest $REMOTE_DIR/.previous-*." >&2
  exit 1
fi
echo "✓ edge is serving the page just published"
got="$(served appcast.json | offered || true)"
[ "$got" = "$VERSION $SHA" ] \
  || { echo "✗ edge's appcast offers ${got:-nothing readable}, not $VERSION $SHA" >&2; exit 1; }
echo "✓ edge's appcast offers $VERSION, with this download's sha256"
for name in "$ZIPNAME" ClickGraft.zip; do
  got="$(served "$name" | sha_of || true)"
  [ "$got" = "$SHA" ] || { echo "✗ edge serves $name with sha256 $got, not $SHA" >&2; exit 1; }
done
echo "✓ edge serves $ZIPNAME and ClickGraft.zip, both with that sha256"

echo "==> verify ${HEALTH_URL}"
sleep 4
# Same marker as healthcheck.sh, for the same reason: these two are ours, and
# the HEAD on the zip was being counted as a download on every deploy.
CHECK=(-H "X-ClickGraft-Check: 1")
LIVE="$(curl -fsS --max-time 20 "${CHECK[@]}" "$HEALTH_URL" || true)"
if [ -z "$LIVE" ]; then
  echo "! ${HEALTH_URL} did not answer."
  echo "  edge is serving correctly, so this is DNS or the tunnel. Check that"
  echo "  clickgraft.elusive.net CNAMEs to edge's tunnel"
  echo "  (67bb46b9-96e5-4250-96c5-ca439065108f.cfargotunnel.com, proxied) and"
  echo "  that its Public Hostname routes to http://nginx:80. A 530/1033 means"
  echo "  the record points at a tunnel that no longer exists."
  exit 2
fi
# The page and the appcast pass through Cloudflare uncached (cf-cache-status
# DYNAMIC, 22 Sep 2026), so both must already be the ones edge just served.
case "$LIVE" in
  *"$SHA"*) echo "✓ page is live, quoting this download's sha256" ;;
  *) echo "✗ ${HEALTH_URL} answers, but its page does not quote $SHA." >&2
     echo "  edge serves the new page, so something between Cloudflare and edge is" >&2
     echo "  serving another copy." >&2
     exit 1 ;;
esac
got="$(curl -fsS --max-time 20 "${CHECK[@]}" "${HEALTH_URL}appcast.json" | offered || true)"
[ "$got" = "$VERSION $SHA" ] \
  || { echo "✗ the public appcast offers ${got:-nothing readable}, not $VERSION $SHA" >&2; exit 1; }
echo "✓ the public appcast offers $VERSION"
code=$(curl -s -o /dev/null -w '%{http_code}' -I --max-time 30 "${CHECK[@]}" "${HEALTH_URL}ClickGraft.zip")
[ "$code" = 200 ] && echo "✓ download reachable" || { echo "✗ ClickGraft.zip returned HTTP $code" >&2; exit 1; }
# Check every endpoint a user's Mac touches, not just the two obvious ones.
# A deploy once reported success while /report returned 404, and the first we
# knew of it was a user whose bug report vanished.
echo
# RELEASE_PENDING: on a release, the GitHub release is created after this
# deploy (CLAUDE.md step 7), so its absence here is the next step, not a fault.
BASE="${HEALTH_URL%/}" RELEASE_PENDING=1 sh "$HERE/healthcheck.sh"


# The watcher is a systemd unit on the CT, not a container, so nothing else
# would ever update it. It went four days announcing downloads while silently
# dropping every visitor because a site change renamed the assets it looked for
# and no deploy touched it. Re-running the installer here is what couples them.
echo
echo "==> refreshing the visitor watcher"
sh "$HERE/watch/install-watch.sh" >/dev/null && echo "✓ watcher reinstalled and running"
