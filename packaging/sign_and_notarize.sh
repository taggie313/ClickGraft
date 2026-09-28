#!/bin/bash
# Sign ClickGraft.app with a Developer ID, notarize it, and staple the ticket.
#
# ONE-TIME SETUP — do this yourself; this script never sees your credentials:
#
#   xcrun notarytool store-credentials clickgraft-notary \
#       --apple-id "you@example.com" \
#       --team-id  "U9U8JC2JT7" \
#       --password "abcd-efgh-ijkl-mnop"      # app-specific password, not your
#                                             # Apple ID password. Create one at
#                                             # appleid.apple.com > Sign-In and
#                                             # Security > App-Specific Passwords
#
# That stores the secret in your login keychain under the profile name. From
# then on this script only refers to the profile.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
APP="${1:-$ROOT/dist/ClickGraft.app}"
PROFILE="${NOTARY_PROFILE:-clickgraft-notary}"

[ -d "$APP" ] || { echo "No app at $APP — run build_app.sh first." >&2; exit 1; }

# --- identity --------------------------------------------------------------
IDENTITY="${CODESIGN_IDENTITY:-}"
if [ -z "$IDENTITY" ]; then
    IDENTITY="$(security find-identity -v -p codesigning \
        | grep "Developer ID Application" | head -1 \
        | sed -E 's/.*"(.*)"/\1/')"
fi
[ -n "$IDENTITY" ] || {
    echo "No 'Developer ID Application' identity found in your keychain." >&2
    echo "Notarization requires one — an 'Apple Development' cert will not do." >&2
    exit 1
}
echo "==> Signing as: $IDENTITY"

# --- check the notary profile BEFORE doing any work ------------------------
# Signing and zipping take ~30s; discovering a missing credential profile
# afterwards wastes all of it and reads like a failure rather than setup.
echo "--> checking notary credentials"
# Capture first, then match. Piping into grep does not work here: `set -o
# pipefail` makes the pipeline non-zero because notarytool itself exits
# non-zero on the error, so the `if` reads false even when grep matched — and
# the check silently never fires.
NOTARY_PROBE="$(xcrun notarytool history --keychain-profile "$PROFILE" 2>&1 || true)"
if printf '%s' "$NOTARY_PROBE" | grep -q "No Keychain password item found"; then
    TEAM_ID="$(printf '%s' "$IDENTITY" | sed -E 's/.*\(([A-Z0-9]+)\)$/\1/')"
    cat >&2 <<MSG

Notary credential profile "$PROFILE" does not exist yet — this is one-time
setup, not a failure. Nothing has been signed or submitted.

Create it with your OWN credentials:

  xcrun notarytool store-credentials $PROFILE \\
      --apple-id "your-apple-id@example.com" \\
      --team-id  "$TEAM_ID" \\
      --password "xxxx-xxxx-xxxx-xxxx"

The password is an APP-SPECIFIC PASSWORD, not your Apple ID password. Generate
one at appleid.apple.com > Sign-In and Security > App-Specific Passwords.

It is stored in your login keychain; this script only ever refers to the
profile by name and never sees the secret. Then re-run this script.
MSG
    exit 1
fi
echo "    profile '$PROFILE' found"

# --- sign ------------------------------------------------------------------
# Hardened runtime is mandatory for notarization. No entitlements are needed,
# and the reason is worth writing down because it moved twice in one day.
#
# A released app carries no interpreter: it ships packaging/python-pin.json and a
# Mac without Apple's developer tools fetches one into its own Application
# Support. So the loop below normally signs the launcher and nothing else, and the
# interpreter is signed where it is built, by fetch_python.build_payload.
#
# What matters either way is that the interpreter and the support files it loads
# carry ONE Developer ID. Under the hardened runtime a Python.framework is subject
# to library validation: every dylib loaded must share the loading binary's Team
# ID or be an Apple platform binary. Ad-hoc signing therefore does not work, and
# fails in a way that never mentions signing. Measured 28 September 2026 on
# macOS 12.4:
#
#   dyld: Library not loaded: @loader_path/../Python
#   Reason: ... mapped file has no Team ID and is not a platform binary
#           (signed with custom identity or adhoc?)
#
# CLICKGRAFT_BUNDLE_PYTHON=1 puts the framework back inside the app, for an estate
# with no internet. The loop below then covers it too, with the same identity, for
# the same reason -- and check_release.py refuses to release such a build.
#
# If a future change ever needs mixed identities, the entitlement to add is
# com.apple.security.cs.disable-library-validation -- and adding it should be a
# deliberate decision, not a reflex, because it turns the check off entirely.
echo "--> removing stale signatures and metadata"
/usr/bin/xattr -cr "$APP"
find "$APP" -name '.DS_Store' -delete 2>/dev/null || true
# Not inside a Python.framework, when a CLICKGRAFT_BUNDLE_PYTHON=1 build has one:
# its __pycache__ IS the stdlib, precompiled. Deleting it would make the
# interpreter recompile every module on every launch -- it cannot write .pyc back
# into a signed bundle -- and would change the framework from the one
# packaging/python-pin.json names.
find "$APP" -name '__pycache__' -type d \
     ! -path "*/Python.framework/*" -exec rm -rf {} + 2>/dev/null || true

