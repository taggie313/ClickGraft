# ClickGraft

**Make HP Click run natively on Apple Silicon.**

HP ships HP Click for macOS as an Intel-only build, so on an M-series Mac it
runs under Rosetta — and it is *painfully* slow. ClickGraft grafts the official
Apple Silicon Electron runtime onto a **copy** of your installation. Your
original app is never touched.

> Not affiliated with, endorsed by, or supported by HP Inc. See [NOTICE](NOTICE).

---

## Is it actually faster?

Measured on an M-series Mac, stock 4.8.117 under Rosetta vs. the ClickGraft
build. Median of 3 runs each, both with the updater disabled so neither could
pull a background download.

| | Rosetta | ClickGraft | |
|---|---:|---:|---|
| Time to finish starting up | 57.1 s | **5.0 s** | **11× faster** |
| CPU burned getting there | 79.2 s | **5.6 s** | **14× less** |
| Main-thread stalls, idle | 17 | **0** | |
| Total blocking time, idle | 9.0 s | **0 ms** | |
| Input latency, 95th percentile | 298 ms | **2.5 ms** | |

The last three rows are the "I clicked and nothing happened" problem. Under
Rosetta the app isn't uniformly slow — it's *periodically dead*, freezing its
own main thread seventeen separate times while sitting idle. Run-to-run startup
ranged 54–109 s; ClickGraft's ranged 4.9–5.3 s.

**Why this works at all:** HP already compiles the hard parts for arm64.
`DjCoreServicesNative-Electron.node`, `DjConnServicesNative-Electron.node`, the
entire Adobe PDF Print Engine, and the Qt libraries all ship as universal
binaries containing arm64 code today. The only Intel-only component is the
Electron runtime — a file HP downloads rather than compiles. ClickGraft swaps
that one piece.

---

## Requirements

**Xcode Command Line Tools.** That is the entire list.

It provides the `python3` ClickGraft runs on, plus `codesign`,
`install_name_tool`, `lipo`, `otool`, and `nm`. No Homebrew. No compiler. No
`pip install`. If it's missing, the wizard offers a button that runs Apple's own
installer.

You also need your own **legally installed HP Click 4.8.117**. ClickGraft ships
no HP software and cannot obtain it for you.

---

## Install and first run

1. Download `ClickGraft.zip` from the latest release.
2. Unzip it and drag **ClickGraft.app** wherever you like.
3. Double-click it.

That's it. ClickGraft is signed with an Apple Developer ID and notarized by
Apple, so it opens normally — no right-click, no security warning, no
"unidentified developer" dialog.

The wizard walks you through eight screens: welcome, requirements, choose your
app, compatibility check, **review plan**, build, verify, done. Nothing is
written until you accept the plan screen, which lists every patch, every file to
be downloaded with its SHA-256, and every library to be added.

> **Running from source instead?** `python3 -m clickgraft.cli gui` from a clone
> works identically. Use Apple's `/usr/bin/python3` — a Homebrew or pyenv
> Python often lacks `tkinter` and will fail with an `ImportError`.

---

## What you end up with

`HP Click (Apple Silicon).app`, next to your original.

> ### ⚠️ The two apps cannot run at the same time
> Both share Electron's single-instance lock via shared settings. Launching one
> while the other is running makes the second exit **silently** — no window, no
> error message, nothing. It looks like the app failed to start.
>
> Quit one before opening the other. Sharing settings is deliberate: your
> printers and preferences carry over.

**To uninstall:** drag `HP Click (Apple Silicon).app` to the Trash. Your original
was never modified, so there is nothing else to undo.

---

## Command line

The wizard is the supported path, but everything is available headless:

```bash
python3 -m clickgraft.cli preflight                      # check the environment
python3 -m clickgraft.cli build   --source "/Applications/HP Click.app"
python3 -m clickgraft.cli verify  --app    "/Applications/HP Click (Apple Silicon).app"
python3 -m clickgraft.cli probe   --app    "/Applications/HP Click.app"
python3 -m clickgraft.cli gui                            # launch the wizard
```

---

## Supported versions

| HP Click | Electron | Status |
|---|---|---|
| 4.8.117 | 39.8.4 | Supported |

