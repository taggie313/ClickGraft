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
# Hardened runtime is mandatory for notarization. No entitlements are needed:
# the launcher execs /usr/bin/python3, and after exec the process runs under
# Apple's own signature and entitlements, not ours.
echo "--> removing stale signatures and metadata"
/usr/bin/xattr -cr "$APP"
find "$APP" -name '.DS_Store' -delete 2>/dev/null || true
find "$APP" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

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
spctl --assess --type execute --verbose=4 "$APP" 2>&1 | sed 's/^/    /'
xcrun stapler validate "$APP" 2>&1 | sed 's/^/    /'

echo
echo "Ready to distribute: $ZIP"
echo "Users can now double-click normally — no right-click, no quarantine warning."
