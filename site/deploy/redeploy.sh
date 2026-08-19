#!/usr/bin/env bash
# Push the site's CONTENT to the shared edge host.
#
#   ./site/deploy/redeploy.sh
#
# Content only. ClickGraft no longer owns a container: nginx, the tunnel and the
# routing belong to edge (CT 136) and are deployed from ~/JoshCode/elusive-edge.
# This ships files into sites/clickgraft/ and restarts nothing but ClickGraft's
# own collector — a site deploy must never be able to take the other projects
# sharing that nginx offline.
#
# House pattern: go THROUGH the PVE host, never ssh into the CT directly.
set -euo pipefail

# Local settings — host, container id, paths. Not in git; see deploy.env.example.
_ENV="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/deploy.env"
# shellcheck disable=SC1090
[ -f "$_ENV" ] && . "$_ENV"

PVE_HOST="${PVE_HOST:?set PVE_HOST in site/deploy/deploy.env}"

# Fail before staging a gigabyte and half-writing a deploy. An unreachable host
# is a normal condition here — the PVE nodes are on a tailnet — and it should
# read as that rather than as a mysterious rsync error.
if ! ssh -o BatchMode=yes -o ConnectTimeout=8 "$PVE_HOST" true 2>/dev/null; then
  echo "✗ cannot reach $PVE_HOST over SSH — nothing was staged or deployed." >&2
  TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
  if [ -x "$TS" ] && ! "$TS" status >/dev/null 2>&1; then
    echo "  Tailscale is not running. Start it:  Tailscale up --accept-dns=false" >&2
  fi
  exit 1
fi
CT_ID="${CT_ID:-136}"
REMOTE_DIR="${REMOTE_DIR:-/opt/edge/sites/clickgraft}"
HEALTH_URL="${HEALTH_URL:-https://clickgraft.elusive.net/}"
STAGE="/tmp/clickgraft-stage"

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

echo "==> staging"
rm -rf /tmp/cg-build && mkdir -p /tmp/cg-build/html /tmp/cg-build/collector
cp "$ZIP"                    /tmp/cg-build/html/ClickGraft.zip

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
VERSION="$(/usr/bin/defaults read "$ROOT/dist/ClickGraft.app/Contents/Info.plist" CFBundleShortVersionString)"
UPDATED="$(date -r "$ZIP" '+%-d %B %Y')"

sed -e "s|{{ZIP_SHA256}}|$SHA|g" \
    -e "s|{{VERSION}}|$VERSION|g" \
    -e "s|{{UPDATED}}|$UPDATED|g" \
    "$SITE/index.html" > /tmp/cg-build/html/index.html
printf '%s  ClickGraft.zip\n' "$SHA" > /tmp/cg-build/html/ClickGraft.zip.sha256
cp "$SITE/clickgraft-icon.svg" "$SITE/clickgraft-og.jpg" "$SITE/clickgraft-apple-touch-icon.png" \
   "$SITE/clickgraft-favicon.ico" /tmp/cg-build/html/
# Keeps crawlers off the download. Cloudflare serves a managed robots.txt of its
# own and merges this into it; without an origin file there is nothing telling
# anyone to leave the half-megabyte binary alone.
cp "$SITE/robots.txt" /tmp/cg-build/html/

# Any surviving placeholder means the page would ship with {{...}} visible.
if grep -o '{{[A-Z_]*}}' /tmp/cg-build/html/index.html | sort -u | grep .; then
  echo "✗ the placeholders above were not substituted" >&2; exit 1
fi
echo "    version $VERSION, updated $UPDATED"
echo "    sha256 $SHA"

# Advertised version comes from the app itself, never from a hand-edited file.
sh "$HERE/make-appcast.sh" "$ROOT/dist/ClickGraft.app" \
   /tmp/cg-build/html/appcast.json "$SHA" "$ZIP"
echo "    appcast $(/usr/bin/defaults read "$ROOT/dist/ClickGraft.app/Contents/Info.plist" CFBundleShortVersionString)"
# The collector script and summary.sh live under the site directory so the code
# has ONE home: edge mounts the collector directory rather than keeping a second
# copy, and fetch-stats.sh runs summary.sh from REMOTE_DIR — it prints nothing at
# all if that file is missing, which reads exactly like "no traffic".
cp "$HERE/collector/collector.py" /tmp/cg-build/collector/
cp "$HERE/summary.sh"             /tmp/cg-build/
printf '%s\n' "$(cd "$ROOT" && git rev-parse --short HEAD)" > /tmp/cg-build/html/.build

