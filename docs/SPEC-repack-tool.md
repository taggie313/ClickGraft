# Spec: `hpclick-arm64` — scripted Apple Silicon repack of HP Click

## Goal

A tool that converts a user's **own installed** HP Click 4.8.117 into a native
arm64 build. It distributes a *recipe*, never HP's or Adobe's bytes.

## Non-negotiables

1. **Ship no third-party binaries.** No HP assets, no Adobe assets, no Qt, no
   Electron, no LGPL dylibs in the repo. Everything is fetched at run time from
   its upstream, or copied from the user's own machine.
2. **No signature-bypass logic, ever.** We modify `app.asar` while
   `app.asar.sign` exists, and that is fine only because HP does not verify it
   at runtime. If a check turns out to be enforced, the tool stops and reports —
   it must never grow code to defeat one.
3. **Reversible.** Back up anything overwritten; provide a working `restore`.
4. **Fail loud.** Every assumption is an assertion with a readable message.
   Silent partial success is the failure mode we are engineering against.

## Shape — keep it small

**One file, `repack.py`. Python 3.11+, standard library only.** No pip installs,
no TOML/YAML files, no package layout. Patch definitions live in a table inside
the script. `urllib`, `zipfile`, `hashlib`, `json`, `struct`, `plistlib`,
`subprocess`, `shutil`, `argparse` cover everything. Target ~600–800 lines.

Resist splitting into modules, adding a config file format, or wrapping
`codesign` in an abstraction. One readable script beats a small framework.

## CLI

```
repack.py preflight            # check environment, refuse early with specifics
repack.py build   [--out DIR]  # produce the arm64 bundle in a staging dir
repack.py verify  [--app PATH] # structural + signature + smoke-launch checks
repack.py install [--app PATH] # back up /Applications copy, swap new one in
repack.py restore              # undo install from the backup
```

Global: `--dry-run`, `--yes`, `--source "/Applications/HP Click.app"`.

`build` must be runnable without `install`. Never write to `/Applications`
except in `install`.

---

## Verified environment facts

Do not re-derive these; assert them.

| Fact | Value |
|---|---|
| Supported source version | **4.8.117** only |
| Electron | **exactly 39.8.4**, `darwin-arm64` |
| Stock `app.asar` SHA-256 | `47709539778938bca5b6128278b545b4f490609175a7e16cade84ccbd803bb21` |
| asar packed / unpacked entries | 19202 / **25** |
| Native modules | `Dj{Core,Conn}ServicesNative-Electron.node` — already `x86_64 arm64`, **never touch** |
| APPE + Adobe libs | already universal, **never touch** |
| Only Intel-only Mach-O left after repack | `AdobeAXE16SharedExpat.framework` — orphaned, nothing links it, leave it |
| Bundle id | `com.hp.hpclick`; helpers `com.hp.hpclick.helper` |

**Why the exact Electron version is mandatory:** the `.node` modules are built
against **Nan** (`Nan::FunctionCallbackInfo`, raw `v8::Local` in the exported
symbols), which is not ABI-stable across Electron majors. 39.8.x-latest is not
good enough. Verify the downloaded zip against Electron's published
`SHASUMS256.txt` for that release.

---

## asar handling — the core correctness risk

Implement this yourself; do not shell out to the `asar` npm tool. It silently
dropped all 25 `unpacked` flags in a previous attempt, which moved
`DjConnServices/resources/*` inside the archive where the native module — which
uses plain POSIX I/O — cannot read them. Nothing crashed at startup; it broke
printer-dependent paths only.

**Format:**

```
[0:4]   uint32 = 4
[4:8]   uint32 = headerSize + pad + 8
[8:12]  uint32 = headerSize + pad + 4
[12:16] uint32 = headerSize
[16:16+headerSize]        header JSON (compact, no spaces)
        pad = (4 - headerSize % 4) % 4    <-- easy to miss; miss it and every
                                              offset looks shifted
content blob follows; entry offsets are relative to 16+headerSize+pad
```

