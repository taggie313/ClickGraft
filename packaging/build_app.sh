#!/bin/bash
# Build ClickGraft.app — an unsigned bundle. Run sign_and_notarize.sh next.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
OUT="${1:-$ROOT/dist}"
APP="$OUT/ClickGraft.app"

VERSION="${CLICKGRAFT_VERSION:-0.1.0}"
BUNDLE_ID="${CLICKGRAFT_BUNDLE_ID:-io.github.taggie313.clickgraft}"

echo "==> Building ClickGraft.app  (version $VERSION)"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# --- compiled launcher (universal: works on Intel Macs too) ----------------
echo "--> compiling launcher"
clang -O2 -Wall -Wextra -arch arm64 -arch x86_64 \
      -mmacosx-version-min=12.0 \
      -o "$APP/Contents/MacOS/ClickGraft" "$HERE/launcher.c"

# --- payload ---------------------------------------------------------------
echo "--> copying payload"
/usr/bin/rsync -a --exclude='__pycache__' --exclude='*.pyc' \
    "$ROOT/clickgraft" "$APP/Contents/Resources/"
/usr/bin/rsync -a "$ROOT/manifests" "$APP/Contents/Resources/"
cp "$ROOT/LICENSE" "$ROOT/NOTICE" "$APP/Contents/Resources/"

# --- Info.plist ------------------------------------------------------------
echo "--> writing Info.plist"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>              <string>ClickGraft</string>
    <key>CFBundleDisplayName</key>       <string>ClickGraft</string>
    <key>CFBundleExecutable</key>        <string>ClickGraft</string>
    <key>CFBundleIdentifier</key>        <string>$BUNDLE_ID</string>
    <key>CFBundlePackageType</key>       <string>APPL</string>
    <key>CFBundleShortVersionString</key><string>$VERSION</string>
    <key>CFBundleVersion</key>           <string>$VERSION</string>
    <key>LSMinimumSystemVersion</key>    <string>12.0</string>
    <key>NSHighResolutionCapable</key>   <true/>
    <key>LSApplicationCategoryType</key> <string>public.app-category.utilities</string>
    <key>NSHumanReadableCopyright</key>
    <string>Copyright (c) 2026 Joshua Lutz. MIT licensed. Not affiliated with HP Inc.</string>
</dict>
</plist>
PLIST

echo "--> stripping quarantine and stray metadata"
/usr/bin/xattr -cr "$APP" 2>/dev/null || true
find "$APP" -name '.DS_Store' -delete 2>/dev/null || true

echo
echo "Built: $APP"
/usr/bin/lipo -archs "$APP/Contents/MacOS/ClickGraft" | sed 's/^/  launcher archs: /'
du -sh "$APP" | sed 's/^/  size: /'
echo
echo "Next:  $HERE/sign_and_notarize.sh"
