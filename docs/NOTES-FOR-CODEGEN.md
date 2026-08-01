# Notes for the codegen agent — HP Click arm64 repack

Written after auditing the repack against the original brief and the shipped
build. The core work was **correct**: stock arm64 Electron 39.8.4 swapped in,
universal `.node` modules reused untouched, APPE recursively re-signed to fix a
real `libMarker.dylib` Team ID rejection, updater neutralised. Measured result
is ~11× faster startup and ~14× less CPU than the Intel build under Rosetta.

What follows are the defects found afterwards and the habits that would have
caught them. They generalise past this project.

---

## 1. Verify the key you write is the key that gets read

`package.json` was given a top-level `"crashAutoSubmit": false` and
`"updater_config": {}`. The code reads:

```js
uploadToServer: packagejson.hp_configs.crashAutoSubmit
let updateconfig = packagejson.hp_configs.updater_config
```

Both overrides sat at the wrong nesting level and did nothing. Crash dumps kept
auto-uploading to `http://15.23.17.24:9100` in plaintext, and the handoff report
claimed the opposite.

**Habit:** before changing config, grep for the key's *read site* and match the
exact access path. Then re-read the value the way the app does and assert it.

## 2. Prove a change took effect at runtime, not just on disk

The updater bypass (`startup()` early return) was real and works — but the
report credited the version bump to `99.99.999`, which is not what stops it
(Squirrel.Mac doesn't version-compare client-side; it installs whatever the feed
offers). Right outcome, wrong stated mechanism, which means the next person
"simplifies" away the part that actually mattered.

**Habit:** trace the call chain (`main.js → app-starter.js → require('./app-updater')`)
and confirm the edited file is on it. Say which mechanism does the work.

## 3. When repacking an asar, diff the header — not just the file list

The repack silently dropped **all 25 `unpacked` flags** (19202 packed → 19227).
Files still existed under `app.asar.unpacked/`, but Electron then served them
from inside the archive. That breaks any consumer needing a real filesystem
path — here, `DjConnServices` reads `pageSizes.json`, `config.json`,
`printersValidateCRLF.json` with plain POSIX I/O, which cannot see inside an
archive. Nothing crashed at startup, so it passed a liveness check while being
broken on printer-dependent paths.

**Habit:** after any archive rewrite, compare old vs new headers on:
packed count, unpacked count, per-file integrity, total size. Assert equality
on everything you didn't intend to change.

Related: `ElectronAsarIntegrity` in `Info.plist` went stale for the same reason.
It happened to be inert (`EnableEmbeddedAsarIntegrityValidation` is Disabled in
stock Electron), but "inert today" is not "correct".

## 4. Scope audit scripts to the real surface, then report the scope

`audit_app.py` walked Mach-O binaries and grepped `otool -L`, then reported
**"Homebrew / /usr/local Hardcoded Dependencies: 0 (Clean)"**. Meanwhile the
launcher — a shell script, not a Mach-O, so never scanned — contained:

```bash
export DYLD_LIBRARY_PATH=".../lib:/opt/homebrew/lib:/usr/local/lib"
```

The finding wasn't wrong, it was *narrower than its headline*. A clean bill of
health on a partial scan is worse than no scan, because it stops people looking.

**Habit:** state what the check covered and what it structurally cannot cover.

## 5. Separate live code from dead code before editing

- `app/shared/constants.js` — **live**: loaded by `index.html` as a classic
  `<script src>` *and* `require()`d by 11 main-process files.
- `app/shared/shared.module.js` — **dead**: the live copy is webpacked into
  `bundle.js`. It was edited anyway, and only half-converted (the `export` was
  rewritten, a top-level `import` was left), which reads like an oversight.
- `app/machan.js` — **dead**: 242 concatenated module bodies with colliding
  `const` declarations; it cannot load as a single CommonJS module.

**Habit:** establish the load path (`package.json main` → requires; `index.html`
→ script tags; webpack bundle contents) before touching a file. Don't edit what
nothing loads, and never leave a file half-converted.

## 6. Say when a bug you fixed was pre-existing

The `Uncaught SyntaxError: Unexpected token 'export'` fix was correct and
necessary. It was also **HP's bug, present on Intel too** — architecture has no
bearing on JS parsing, and the stock build logs the error while the repacked one
logs zero. That's a genuine find worth surfacing, not repack fallout to patch
quietly.

