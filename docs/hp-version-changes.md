# What HP changed, and how it was checked

Two removals in HP Click that people ask about. Measured 9 September 2026 by
diffing HP's own packages, not read from a changelog — HP published neither
change.

**This goes stale the moment HP ships past 4.10.42.** Every "last version" claim
below is bounded by the four builds that could be obtained: 4.8.117, 4.8.118,
4.10.38, 4.10.42. Re-run the diff before repeating any of it.

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

Gone: T310 24-in, T320 24-in, T350 24-in, T720 24-in and 36-in, T750 24-in and
36-in. Five models, seven entries. Nothing added at any boundary.

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
(`hoops1610.dll`), Autodesk PDK 2.3.0.11.mt, plus Qt5 and `dwfApp.exe`. Newest
copyright anywhere in it: 2009.

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
