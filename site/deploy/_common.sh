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
# 136, not 117: CT 117 was ClickGraft's own container and was destroyed when the
# shared edge host took the site over. redeploy.sh sources this file, so a stale
# default here would be a stale default there too.
CT_ID="${CT_ID:-136}"
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

    # It is not here. Follow it rather than stopping: PVE_HOST is an entry
    # point into the cluster, not the answer to where the container lives. A
    # migration is a normal event and it should not cost a failed deploy —
    # especially since release.sh has already tagged, notarised and published
    # by the time this runs, leaving the site as the one artifact still
    # pointing at the old version.
    #
    # This asks only after the direct check has failed, so a normal run costs
    # no extra round trip and there is nothing to compare: the node name that
    # comes back is never weighed against the configured address, which is
    # what would otherwise report a move on every single run.
    _node="$(ssh -o BatchMode=yes -o ConnectTimeout=8 "$PVE_HOST" \
               "pvesh get /cluster/resources --type vm --output-format json" 2>/dev/null \
             | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
print(next((str(r.get("node","")) for r in d
            if str(r.get("vmid")) == sys.argv[1] and r.get("status") == "running"), ""))' \
               "$CT_ID" 2>/dev/null)"

    if [ -z "$_node" ]; then
      printf '✗ CT %s is not on %s, and the cluster does not list it running.\n\n' \
             "$CT_ID" "$PVE_HOST" >&2
      printf '  NOTHING WAS DONE.\n' >&2
      exit 1
    fi

    # Confirm the new target before adopting it. Being told a node name is not
    # the same as being able to reach it and find the container there, and
    # switching to a host we have not checked would just move the failure.
    _new="${PVE_HOST%%@*}@$_node"
    if ! ssh -o BatchMode=yes -o ConnectTimeout=8 "$_new" \
             "pct status $CT_ID" >/dev/null 2>&1; then
      printf '✗ CT %s has moved to node %s, which this Mac cannot reach as %s.\n\n' \
             "$CT_ID" "$_node" "$_new" >&2
      printf '  Set PVE_HOST in site/deploy/deploy.env to an address for %s.\n\n' "$_node" >&2
      printf '  NOTHING WAS DONE.\n' >&2
      exit 1
    fi

    printf '==> CT %s has moved to %s (deploy.env says %s); following it\n' \
           "$CT_ID" "$_node" "${PVE_HOST#*@}"
    PVE_HOST="$_new"
    return 0
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
