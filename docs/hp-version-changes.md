# What HP changed, and how it was checked

Two removals in HP Click that people ask about, and the macOS HP's own libraries
declare. The removals were measured 9 September 2026 by diffing HP's own
packages, not read from a changelog — HP published neither change.

**This goes stale with every HP release.** Every "last version" claim below is
bounded by the builds that could be obtained: 4.8.117, 4.8.118, 4.10.38 and
4.10.42, plus the macOS 4.11.31 where a section says so. Re-run the diff before
repeating any of it.

## The seven printer entries

Removed in **4.8.118**, not 4.10.42 — an earlier version of the site said
otherwise and told owners to install 4.8.118, which is the release that broke
them.

| Version | Rows | Bytes | sha256 of the file |
|---|---|---|---|
| 4.8.117 | 117 | 146,892 | `db9c035e15bc54bc…` |
| 4.8.118 | 110 | 138,170 | `9853aa6724b40fc1…` |
| 4.10.38 | 110 | 142,028 | `1921f8f5c38894bf…` |
| 4.10.42 | 110 | 142,028 | `1921f8f5c38894bf…` |
| 4.11.31 | 110 | 142,028 | `1921f8f5c38894bf…` |

Gone: T310 24-in, T320 24-in, T350 24-in, T720 24-in and 36-in, T750 24-in and
36-in. Five models, seven entries. Nothing added at any boundary. The 4.11.31 row
is the macOS package, read 22 September 2026: byte-identical to 4.10.42's.

**Windows and macOS get byte-identical files** — same sha256 at all three
versions where both exist. The *signature* beside it does not match across
platforms: `printersValidate.sign` is 177 bytes on Windows against 175 on macOS,
different hashes every version. The list is shared; the resources around it are
not.

### Reading it yourself

The file is `printersValidate.json` inside `app.asar`. HP's servers honour range
requests, so a single entry can be pulled without the whole 570 MB package.

Three things that cost a day:

1. **Two copies exist per package.** The one the app loads,
   `app/node_modules/DjConnServices/resources/`, is an *unpacked* asar entry —
   extract the archive and it lands as **zero bytes**, a placeholder. Read
   `app/troubleshootingtool/resources/` instead; identical size and integrity
   hash at every version.
2. **Diff by the `name` field.** 4.8.117 → 4.8.118 is a pure deletion, no
   surviving row changes a field. The churn is at 4.8.118 → 4.10.38, where all
   110 rows gain an `icon` field (`settings` on 69, `family` on 49) with the
   printer set and row order untouched. A whole-row diff spanning both reports
   all 117 rows as changed and tells you nothing.
3. That churn is why 138,170 bytes becomes 142,028 with no printer added.

### Why it can't be undone

Restoring the rows produces an app that will not launch: *"Cannot verify the
integrity of HP Click… The application will now close."* Established by A→B→A on
byte-identical builds differing only in that file. Worse than HP's own log string
for an unsupported printer, which claims the app still runs without printing.

## Windows DWF

Removed in **4.10.38**, not 4.10.42. The whole
`resources/app/appData/win64/DWF/` directory — 21 files, 30,789,032 bytes.
`jobprocessingShell.exe` lost 9,053,696 bytes in the same release
(26,006,600 → 16,952,904), consistent with the dispatch being compiled out,
though that part is inference from the size, not a test.

The engine was licensed third-party code, read from the DLL version resources:
Autodesk DWF Viewer 7.0.0.928 (`EPlotCore.dll`), Autodesk Heidi 9.2.56.0
(`heidilw.dll`, originally `HEIDI9.DLL`), Tech Soft 3D HOOPS 16.10.01
(`hoops1610.dll`), Autodesk PDK 2.3.0.11.mt, plus Qt5 and `dwfApp.exe`.

The licensed Autodesk and Tech Soft 3D components date from 2002-2008. Do not
repeat "newest copyright anywhere is 2009" -- an earlier version of this file
said that and it is wrong. `dwfApp.exe` is HP's own wrapper and carries
`Copyright (c) Hewlett-Packard 2016`, plus a bundled libjpeg from 2018. The
version resources alone do not show this: `dwfApp.exe` has no VS_VERSIONINFO at
all, so a parser that reads only structured resources reports nothing for it and
the ASCII strings in the file body have to be scanned too.

**macOS never had DWF.** No `appData/macx/DWF`, and not one path matching /dwf/i
in any obtainable macOS package. In the distributed `.nupkg` the entries carry a
`lib/net45/` prefix, so grepping a package listing for the installed path returns
nothing.

## HP's broken 4.10.38 download

`hpclick/darwin/HPClick-4.10.38.zip` serves 7,221,248 bytes against ~572 million
for its siblings. Truncated, not corrupt: a clean prefix, 55 valid local file
headers, 26 complete entries that inflate and CRC-verify, a 27th cut 274,595
bytes short, and no end-of-central-directory anywhere.

