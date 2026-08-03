# Shared preflight for the read-only tools. Sourced, not executed.
#
# Exists because an unreachable host used to look exactly like an empty log:
# ssh failed, stderr went nowhere, and the summary printed zeros. "No visitors
# today" and "I could not reach the server" are opposite facts and must never
# render the same way.

_ENV="$(cd "$(dirname "${BASH_SOURCE[1]:-$0}")" && pwd)/deploy.env"
# shellcheck disable=SC1090
[ -f "$_ENV" ] && . "$_ENV"

PVE_HOST="${PVE_HOST:?set PVE_HOST in site/deploy/deploy.env}"
CT_ID="${CT_ID:-117}"
REMOTE_DIR="${REMOTE_DIR:-/opt/clickgraft}"

die() { printf '✗ %s\n' "$*" >&2; exit 1; }

require_host() {
  ssh -o BatchMode=yes -o ConnectTimeout=8 "$PVE_HOST" true 2>/dev/null && return 0

  printf '✗ cannot reach %s over SSH.\n\n' "$PVE_HOST" >&2
  # Almost always this, and the symptom is indistinguishable from silence.
  if command -v /Applications/Tailscale.app/Contents/MacOS/Tailscale >/dev/null 2>&1 \
     && ! /Applications/Tailscale.app/Contents/MacOS/Tailscale status >/dev/null 2>&1; then
    printf '  Tailscale is not running on this Mac, and %s is a tailnet address.\n' "$PVE_HOST" >&2
    printf '  Start it with:  Tailscale up --accept-dns=false\n\n' >&2
  fi
  printf '  NOTHING WAS READ. This is not "no traffic" — the numbers below would\n' >&2
  printf '  have been zeros for the wrong reason, so none are shown.\n' >&2
  exit 1
}

# Run a command in the CT, failing loudly rather than returning empty output.
ct() {
  local out rc
  out="$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$PVE_HOST" \
          "pct exec $CT_ID -- sh -lc '$1'" 2>&1)" ; rc=$?
  if [ $rc -ne 0 ]; then
    printf '✗ command failed inside CT %s (exit %d):\n%s\n' "$CT_ID" "$rc" "$out" >&2
    exit 1
  fi
  printf '%s\n' "$out"
}