ClickGraft refuses anything else rather than guessing. Patches are anchored to
exact strings inside minified files, and the Electron version must match the
source **exactly** — the native modules are built against Nan, whose ABI is not
stable across Electron majors.

**To get a new version supported**, run `probe`. It reads the app and Electron
versions from the bundle, counts archive entries, checks every patch anchor,
scans for architecture gaps, and writes a report plus a draft manifest. The
wizard offers the same thing as a button when it meets an unknown version.
Open an issue with that report attached and the version can be added.

---

## How it works

1. Copy your app to a staging directory (`ditto`, so it's a fast APFS clone).
2. Download `electron-vX-darwin-arm64.zip` from the official Electron releases
   and verify it against that release's published `SHASUMS256.txt`.
3. Replace the Electron framework, the four helper executables, and the main
   binary. HP's `Info.plist` files, icons, localizations, and native modules are
   left alone.
4. Add the arm64 libraries HP's own arm64 slices need but never shipped —
   see below. Fetched from Homebrew's CDN and SHA-256 verified.
5. Apply four archive patches from the version manifest, each asserting its
   anchor appears **exactly once**.
6. Give the copy its own bundle identifier so macOS keeps the two apps distinct.
7. Ad-hoc sign inner-to-outer, then rename into place.

The archive rebuild asserts, before anything is written: entry counts unchanged,
every unpatched entry byte-identical to the source, every integrity hash
correct, zero unused space, and all unpacked files present on disk and matching.
A rebuild that quietly drops metadata is the single most dangerous failure mode
here, so it is checked rather than assumed.

### The bug HP hasn't noticed

The arm64 slices of HP's own native modules reference `idn2_*` and `nghttp2_*`
symbols with no library to resolve them — HP links a curl built with IDN and
HTTP/2 support and ships neither library. The x86_64 slices have no such
references, which is why the Intel build has never shown it. These resolve at
*call* time, so the app starts fine and would abort later on the affected code
path. ClickGraft bundles and preloads those libraries. Strong evidence HP's
arm64 binaries have never actually been run.

ClickGraft also fixes a genuine HP bug that exists on Intel too: three shared
files ship as ES modules while `index.html` loads one as a classic script, so
the stock app throws `Uncaught SyntaxError: Unexpected token 'export'` in its
renderer on every launch. The patched build logs zero.

---

## Known limitations

- **The build ClickGraft produces is ad-hoc signed**, not Developer ID signed —
  deliberately. Attaching a developer identity to a modified copy of someone
  else's application would misrepresent its origin. The build stays on your
  machine, where ad-hoc signing is both sufficient and more honest.
- **Printing is not yet independently verified.** Everything here is structural
  and startup behaviour. Nesting, auto-rotation, roll selection, and end-to-end
  job submission still need testing against real hardware.
- **One unresolved symbol.** `_png_init_filter_functions_neon` comes from a
  libpng compiled into HP's native module and cannot be supplied externally. It
  is recorded as an accepted, unverified risk in the manifest.
- **One orphaned Intel binary remains** (`AdobeAXE16SharedExpat`). Nothing links
  it and it is never loaded; the live path uses a universal build.
- **A path caveat for scripts:** the output bundle contains parentheses, so
  `pgrep -f "HP Click (Apple Silicon)"` treats them as a regex group and
  silently fails to match. Escape them or match on `HPClickExe` instead.

---

## Contributing

Run the tests before opening a PR — they need a stock HP Click present and take
a few minutes, because several perform real builds:

```bash
python3 -m pytest tests/ -q
```

The most valuable contributions are **manifests for new versions** (run `probe`
and attach the report) and **real-hardware print testing**, which is the biggest
gap in what has been verified.

---

## Legal

ClickGraft distributes no HP or Adobe software. It operates on a copy you
already installed, on your own machine, and produces output only there. Do not
redistribute a ClickGraft-produced bundle — it contains proprietary code this
project has no right to convey.

ClickGraft will never add logic to defeat a protection measure that is actually
enforced. If a future version of HP Click starts verifying its payload,
ClickGraft will stop and report it. Full terms in [NOTICE](NOTICE).

Licensed under the [MIT License](LICENSE).
