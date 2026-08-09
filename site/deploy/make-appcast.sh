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
# The release HISTORY, not just the newest one.
#
# importance describes a release relative to the one before it, which stops
# being the question the moment somebody skips a version: on 1.3.0, with an
# important 1.4.0 followed by a cosmetic 1.4.1, a flat field says "you don't
# need it" while hiding the one release that mattered. The app walks entries
# newer than its own and takes the most serious, so skipped releases cannot
# hide behind a later trivial one.
#
# Built from git tags, reading each tag's packaging/release.json. Tags from
# before that file existed default to "recommended" — the safe direction.
HISTORY=$(cd "$(dirname "$0")/../.." && /usr/bin/python3 - <<'PYEOF'
import json, subprocess
tags = subprocess.run(["git", "tag", "--sort=v:refname"],
                      capture_output=True, text=True).stdout.split()
out = []
for t in tags:
    r = subprocess.run(["git", "show", f"{t}:packaging/release.json"],
                       capture_output=True, text=True)
    imp, summ = "recommended", ""
    if r.returncode == 0:
        try:
            d = json.loads(r.stdout)
            v = d.get("importance", "recommended")
            imp = v if v in ("optional", "recommended", "important") else "recommended"
            summ = d.get("summary", "")
        except Exception:
            pass
    out.append({"version": t.lstrip("v"), "importance": imp, "summary": summ})
print(json.dumps(out, indent=4)[1:-1].strip())
PYEOF
)

cat > "$OUT" <<JSON
{
  "version": "$VER",
  "importance": "$IMPORTANCE",
  "summary": "$SUMMARY",
  "released": "$WHEN",
  "url": "https://clickgraft.elusive.net/",
  "download": "https://clickgraft.elusive.net/ClickGraft.zip",
  "sha256": "$SHA",
  "notes": "https://github.com/taggie313/ClickGraft/releases",
  "releases": [
    $HISTORY
  ]
}
JSON
