#!/bin/sh
# Write appcast.json from the app being shipped, so the advertised version is
# by construction the version in the zip. Hand-editing it is how a tool starts
# telling everyone to upgrade to something that was never released.
set -eu
APP="$1"; OUT="$2"; SHA="$3"; ZIP="$4"
VER=$(/usr/bin/defaults read "$APP/Contents/Info.plist" CFBundleShortVersionString)
WHEN=$(date -u -r "$ZIP" '+%Y-%m-%dT%H:%M:%SZ')
cat > "$OUT" <<JSON
{
  "version": "$VER",
  "released": "$WHEN",
  "url": "https://clickgraft.elusive.net/",
  "download": "https://clickgraft.elusive.net/ClickGraft.zip",
  "sha256": "$SHA",
  "notes": "https://github.com/taggie313/ClickGraft/releases"
}
JSON
