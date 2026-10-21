#!/usr/bin/env bash
# Build "Click Updater Fix.app" — an unsigned, universal bundle.
# Run sign_and_notarize.sh on it next; it takes the app path as its first
# argument.
#
# Separate from build_app.sh on purpose: this ships none of ClickGraft's
# payload (no Python pin, no manifests, no backend). It is one Swift file and
# an icon, and it must stay that small — it is handed to people who are being
# asked to trust an app they did not go looking for.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
OUT="${1:-$ROOT/dist}"
APP="$OUT/Click Updater Fix.app"

VERSION="${FIX_VERSION:-1.0.0}"
BUNDLE_ID="${FIX_BUNDLE_ID:-io.github.taggie313.clickupdaterfix}"

echo "==> Building Click Updater Fix.app  (version $VERSION)"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# swiftc has no -arch flag, so build each slice and lipo them together. Intel
# matters here more than usual: the Macs most likely to be stuck on an old HP
# Click are the older ones.
echo "--> compiling ClickUpdaterFix.swift"
TMPB="$(mktemp -d)"
trap 'rm -rf "$TMPB"' EXIT
cp "$HERE/ClickUpdaterFix.swift" "$TMPB/main.swift"
for arch in arm64 x86_64; do
  swiftc -O -target "${arch}-apple-macos12.0" \
         -module-cache-path "$TMPB/module-cache-$arch" \
         -o "$TMPB/fix-$arch" "$TMPB/main.swift" -framework AppKit
done
lipo -create -output "$APP/Contents/MacOS/ClickUpdaterFix" \
     "$TMPB/fix-arm64" "$TMPB/fix-x86_64"

if [ -f "$HERE/AppIcon.icns" ]; then
  cp "$HERE/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
  ICON_KEY='<key>CFBundleIconFile</key>        <string>AppIcon</string>'
else
  ICON_KEY=''
fi
cp "$ROOT/LICENSE" "$APP/Contents/Resources/" 2>/dev/null || true

echo "--> writing Info.plist"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>              <string>Click Updater Fix</string>
    <key>CFBundleDisplayName</key>       <string>Click Updater Fix</string>
    <key>CFBundleExecutable</key>        <string>ClickUpdaterFix</string>
    <key>CFBundleIdentifier</key>        <string>$BUNDLE_ID</string>
    <key>CFBundleShortVersionString</key><string>$VERSION</string>
    <key>CFBundleVersion</key>           <string>$VERSION</string>
    <key>CFBundlePackageType</key>       <string>APPL</string>
    <key>LSMinimumSystemVersion</key>    <string>12.0</string>
    <key>LSApplicationCategoryType</key> <string>public.app-category.utilities</string>
    <key>NSHighResolutionCapable</key>   <true/>
    $ICON_KEY
</dict>
</plist>
PLIST

# Ad-hoc seal so a scratch build runs locally. This is NOT distributable:
# macOS calls an ad-hoc-signed app carrying a quarantine flag "damaged and
# can't be opened" and offers to bin it. sign_and_notarize.sh replaces this.
echo "--> ad-hoc signing (scratch only)"
/usr/bin/codesign --force --sign - --timestamp=none "$APP" >/dev/null 2>&1

echo "==> Built: $APP"
echo "    Next: ./packaging/sign_and_notarize.sh \"$APP\""
