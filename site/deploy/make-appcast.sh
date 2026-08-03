#!/bin/sh
# Write appcast.json from the app being shipped, so the advertised version is
# by construction the version in the zip. Hand-editing it is how a tool starts
# telling everyone to upgrade to something that was never released.
set -eu
APP="$1"; OUT="$2"; SHA="$3"; ZIP="$4"
VER=$(/usr/bin/defaults read "$APP/Contents/Info.plist" CFBundleShortVersionString)

# How to announce it. Lives in packaging/release.json beside the code, so it is
# reviewed in the same commit as the change it describes. Falls back to
# "recommended" when the file is missing or the value is unrecognised: an update
# whose importance we cannot read must never be presented as ignorable.
REL="$(cd "$(dirname "$0")/../../packaging" && pwd)/release.json"
IMPORTANCE=recommended
SUMMARY=""
if [ -f "$REL" ]; then
  IMPORTANCE=$(/usr/bin/python3 -c "
import json
v=json.load(open('$REL')).get('importance','recommended')
print(v if v in ('optional','recommended','important') else 'recommended')")
  SUMMARY=$(/usr/bin/python3 -c "
import json
print(json.load(open('$REL')).get('summary','').replace(chr(34),'').strip())")
fi
WHEN=$(date -u -r "$ZIP" '+%Y-%m-%dT%H:%M:%SZ')
cat > "$OUT" <<JSON
{
  "version": "$VER",
  "importance": "$IMPORTANCE",
  "summary": "$SUMMARY",
  "released": "$WHEN",
  "url": "https://clickgraft.elusive.net/",
  "download": "https://clickgraft.elusive.net/ClickGraft.zip",
  "sha256": "$SHA",
  "notes": "https://github.com/taggie313/ClickGraft/releases"
}
JSON
