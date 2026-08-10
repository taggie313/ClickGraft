# Shared preflight for the read-only tools. Sourced, not executed.
#
# Exists because an unreachable host used to look exactly like an empty log:
# ssh failed, stderr went nowhere, and the summary printed zeros. "No visitors
# today" and "I could not reach the server" are opposite facts and must never
# render the same way.

# BASH_SOURCE[0] — this file — not [1], the caller. deploy.env sits next to THIS
# script, and keying off the caller silently broke the moment a script lived in
# a subdirectory (watch/), which then failed claiming PVE_HOST was unset.
_ENV="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/deploy.env"
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
#
# The script is base64'd rather than interpolated into a quoted string. It has
# to survive two shells (local -> ssh -> pct exec -> sh) and the first version
# wrapped it in single quotes, so a sed expression containing one broke the
# remote parse and the tool exited 2 with no output — the same silent-empty
# failure this file exists to prevent, reintroduced by the fix for it.
ct() {
  local payload out rc
  payload="$(printf '%s' "$1" | base64 | tr -d '\n')"
  out="$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$PVE_HOST" \
          "pct exec $CT_ID -- sh -c \"echo $payload | base64 -d | sh\"" 2>&1)" ; rc=$?
  if [ $rc -ne 0 ]; then
    printf '✗ command failed inside CT %s (exit %d):\n%s\n' "$CT_ID" "$rc" "$out" >&2
    exit 1
  fi
  printf '%s\n' "$out"
}