It is broken at HP's origin, not in transit — Akamai NetStorage's ETag is the MD5
of the stored object, and `c7e0eb458a19206a…` equals the MD5 of the delivered
bytes. Six fetches returned identical bytes, sha256 `4aa473958a37c331…`. The size
is 1763 × 4096 exactly. The `.dmg` for that version 404s, so **macOS 4.10.38
cannot be obtained from HP at all** — which is why the appeal for a copy exists
on the site. Kept out of the posts as a tangent; useful if someone is told to
re-download.

## The macOS HP's own libraries declare

Every version's `Info.plist` says `LSMinimumSystemVersion` 12.0. Libraries
inside it declare more. Read 22 September 2026 with `vtool -show-build`, both
slices of each file (they agree), and every copy of OpenSSL in a bundle —
`lib/`, `Frameworks/` and APPE's — agrees with the others:

| Version | `Info.plist` | `libmagic.1.dylib` | `libcrypto`, `libssl` |
|---|---|---|---|
| 4.8.117 | 12.0 | 15.0 | 13.0 |
| 4.8.118 | 12.0 | 15.0 | 13.0 |
| 4.10.42 | 12.0 | 15.0 | 15.0 |
| 4.11.31 | 12.0 | 15.0 | 15.0 |

OpenSSL moved from 13.0 to 15.0 between 4.8.118 and 4.10.42; 4.10.38 can't be
checked on macOS (above). These are declarations, not a test on an older macOS:
whether HP's builds actually need 15.0 is unknown, and `clickgraft/macos_floor.py`
has what is known. ClickGraft counts them all the same, so, with the Homebrew
bottles it adds, every copy needs macOS 15 from ClickGraft 1.5.9 on. HP's
download page lists 4.11.31 for macOS 12 to 26 regardless.

## HP keeps one installer and deletes the rest

Found 9 October 2026, when the version watcher's control file stopped resolving.

`hpdesignjetclick/` holds exactly one `.dmg` — the current release — and HP
deletes every older one when it publishes. That morning the only survivor was
`HPClick-4.11.32.dmg` (707,645,173 bytes, `Last-Modified: Wed, 30 Sep 2026
17:55:01 GMT`). 4.8.117, 4.8.118, 4.10.42 and 4.11.31 all returned 404 —
4.11.31 included, a week after it was the version HP was shipping.

4.10.42's removal, recorded in `capabilities.py` as a one-off, was the first
instance of this policy rather than an exception.

`hpclick/darwin/` is the opposite: it only ever grows. Builds from June and
August are still served there long after their installers were withdrawn. So:

- a composed `.dmg` URL is a good guess **only for the newest version**
- a `.zip` URL is the durable one, and is the app itself
- `availability()` is the only thing that can say which exist

This is also why the watcher's control is a zip. A pinned `.dmg` is guaranteed
to vanish on precisely the day there is something to find, so its disappearance
is the strongest new-release signal available — not evidence of blindness. The
watcher treated it as fatal and exited before sweeping; see
`site/deploy/watch/clickgraft-version-watch.sh`.

One inference died with this: a missing `.dmg` no longer means HP never
published one. It may have published it and pruned it since.

## Windows did not lead 4.11.32

Every earlier Mac build was preceded by a Windows release, which is why the
sweep centres its candidate range on the version `hpclick/x64/RELEASES` names.
4.11.32 broke that: on 9 October 2026 `RELEASES` still named
`hpclick-4.11.31-full.nupkg` while macOS had 4.11.32.

The sweep found it anyway, because 4.11.32 is adjacent to 4.11.31 and well
inside the generated range. A Mac-only build further from the Windows version
would fall outside it. Worth remembering before trusting the range alone.

## 4.11.31 → 4.11.32, read from the builds

HP published 4.11.32 on 30 September 2026 and, as far as can be found, said
nothing about what is in it: the macOS update feed carries no notes field (and
still advertises 4.8.118), there is no notes file beside the installer, and the
Squirrel `RELEASES` file is a hash, a URL and a byte count. So this was read out
of the two builds directly, 9 October 2026.

**Nothing ClickGraft cares about changed.** Both are capability-identical:

| | 4.11.31 | 4.11.32 |
|---|---|---|
| `LSMinimumSystemVersion` | 12.0 | 12.0 |
| Electron | 39.8.4 | 39.8.4 |
| main executable | arm64 + x86_64 | arm64 + x86_64 |
| `DjCoreServicesNative` | arm64 + x86_64 | arm64 + x86_64 |
| `DjConnServicesNative` | arm64 + x86_64 | arm64 + x86_64 |
| printers | 110 | 110 |
| `hp_native` / blockers | true / none | true / none |

The seven T-series printers dropped after 4.8.117 are still absent, so 4.11.32
is no use to a T310/T320/T350/T720/T750 owner either.