Header JSON round-trips exactly as `json.dumps(header, separators=(",", ":"))`.
Assert that before writing — if it doesn't round-trip, abort.

Entry shapes:
- packed: `{size, offset (string), integrity{algorithm,hash,blockSize,blocks}}`
- unpacked: `{size, unpacked: true, integrity{...}}` — **no `offset`**

**Rebuild, don't extract.** Build the new blob by walking packed entries in
offset order and concatenating each one's bytes — original slice, or patched
content — assigning new offsets as you go. Unpacked entries contribute nothing.
~40 lines, no temp files, structurally incapable of losing a flag.

**Post-conditions, asserted before the result is written anywhere:**

- packed count == 19202, unpacked count == 25
- every entry not in the patch table is **byte-identical** to the source
- every entry's recomputed SHA-256 matches its header `integrity.hash`
- the 25 unpacked files exist under `app.asar.unpacked/` and hash-match
- `max(offset+size)` == blob length (no gaps, no slack)

Then update `Info.plist` → `ElectronAsarIntegrity → Resources/app.asar → hash`
to `sha256(new header bytes)`. (Currently inert — the
`EnableEmbeddedAsarIntegrityValidation` fuse is Disabled — but keep it correct.)

## Patch table

Each entry asserts its `old` string occurs **exactly once**. Zero or two
occurrences is a hard error naming the file.

| # | asar path | change | why |
|---|---|---|---|
| 1 | `package.json` | `hp_configs.crashAutoSubmit` → `false` | Read as `packagejson.hp_configs.crashAutoSubmit`. A top-level key does nothing — that mistake shipped once already. Stops plaintext dump upload to `http://15.23.17.24:9100`. |
| 2 | `app/node/main/app-updater.js` | `function startup(e){` → `function startup(e){return;` | The real updater gate. Reached via `main.js → app-starter.js → require('./app-updater')` — verify that chain. Squirrel.Mac does **not** version-compare client-side, so bumping `version` does not stop it. |
| 3 | `app/shared/constants.js` | drop leading `export ` from `export var SharedConstants;`, append `if (typeof exports !== 'undefined') { exports.SharedConstants = SharedConstants; }` | Fixes a **pre-existing HP bug**, not repack fallout. |
| 4 | `app/shared/industries.js` | same treatment for `export const Industries = [` | Same bug. |

**Do not patch `app/shared/shared.module.js`.** It is dead code — the live copy
is webpacked into `bundle.js`. A previous run half-converted it (rewrote the
`export`, left a top-level `import`), which is pure noise in the diff.

**Background for #3/#4, so nobody "simplifies" them away:** `app/index.html`
loads `<script src="./shared/constants.js">` as a *classic script*, where
top-level `export` is a syntax error. The main process gets away with
`require()`ing the same file only because Node 22+ can require an ES module.
Converting to CommonJS satisfies both consumers. Confirmed empirically: the
stock build logs `Uncaught SyntaxError: Unexpected token 'export'` in the
renderer; the patched build logs zero.

`version` stays `4.8.117` by default. Offer `--fake-version` for the
`99.99.999` belt-and-braces, off by default — a version that disagrees with
`Info.plist` makes logs and crash metadata confusing for no gain once #2 lands.

---

## Build steps

1. **Preflight.** macOS on Apple Silicon; Xcode CLT (`codesign`,
   `install_name_tool`, `lipo`, `otool`); source app present; source `app.asar`
   hash matches; required dylibs available (below). Refuse with the exact
   remediation command.
2. **Fetch Electron** `electron-v39.8.4-darwin-arm64.zip`, verify against
   `SHASUMS256.txt`, cache it.
3. **Stage.** `ditto` the source app to a staging dir with a **non-`.app`
   name**. See "App Management" below — this is not cosmetic.