echo "--> signing (inner to outer)"
# Sign any nested Mach-O first, then the bundle itself. --deep is deprecated
# and unreliable; walk it explicitly.
while IFS= read -r f; do
    /usr/bin/codesign --force --timestamp --options runtime --sign "$IDENTITY" "$f"
done < <(find "$APP/Contents" -type f -perm +111 ! -path "*/MacOS/ClickGraft" \
         -exec sh -c 'file "$1" | grep -q Mach-O' _ {} \; -print)

/usr/bin/codesign --force --timestamp --options runtime --sign "$IDENTITY" "$APP"

echo "--> verifying signature"
/usr/bin/codesign --verify --strict --verbose=2 "$APP"

# --- notarize --------------------------------------------------------------
ZIP="${APP%.app}.zip"
echo "--> zipping for submission"
rm -f "$ZIP"
/usr/bin/ditto -c -k --keepParent "$APP" "$ZIP"

echo "--> submitting to Apple (this usually takes a few minutes)"
if ! xcrun notarytool submit "$ZIP" --keychain-profile "$PROFILE" --wait; then
    cat >&2 <<MSG

Apple rejected the submission, or the upload failed.

The app IS signed correctly at this point (the signature was verified above) —
what failed is Apple's review of it. Get the actual reason:

  xcrun notarytool history --keychain-profile $PROFILE
  xcrun notarytool log <submission-id> --keychain-profile $PROFILE

The log names the specific file and problem. The usual causes are a nested
binary missing the hardened runtime, or an unsigned executable somewhere in
Contents/.
MSG
    exit 1
fi

echo "--> stapling the ticket to the app"
xcrun stapler staple "$APP"

echo "--> re-zipping the stapled app for distribution"
rm -f "$ZIP"
/usr/bin/ditto -c -k --keepParent "$APP" "$ZIP"

# --- final check -----------------------------------------------------------
echo
echo "==> Gatekeeper assessment (what a downloader's Mac will do):"
# Full path: spctl lives in /usr/sbin, which a restricted PATH leaves out. On
# 14 Sep 2026 a bare `spctl` was "command not found", and under set -e the
# script stopped at the one line that says whether the release is safe to ship
# -- indistinguishable, at a glance, from Gatekeeper rejecting it.
#
# The app INSIDE the zip, not dist/ClickGraft.app: the zip is what people
# download, and it is re-created after stapling, so it is the thing to prove.
CHECK_DIR="$(mktemp -d)"
/usr/bin/ditto -x -k "$ZIP" "$CHECK_DIR"
SHIPPED="$CHECK_DIR/$(basename "$APP")"
if ! verdict=$(/usr/sbin/spctl --assess --type execute --verbose=4 "$SHIPPED" 2>&1) \
   || ! staple=$(xcrun stapler validate "$SHIPPED" 2>&1); then
  printf '%s\n%s\n' "${verdict:-}" "${staple:-}" | sed 's/^/    /'
  rm -rf "$CHECK_DIR"
  echo "✗ Gatekeeper would not accept the app in $ZIP. Do not distribute it." >&2
  exit 1
fi
printf '%s\n%s\n' "$verdict" "$staple" | sed 's/^/    /'
rm -rf "$CHECK_DIR"

echo
echo "Ready to distribute: $ZIP"
echo "Users can now double-click normally — no right-click, no quarantine warning."
