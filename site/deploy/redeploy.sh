#!/usr/bin/env bash
# Push the site and compose stack to the clickgraft CT and restart it.
#
#   ./site/deploy/redeploy.sh
#
# House pattern: go THROUGH the PVE host, never ssh into the CT directly.
set -euo pipefail

# Local settings — host, container id, paths. Not in git; see deploy.env.example.
_ENV="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/deploy.env"
# shellcheck disable=SC1090
[ -f "$_ENV" ] && . "$_ENV"

PVE_HOST="${PVE_HOST:?set PVE_HOST in site/deploy/deploy.env}"
CT_ID="${CT_ID:-117}"
REMOTE_DIR="${REMOTE_DIR:-/opt/clickgraft}"
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
rm -rf /tmp/cg-build && mkdir -p /tmp/cg-build/html /tmp/cg-build/stats
cp "$ZIP"                    /tmp/cg-build/html/ClickGraft.zip

# The page quotes the download's SHA-256. Substituting it at deploy time from
# the very file being shipped is the only way that number cannot drift: a hash
# typed into the HTML would silently go stale the first time the app is rebuilt,
# and a wrong fingerprint is worse than none — it teaches people the check is
# meaningless.
SHA="$(shasum -a 256 "$ZIP" | cut -d' ' -f1)"
sed "s|{{ZIP_SHA256}}|$SHA|g" "$SITE/index.html" > /tmp/cg-build/html/index.html
printf '%s  ClickGraft.zip\n' "$SHA" > /tmp/cg-build/html/ClickGraft.zip.sha256
grep -q '{{ZIP_SHA256}}' /tmp/cg-build/html/index.html && { echo "✗ hash placeholder not substituted" >&2; exit 1; }
echo "    sha256 $SHA"
cp "$HERE/docker-compose.yml" "$HERE/nginx.conf" /tmp/cg-build/
cp "$HERE/stats/run-goaccess.sh" /tmp/cg-build/stats/
cp "$HERE/summary.sh"        /tmp/cg-build/
printf '%s\n' "$(cd "$ROOT" && git rev-parse --short HEAD)" > /tmp/cg-build/html/.build

echo "    site $(du -h /tmp/cg-build/html/index.html | cut -f1), download $(du -h /tmp/cg-build/html/ClickGraft.zip | cut -f1)"

echo "==> rsync to ${PVE_HOST}:${STAGE}"
ssh "$PVE_HOST" "mkdir -p '$STAGE'"
rsync -az --delete /tmp/cg-build/ "$PVE_HOST:$STAGE/"

echo "==> push into CT ${CT_ID} and restart compose"
# Unquoted heredoc on purpose: the vars expand here and arrive as literals.
ssh "$PVE_HOST" bash -s <<EOF
set -euo pipefail
pct exec $CT_ID -- mkdir -p '$REMOTE_DIR'
# --exclude .env would be wrong here: tar only carries what we staged, and we
# never stage .env. The secret lives on the CT and is never copied off it.
tar -C '$STAGE' -cf - . | pct exec $CT_ID -- tar -C '$REMOTE_DIR' -xf -
pct exec $CT_ID -- sh -lc 'cd $REMOTE_DIR && chmod +x stats/run-goaccess.sh summary.sh'
pct exec $CT_ID -- sh -lc 'cd $REMOTE_DIR && test -s .env || { echo "✗ $REMOTE_DIR/.env has no tunnel token" >&2; exit 1; }'
pct exec $CT_ID -- sh -lc 'cd $REMOTE_DIR && docker compose up -d --remove-orphans'
EOF

echo "==> verify inside the CT"
ssh "$PVE_HOST" "pct exec $CT_ID -- sh -lc 'cd $REMOTE_DIR && docker compose exec -T web wget -qO- http://localhost/'" \
  | grep -q 'ClickGraft' && echo "✓ nginx is serving the page" || { echo "✗ nginx is not serving the page" >&2; exit 1; }

echo "==> verify ${HEALTH_URL}"
sleep 4
if curl -fsS --max-time 20 "$HEALTH_URL" | grep -q 'ClickGraft'; then
  echo "✓ page is live"
  code=$(curl -s -o /dev/null -w '%{http_code}' -I --max-time 30 "${HEALTH_URL}ClickGraft.zip")
  [ "$code" = 200 ] && echo "✓ download reachable" || { echo "✗ ClickGraft.zip returned HTTP $code" >&2; exit 1; }
else
  echo "! ${HEALTH_URL} did not answer."
  echo "  The container is serving correctly, so this is the tunnel: check that"
  echo "  /opt/clickgraft/.env has a live token and that the Cloudflare public"
  echo "  hostname routes clickgraft.elusive.net -> http://web:80."
  exit 2
fi