4. **Swap the runtime.** Replace `Contents/Frameworks/Electron Framework.framework`
   wholesale; replace the Mach-O inside each of the four `HP Click Helper*.app`
   bundles from the correspondingly-named stock helper, **keeping HP's helper
   bundle names, Info.plists and bundle ids**; replace `Contents/MacOS/HPClickExe`.
   Leave `Contents/Resources/` alone apart from the asar patches.
5. **Dylibs — exactly three, and only for arm64.** Copy `libidn2.0.dylib`,
   `libunistring.5.dylib`, `libintl.8.dylib` from the user's Homebrew into
   `Contents/Resources/app/appData/macx/lib` and rewrite their ids to
   `@rpath/...`. **Never vendor these** — LGPL. If missing:
   `brew install libidn2 gettext` (libidn2 pulls libunistring; gettext provides
   libintl).

   **Do not touch `libmagic.1.dylib`.** HP already ships it, already universal
   (`x86_64 arm64`). It is not our dependency — an earlier pass listed it as one.

   **Why these three are mandatory (verified, do not "optimise" away):**
   ```
   nm -arch arm64 -u DjCoreServicesNative-Electron.node | grep idn2
     _idn2_check_version  _idn2_free  _idn2_lookup_ul  _idn2_strerror
   nm -arch arm64 -m ... → "(undefined) external ... (dynamically looked up)"
   otool -arch arm64 -L ... → zero libidn2 load commands
   nm -arch x86_64 -u ... | grep idn2 → 0 matches
   ```
   HP's **arm64** slice links a curl built with IDN support and leaves the
   symbols to flat-namespace lookup, but the app ships no libidn2. The x86_64
   slice has no such symbols, which is why the stock Intel build works without
   the library and why HP has evidently never run their own arm64 binaries.
   Flat lookup resolves at *call* time, so the app launches fine and only dies
   on code paths doing IDN work — do not conclude from a clean launch that the
   library is unnecessary. `libunistring` and `libintl` are libidn2's own
   dependencies.
6. **Patch the asar** per the table, with all post-conditions.
7. **Launcher.** Write `Contents/MacOS/HP Click` with `DYLD_FRAMEWORK_PATH` and
   `DYLD_LIBRARY_PATH` pointing **only** inside the bundle. No `/opt/homebrew`,
   no `/usr/local` — those leaked into a previous build and made it depend on a
   Homebrew tree it already ships copies of. Keep `DYLD_INSERT_LIBRARIES` for
   `libunistring`+`libidn2` behind `--preload-idn2` (**default on**) with a
   comment recording that libcurl resolves `idn2_check_version` dynamically.
   Note the launcher ends `exec "$DIR"/HPClickExe` with no `"$@"`, matching HP —
   so args never reach the app, which matters for testing.
8. **Sign**, inner to outer, no `--deep`:
   - every framework and dylib
   - the whole `APPE/JDFPrintProcessor/` tree **recursively** — without this,
     dyld rejects `libMarker.dylib` for Team ID mismatch against ad-hoc hosts
     and JDFPrintProcessor dies at launch with `DYLD Library missing`
   - the four helper apps with `--options runtime` and entitlements including
     **`com.apple.security.cs.disable-library-validation`** (plus `allow-jit`,
     `allow-unsigned-executable-memory`, `allow-dyld-environment-variables`)
   - the outer bundle last
   Then `xattr -dr com.apple.quarantine`.
9. **Rename staging dir to `HP Click.app`** as the final action.

## `verify`

- `lipo -archs` on `Electron Framework`, `HPClickExe`, all four helpers → `arm64`
- both `.node` modules still report `x86_64 arm64` and are byte-identical to source
- full-bundle scan: report every remaining x86_64-only Mach-O (expect exactly
  `AdobeAXE16SharedExpat`), and every `/opt/homebrew` or `/usr/local` reference
  in `otool -L` **and in the launcher script** — a previous audit scanned only
  Mach-O files and reported "0 Homebrew dependencies (Clean)" while the launcher
  had two
