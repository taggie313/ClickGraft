#!/usr/bin/env bash
# Render packaging/icon.svg into AppIcon.icns and the web assets.
#
#   ./packaging/make-icon.sh
#
# One SVG is the source of truth, so the app icon and the site can't drift.
#
# Rendered with qlmanage, not ImageMagick: IM's bundled SVG renderer ignores
# gradients, filters and transforms — it produced a black tile with the plaster
# off-canvas. Quick Look uses the same engine as the rest of macOS and gets it
# right. Everything is rendered once at 1024 and downsampled with sips, which
# resamples better than re-rendering small.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SVG="$HERE/icon.svg"
OUT="${1:-$HERE/AppIcon.icns}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

qlmanage -t -s 1024 -o "$WORK" "$SVG" >/dev/null 2>&1
MASTER="$WORK/$(basename "$SVG").png"
[ -s "$MASTER" ] || { echo "✗ qlmanage did not render $SVG" >&2; exit 1; }
[ "$(sips -g pixelWidth "$MASTER" | awk '/pixelWidth/{print $2}')" = 1024 ] \
  || { echo "✗ master render is not 1024px" >&2; exit 1; }

SET="$WORK/AppIcon.iconset"; mkdir -p "$SET"
for spec in "16 icon_16x16" "32 icon_16x16@2x" "32 icon_32x32" "64 icon_32x32@2x" \
            "128 icon_128x128" "256 icon_128x128@2x" "256 icon_256x256" \
            "512 icon_256x256@2x" "512 icon_512x512" "1024 icon_512x512@2x"; do
  set -- $spec
  sips -s format png -z "$1" "$1" "$MASTER" --out "$SET/$2.png" >/dev/null 2>&1
done

iconutil -c icns "$SET" -o "$OUT"
echo "✓ $OUT ($(du -h "$OUT" | cut -f1))"

# Web: the same master, for the page header and social previews.
cp "$SVG" "$HERE/../site/icon.svg"

# Social preview as JPEG, not PNG. The artwork is a smooth gradient over an
# opaque tile: PNG stores every pixel of that and came to 957 KB at 1024px,
# which every link-preview bot then fetched — four times in one evening from a
# single share. JPEG at 640px is visually identical in a chat bubble.
sips -s format jpeg -s formatOptions 82 -z 640 640 "$MASTER" \
     --out "$HERE/../site/og.jpg" >/dev/null 2>&1

# apple-touch-icon has a defined size of 180px. Shipping a 512px one just made
# iOS download four times the pixels it uses.
sips -s format png -z 180 180 "$MASTER" --out "$HERE/../site/apple-touch-icon.png" >/dev/null 2>&1
rm -f "$HERE/../site/icon-512.png" "$HERE/../site/icon-1024.png"

# favicon.ico as well as the SVG. Browsers request /favicon.ico implicitly
# whatever the page declares — four such requests had already 404'd on real
# visits — and it covers anything that will not take an SVG favicon.
#
# ImageMagick is fine here: it only ever sees PNG. It is the SVG renderer that
# is unusable, which is why the master comes from qlmanage.
for z in 16 32 48; do
  sips -s format png -z $z $z "$MASTER" --out "$WORK/f$z.png" >/dev/null 2>&1
done
magick "$WORK/f16.png" "$WORK/f32.png" "$WORK/f48.png" "$HERE/../site/favicon.ico"
echo "✓ site/icon.svg, og.jpg, apple-touch-icon.png, favicon.ico"