**Habit:** ask "would this have failed before my change?" If yes, report it as a
discovered defect with the evidence.

## 7. Keep environment hacks minimal, justified, and reversible

`DYLD_INSERT_LIBRARIES` force-preloads two dylibs into **every** child process
the app spawns, including helpers and `JDFPrintProcessor`. That may well be
necessary (libcurl resolves `idn2_check_version` dynamically), but the report
didn't say what proved it necessary or what would prove it unnecessary. The
`/opt/homebrew` path additions were pure leftover and made the build depend on a
Homebrew tree it ships its own copies of.

**Habit:** for each env hack, record the symptom it fixes and the test that
would retire it.

## 8. Don't present liveness as verification

The report's table read `PASSED` on: process is arm64, JDFPrintProcessor
spawned, DJCS initialised, zero crash dumps. All true, none of it functional.
Nothing exercised `CalculateNest`, `createAutoRotations`, roll selection, or a
print job — and **nothing measured speed**, though "the app is unbearably slow"
was the entire reason for the project. A build that launched natively and stayed
janky would have produced an identical report.

Also: three `JDFPrintProcessor` crash dumps sat in `~/Library/Logs/DiagnosticReports`
from mid-session while the report said "0 dumps". They predated the fix and were
fine — but the claim was checked against the wrong source.

**Habit:** split the report into *verified*, *unverified*, and *cannot verify
without X*. Leaving "we never measured the thing we set out to fix" visible is
more useful than a full green table.

## 9. Leave a reversible trail

There was no backup of the pre-edit `app.asar`, and no script — only prose
describing what had been done. Reproducing or undoing it meant re-deriving
everything. Ship the script that made the change, and back up what you overwrite.

---

## macOS specifics worth knowing next time

- **App Management protection.** The moment a directory is named `.app`, macOS
  stamps `com.apple.macl`/`com.apple.provenance` and blocks *all* writes inside
  it — including `codesign`'s own writes, which fail with `Operation not
  permitted` and leave a broken seal. Patch and sign while the directory has a
  non-`.app` name, rename last. To *run* a build for testing you don't need the
  `.app` extension at all — exec `Contents/MacOS/<exe>` directly, which also
  bypasses Gatekeeper/LaunchServices entirely.
- **asar header padding.** The header is padded to a 4-byte boundary; content
  base is `16 + headerSize + ((4 - headerSize % 4) % 4)`. Miss it and every
  offset appears shifted and every integrity check appears to fail.
- **Byte-neutral patching.** To edit a file inside an asar without repacking,
  keep the byte length identical (pad JSON with trailing whitespace; balance a
  JS insert against an equal-length trim of a log-string literal) and refresh
  that entry's integrity hash. No offsets move, and the `unpacked` flags can't
  get lost because the header is otherwise untouched.
- **Runtime-written files break code signatures.** `JDFPrintProcessor` writes
  `AdobeFnt16.lst` font caches *into its own bundle*, invalidating HP's Developer
  ID seal. Harmless while LaunchServices has already assessed the bundle —
  **renaming the bundle resets that**, and it then refuses to launch as
  "damaged". Renaming a shipped app is not a free operation.
- **Shared single-instance lock.** Two builds with the same bundle id
  (`com.hp.hpclick`) share Electron's `requestSingleInstanceLock()`. A survivor
  from either makes the next launch `process.exit(0)` *silently* — no error, no
  log, zero CPU. Sweep every related process before launching, not just the one
  bundle's.
- **Launchers drop arguments.** `Contents/MacOS/HP Click` ends with
  `exec "$DIR"/HPClickExe` — no `"$@"`. Command-line args never reach the app;
  pass them by invoking `HPClickExe` directly with the env vars replicated.

## Benchmarking, if asked to prove a speedup

- Don't stop an A/B when CPU goes flat. That measures "this process paused", and
  on a slow build a mid-startup pause is indistinguishable from being finished —
  it silently compares different amounts of work.
- Anchor to a semantic milestone the app itself emits, or to quiescence of its
  log. HP Click writes a timestamped renderer log to
  `$TMPDIR/HP/HP Click/logs/HP Click.log`; use its own millisecond stamps rather
  than externally sampled timings.
- Control the confounds: the stock build downloads a ~200 MB update on launch,
  which will dominate any CPU comparison if left enabled.
- Report run-to-run range, not just a median. The Intel build ranged 54–109 s
  across three runs; that variance *is* the user-visible symptom.