- asar post-conditions re-checked on the built artifact
- `codesign --verify` clean; `ElectronAsarIntegrity` matches the header
- **smoke launch**: exec `Contents/MacOS/HPClickExe` directly, wait for
  `/tmp/HP/HP Click/logs/HP Click.log` to reach quiescence, assert
  `DJRIP_DJCS: successful initialization = "1"`, assert **zero**
  `SyntaxError` in the main log, then terminate. Report time-to-quiescence.

Exit non-zero on any failure. Print a one-line-per-check summary.

---

## macOS behaviours that will bite the implementer

- **App Management.** The instant a directory is named `*.app`, macOS stamps
  `com.apple.macl`/`com.apple.provenance` and blocks *all* writes inside it —
  including `codesign`'s own, which fails `Operation not permitted` and leaves a
  **broken seal**, after which Gatekeeper reports the app as "damaged". Do all
  work under a non-`.app` name and rename last. To *run* a build for testing you
  never need the extension — exec `Contents/MacOS/HPClickExe` directly, which
  also bypasses LaunchServices and Gatekeeper entirely.
- **Shared single-instance lock.** Any HP Click process alive — from *either*
  build, they share `com.hp.hpclick` — makes the next launch hit
  `requestSingleInstanceLock() == false` and `process.exit(0)` **silently**: no
  error, no log, zero CPU. Before any launch, sweep `HPClickExe`,
  `HP Click Helper`, `JDFPrintProcessor`, `chrome_crashpad_handler`, then wait
  for the lock to release.
- **The app breaks its own signature at runtime.** `JDFPrintProcessor` writes
  `AdobeFnt16.lst` font caches *into its own bundle*. Harmless while
  LaunchServices has already assessed the bundle — but **renaming a bundle
  resets that assessment**, after which it refuses to launch. `install` must
  therefore re-sign after any rename, and `restore` must not simply rename the
  backup back into place without re-signing.

## Acceptance tests

1. `preflight` on a machine missing Xcode CLT / wrong app version / missing brew
   formulae → distinct, actionable message each time.
2. `build` twice → identical output (modulo signatures).
3. Corrupt one asar patch's `old` string → hard error naming the file, no output
   written.
4. Built artifact: `verify` passes every check.
5. Diff built asar header against source: **only** the 4 patched entries plus
   their integrity hashes differ; packed/unpacked counts unchanged.
6. `install` then `restore` → source app back to its original hash.
7. Launch the result and reach log quiescence with no `SyntaxError` and no new
   crash reports in `~/Library/Logs/DiagnosticReports`.

## Explicitly out of scope

Universal/fat output (`@electron/universal` needs both slices; we have one),
supporting any version but 4.8.117, touching `app.asar.sign` or
`printersValidate.sign`, and anything that would let this be redistributed with
HP's payload included.

## Resolved since first draft

- **Which dylibs are needed:** three, not four. `libidn2` + its deps
  `libunistring` and `libintl`, from Homebrew. `libmagic` is HP's own and
  already universal — leave it alone.
- **Is `DYLD_INSERT_LIBRARIES` required:** yes. The arm64 `.node` slices carry
  flat-namespace `idn2_*` undefined symbols with no load command, so nothing
  pulls libidn2 into the process on its own. Preloading is the only clean fix —
  there is no supported way to add an `LC_LOAD_DYLIB` to HP's binary after the
  fact, and we want their modules left byte-identical. Preloading `libidn2`
  alone should be sufficient (libunistring/libintl load as its dependencies via
  `@rpath`); the current launcher preloads libunistring too, which is harmless
  belt-and-braces. Worth trimming to one entry and confirming.

## Still unresolved — flag, don't guess

- Whether any *other* arm64 slice in the bundle carries flat-namespace undefined
  symbols that the x86_64 slice lacks. The idn2 case was found by accident; the
  same latent breakage could exist elsewhere. `verify` should scan every
  universal Mach-O for symbols undefined in the arm64 slice but not the x86_64
  one, and report them. This is cheap and catches the whole bug class.
