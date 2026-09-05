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
  if ssh -o BatchMode=yes -o ConnectTimeout=8 "$PVE_HOST" true 2>/dev/null; then
    # Reaching the node is not the same as reaching the container, and this
    # used to stop at the former. CT 136 was migrated bb2 -> bb1 on 2 Sep 2026;
    # bb2 kept answering ssh, so this returned 0 and every deploy failed several
    # steps later with "Configuration file does not exist" — a preflight that
    # passes and then lets the run die is worse than no preflight, because it
    # tells you the thing it was asked to rule out has been ruled out.
    ssh -o BatchMode=yes -o ConnectTimeout=8 "$PVE_HOST" \
        "pct status $CT_ID" >/dev/null 2>&1 && return 0

    printf '✗ CT %s is not on %s.\n\n' "$CT_ID" "$PVE_HOST" >&2
    # Ask the cluster where it went. The answer is one query away and the
    # alternative is the reader guessing which node was rebuilt this week.
    _node="$(ssh -o BatchMode=yes -o ConnectTimeout=8 "$PVE_HOST" \
               "pvesh get /cluster/resources --type vm --output-format json" 2>/dev/null \
             | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
print(next((str(r.get("node","")) for r in d if str(r.get("vmid")) == sys.argv[1]), ""))' \
               "$CT_ID" 2>/dev/null)"
    if [ -n "$_node" ]; then
      printf '  It is on node %s now. Point PVE_HOST at that node in\n' "$_node" >&2
      printf '  site/deploy/deploy.env, then run this again.\n\n' >&2
    else
      printf '  The cluster does not list a guest with id %s at all.\n\n' "$CT_ID" >&2
    fi
    printf '  NOTHING WAS DONE.\n' >&2
    exit 1
  fi

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
