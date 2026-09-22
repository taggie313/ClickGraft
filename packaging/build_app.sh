#!/bin/bash
# Build ClickGraft.app — an unsigned bundle. Run sign_and_notarize.sh next.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
OUT="${1:-$ROOT/dist}"
APP="$OUT/ClickGraft.app"

VERSION="${CLICKGRAFT_VERSION:-1.6.0}"
BUNDLE_ID="${CLICKGRAFT_BUNDLE_ID:-io.github.taggie313.clickgraft}"

echo "==> Building ClickGraft.app  (version $VERSION)"

# --- patch guard -----------------------------------------------------------
# Nothing else on the release path runs a test, so this is where a manifest op
# that is not on clickgraft/manifest_guard.py's allowlist stops the build:
# before anything is compiled, and before manifests/ is copied into the app
# (CLAUDE.md "Never distribute"). Export CLICKGRAFT_NEVER_DISTRIBUTE, naming a
# signature file kept outside this repository, and ops matching it are refused
# as well; the step says how many signatures it loaded.
#
# /usr/bin/python3 is Apple's stub, which refuses to run while an updated Xcode
# waits for its licence (Xcode 27.0, 15 Sep 2026). The Command Line Tools have
# no licence gate, so fall back to them, as ClickGraft.swift's Toolchain does.
echo "--> checking manifests against the patch guard"
GUARD_PY=(/usr/bin/python3)
if ! /usr/bin/python3 -c '' >/dev/null 2>&1 \
   && [ -x /Library/Developer/CommandLineTools/usr/bin/python3 ]; then
  GUARD_PY=(env DEVELOPER_DIR=/Library/Developer/CommandLineTools /usr/bin/python3)
fi
( cd "$ROOT" && PYTHONDONTWRITEBYTECODE=1 \
    "${GUARD_PY[@]}" -m clickgraft.manifest_guard manifests )

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# --- native AppKit front end (universal: opens on Intel Macs too) ----------
# swiftc has no -arch flag, so build each slice and lipo them together.
echo "--> compiling ClickGraft.swift"
TMPB="$(mktemp -d)"
# Once there are two source files, swiftc allows top-level code only in one
# named main.swift. Given ClickGraft.swift as it is, Swift 6.4 fails with
# "expressions are not allowed at the top level" (22 Sep 2026), so compile a
# copy under that name rather than renaming the file everyone knows.
cp "$HERE/ClickGraft.swift" "$TMPB/main.swift"
# The module cache goes in the build's own temporary folder, not swiftc's
# default per-user cache under ~/Library, which Xcode shares: the pass that
# added this (Sep 2026) built in a sandbox that could not write there, and a
# fresh cache cannot hold modules another toolchain left behind.
for arch in arm64 x86_64; do
  swiftc -O -target "${arch}-apple-macos12.0" -module-cache-path "$TMPB/module-cache-$arch" \
         -o "$TMPB/ClickGraft-$arch" "$TMPB/main.swift" "$HERE/BackendTransport.swift" -framework AppKit
done
lipo -create -output "$APP/Contents/MacOS/ClickGraft" \
     "$TMPB/ClickGraft-arm64" "$TMPB/ClickGraft-x86_64"
rm -rf "$TMPB"

# --- payload ---------------------------------------------------------------
echo "--> copying payload"
/usr/bin/rsync -a --exclude='__pycache__' --exclude='*.pyc' \
    "$ROOT/clickgraft" "$APP/Contents/Resources/"
/usr/bin/rsync -a "$ROOT/manifests" "$APP/Contents/Resources/"
cp "$ROOT/LICENSE" "$ROOT/NOTICE" "$APP/Contents/Resources/"

# Icon. Rendered from packaging/icon.svg if it is missing or older than the
# source, so editing the SVG is enough — nobody has to remember a second step.
if [ ! -f "$HERE/AppIcon.icns" ] || [ "$HERE/icon.svg" -nt "$HERE/AppIcon.icns" ]; then
  sh "$HERE/make-icon.sh" >/dev/null
fi
cp "$HERE/AppIcon.icns" "$APP/Contents/Resources/"

# Source record: the sha256 of every file the app was copied or built from.
# check_release.py --artifact compares it with the sources committed at the
# release tag, so a notarised binary that no longer matches its source is
# refused before it ships (CLAUDE.md, "Shipping a release"). After the icon,
# because make-icon.sh may just have rewritten AppIcon.icns. Through GUARD_PY,
# for the same Xcode licence gate as the patch guard above.
echo "--> recording the sources"
( cd "$ROOT" && PYTHONDONTWRITEBYTECODE=1 \
    "${GUARD_PY[@]}" "$HERE/check_release.py" --record "$APP/Contents/Resources/build-source.json" )

# --- Info.plist ------------------------------------------------------------
echo "--> writing Info.plist"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>              <string>ClickGraft</string>
    <key>CFBundleIconFile</key>          <string>AppIcon</string>
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