echo "    site $(du -h /tmp/cg-build/html/index.html | cut -f1), download $(du -h /tmp/cg-build/html/ClickGraft.zip | cut -f1)"

echo "==> rsync to ${PVE_HOST}:${STAGE}"
ssh "$PVE_HOST" "mkdir -p '$STAGE'"
rsync -az --delete /tmp/cg-build/ "$PVE_HOST:$STAGE/"

echo "==> push into CT ${CT_ID} (content only)"
# Unquoted heredoc on purpose: the vars expand here and arrive as literals.
ssh "$PVE_HOST" bash -s <<EOF
set -euo pipefail
pct exec $CT_ID -- mkdir -p '$REMOTE_DIR/html' '$REMOTE_DIR/collector'
# Empty html/ contents first. tar -x MERGES, so a file deleted locally is never
# deleted on the server: two oversized icons kept being served for a day after
# they were replaced. Contents, not the directory — sites/ is bind-mounted into
# nginx and replacing the directory would leave it on the old inode.
pct exec $CT_ID -- sh -lc 'rm -f $REMOTE_DIR/html/* 2>/dev/null || true'
tar -C '$STAGE' -cf - . | pct exec $CT_ID -- tar -C '$REMOTE_DIR' -xf -
pct exec $CT_ID -- sh -lc 'chmod +x $REMOTE_DIR/summary.sh'
# The collector is a long-running python process: replacing the file on disk
# does not reload the code. Its DIRECTORY is mounted, so a restart is enough —
# no --force-recreate, and nothing else in the shared stack is touched.
pct exec $CT_ID -- sh -lc 'cd /opt/edge && docker compose restart clickgraft-report'
EOF

echo "==> verify inside the CT"
# Through edge's nginx with an explicit Host: one nginx serves several sites and
# picks the server block by name, so a request without it proves nothing.
ssh "$PVE_HOST" "pct exec $CT_ID -- docker exec edge-nginx-1 wget -qO- --header='Host: clickgraft.elusive.net' http://localhost/" \
  | grep -q 'ClickGraft' && echo "✓ edge is serving the page" || { echo "✗ edge is not serving the page" >&2; exit 1; }

echo "==> verify ${HEALTH_URL}"
sleep 4
# Same marker as healthcheck.sh, for the same reason: these two are ours, and
# the HEAD on the zip was being counted as a download on every deploy.
CHECK=(-H "X-ClickGraft-Check: 1")
if curl -fsS --max-time 20 "${CHECK[@]}" "$HEALTH_URL" | grep -q 'ClickGraft'; then
  echo "✓ page is live"
  code=$(curl -s -o /dev/null -w '%{http_code}' -I --max-time 30 "${CHECK[@]}" "${HEALTH_URL}ClickGraft.zip")
  [ "$code" = 200 ] && echo "✓ download reachable" || { echo "✗ ClickGraft.zip returned HTTP $code" >&2; exit 1; }
  # Check every endpoint a user's Mac touches, not just the two obvious ones.
  # A deploy once reported success while /report returned 404, and the first we
  # knew of it was a user whose bug report vanished.
  echo
  BASE="${HEALTH_URL%/}" sh "$HERE/healthcheck.sh"
else
  echo "! ${HEALTH_URL} did not answer."
  echo "  edge is serving correctly, so this is DNS or the tunnel. Check that"
  echo "  clickgraft.elusive.net CNAMEs to edge's tunnel"
  echo "  (67bb46b9-96e5-4250-96c5-ca439065108f.cfargotunnel.com, proxied) and"
  echo "  that its Public Hostname routes to http://nginx:80. A 530/1033 means"
  echo "  the record points at a tunnel that no longer exists."
  exit 2
fi


# The watcher is a systemd unit on the CT, not a container, so nothing else
# would ever update it. It went four days announcing downloads while silently
# dropping every visitor because a site change renamed the assets it looked for
# and no deploy touched it. Re-running the installer here is what couples them.
echo
echo "==> refreshing the visitor watcher"
sh "$HERE/watch/install-watch.sh" >/dev/null && echo "✓ watcher reinstalled and running"