**What did change** — `app.asar` 248,349,123 → 248,332,894 bytes, same entry
count (18,628 packed, 25 unpacked), 2 added, 2 removed, 33 modified:

- The 2 added/removed are one case rename, in both vendored copies of `socks`:
  `receivebuffer.d.ts` → `receiveBuffer.d.ts`. A case-sensitivity fix.
- Dependency bumps: `ip-address` (grew ~1 KB per file across dist), 
  `brace-expansion` 11,721 → 14,403, `socks` and its `pac-proxy-agent` /
  `proxy-agent` copies (shrank).
- `app/assets/app.css` 622,376 → 596,564, the largest single change.
- `app/bundle.js` 3,372,551 → 3,372,063.
- Several files identical in size but different in hash (`machan.js`,
  `preload.js`, `package.json`, `troubleshootingtool/bundle.js`) — a rebuild.

`brace-expansion`, `ip-address` and `socks` all have public CVE history, and
the pattern — several transitive dependencies moved, no feature surface touched
— reads as a dependency/security refresh rather than a feature release. That is
an inference from the file list, not something HP stated.

**Consequence for the site:** 4.11.32 can carry the same recommendation as
4.11.31. It is native throughout and needs no graft.

## The whole history, read from the builds

HP publishes no changelog, so this is the substitute: every consecutive pair of
archived macOS builds, diffed inside `app.asar` by file. Produced 9 Oct 2026
from the nine bundles in the local archive, using `clickgraft/asar.py`.

Counts are packed entries; `+` added, `−` removed, `~` changed (size or hash).

| step | app.asar | entries | + | − | ~ | what it was |
|---|---|---|---|---|---|---|
| 3.7.83 → 4.4.59 | 198.3 → 205.2 MB | 16450 → 19354 | 7345 | 4314 | 2007 | a rebuild of most of the app |
| 4.4.59 → 4.5.21 | 205.2 → 205.3 MB | 19354 → 19359 | 6 | 1 | 41 | point release |
| 4.5.21 → 4.6.47 | 205.3 → 205.7 MB | 19359 → 19370 | 14 | 3 | 78 | point release |
| **4.6.47 → 4.8.117** | 205.7 → 201.1 MB | 19370 → 19202 | 757 | 1027 | **18470** | **Electron 8.2.3 → 39.8.4** |
| **4.8.117 → 4.8.118** | 201.1 → 201.4 MB | 19202 → 19198 | 2 | 6 | 47 | **the seven printers go** |
| 4.8.118 → 4.10.42 | 201.4 → 206.0 MB | 19198 → 19337 | 189 | 50 | 453 | notifications rework, new icon |
| **4.10.42 → 4.11.31** | 206.0 → 248.3 MB | 19337 → 18628 | 29 | 738 | 210 | **universal build** |
| 4.11.31 → 4.11.32 | 248.3 → 248.3 MB | 18628 → 18628 | 2 | 2 | 33 | dependency refresh |

### The four that matter

**4.6.47 → 4.8.117 — the Electron jump.** 18,470 of ~19,000 entries changed:
essentially every file. The native libraries under
`app/node_modules/canvas/build/Release/` and all four bundles are the largest
movers. This is the step that moved `LSMinimumSystemVersion` from 10.10.0 to
12.0 and the measured floor to 15.0, and it is why no build before 4.8.117 can
be reasoned about with the same tooling.

**4.8.117 → 4.8.118 — where the seven printers went.** Only 47 files changed,
and among the biggest are
`app/node_modules/DjConnServices/resources/printersValidate.json`, its CRLF
twin, and the troubleshooting tool's copy. The printer roster is a data file,
and HP edited it. That is the whole of the change that cost T310, T320, T350,
T720 and T750 owners their printers — not a rewrite, a list.

**4.8.118 → 4.10.42 — notifications.** `whatsnewdialog` removed; a
`notification-center` dialog plus banner/notificationbar/popup components added
under a renamed `notices/` tree. `resources/icons/app.icns` and `setup.icns`
both changed, so this is also where the app icon moved.

**4.10.42 → 4.11.31 — universal, and why the archive grew.** 738 entries
removed against only 29 added, yet `app.asar` grew by 42 MB. The largest size
changes are all `canvas` native libraries — `librsvg`, `libgio`, `libglib`,
`libgobject`, `libjpeg`. They became fat Mach-Os carrying both slices, which
costs more bytes in fewer files. Also added: `app/node/main/user-guide.js` and
a set of `user-guide-*.pdf` translations, replacing `guide-window.js`.

### What this cannot say

File names and sizes, not behaviour. A changed `bundle.js` says HP rebuilt the
app, not what they fixed. Where a conclusion is drawn above — "the printer
roster is a data file and HP edited it" — it is because the changed file is
named for exactly that, and the printer counts in
`clickgraft/data/capabilities.json` agree independently.
