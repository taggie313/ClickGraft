# Design v2 — `clickgraft`, an open-source Apple Silicon patcher for HP Click

Supersedes `SPEC-repack-tool.md`. That spec's *invariants* still hold verbatim —
asar handling, patch anchoring, signing order, the macOS gotchas. What changes
is the shape: multi-file package, GUI wizard, multi-version support, and the
original app is never touched.

---

## 1. What changed and why

| v1 | v2 |
|---|---|
| single `repack.py` | Python package + GUI + manifests |
| patches `/Applications/HP Click.app` in place, with backup/restore | **copies** to `HP Click (Apple Silicon).app`; original untouched |
| pinned to 4.8.117 | version manifests + a probe that drafts new ones |
| shell only | tkinter wizard, shell still available |
| Homebrew required | Homebrew optional |

**Copy instead of patch** deletes an entire class of risk. There is no
`install`, no `restore`, no backup naming, and no way to destroy the original —
it *is* the fallback. The v1 `install` had a real data-loss hazard (labelling an
already-patched build as the "x86_64 backup"); that bug cannot exist here.

---

## 2. Dependencies — exactly one

**Xcode Command Line Tools.** That's the whole list. It provides:

- `python3` — **3.9.6**, with a working `tkinter` (Tk 8.5). Verified on a stock
  install. **Target Python 3.9**: no `match`, no PEP 604 `X | Y` annotations at
  runtime, no `tomllib`.
- `codesign` (unavoidable — arm64 requires valid signatures), `install_name_tool`,
  `lipo`, `otool`, `nm`.

Everything else is fetched at run time and hash-verified:

- **Electron runtime** — `electron-vX-darwin-arm64.zip` from GitHub releases,
  checked against that release's `SHASUMS256.txt`. Version must match the source
  app **exactly** (the `.node` modules are Nan-built, not N-API, so the ABI is
  not stable across majors).
- **LGPL dylibs** — no Homebrew install required. `https://formulae.brew.sh/api/formula/<name>.json`
  returns per-macOS-version arm64 bottle URLs and SHA-256s; bottles come from
  ghcr.io with the anonymous bearer token Homebrew itself uses. Verify the
  SHA-256, extract, take the dylib. If Homebrew *is* installed, prefer the local
  copy and skip the download. We never host these — the user's machine fetches
  them from Homebrew's own CDN, exactly as `brew` would.

**Missing-dependency UX:** the wizard detects a missing CLT and offers a button
that runs `xcode-select --install`, which brings up Apple's own GUI installer,
then polls until it completes. No terminal, no copy-pasted commands.

---

## 3. Output

`HP Click (Apple Silicon).app`, written next to the source (default
`/Applications`, user-selectable). The original is opened read-only. Nothing is
ever written inside the source bundle.

**Two known consequences to surface in the UI, not bury:**

1. **They cannot run at the same time.** Both bundles share Electron's userData
   (`~/Library/Application Support/hpclick`, derived from `package.json` name),
   so they share `requestSingleInstanceLock()`. Launching one while the other
   runs makes the second `process.exit(0)` **silently** — no error, no window.
   Keep the shared userData (the user keeps their printers and settings) and
   *tell them*: the wizard's final screen must say this plainly, and the README
   must repeat it. This is the single most likely support question.
2. **Bundle identifier.** Two bundles claiming `com.hp.hpclick` confuses
   LaunchServices about which owns file associations. Give the copy
   `com.hp.hpclick.arm64` and its helpers `com.hp.hpclick.arm64.helper`.
   **To verify before shipping:** Electron locates helpers by path, not id, so
   this should be safe — but confirm renderer processes still spawn, because a
   helper-id mismatch shows up as a blank window rather than an error.

---

## 4. Multi-version support

### Manifests

`manifests/<app-version>.json`, one per supported release:

```json
{
  "app_version": "4.8.117",
  "asar_sha256": "4770953977…",
  "electron_version": "39.8.4",
  "asar_entries": { "packed": 19202, "unpacked": 25 },
  "patches": [ { "path": "...", "anchor": "...", "replacement": "...", "why": "..." } ],
  "required_dylibs": ["libidn2.0.dylib", "libunistring.5.dylib", "libintl.8.dylib"],
  "expected_x86_only": ["…/AdobeAXE16SharedExpat.framework/…"],
  "verified_by": "…", "verified_on": "…"
}
```

Refuse to build for an unmanifested version unless `--experimental`, and make
the GUI say so in plain language rather than failing obscurely.

### `probe` — how a new version gets supported

Given any HP Click bundle, without a manifest:

1. Read app version from the asar's `package.json`.
2. Read the Electron version from `Electron Framework.framework`'s Info.plist —
   **never hardcode it**; the arm64 runtime must match the source exactly.
3. Count asar packed/unpacked entries.
4. Check every patch anchor: present? **exactly once?** Report each individually.
5. `lipo` every Mach-O; flag anything x86_64-only that isn't the Electron runtime
   (i.e. anything HP would have to rebuild).
6. **Scan every universal Mach-O for symbols undefined in the arm64 slice but
   not in the x86_64 slice.** This is how the libidn2 requirement was found, and
   it generalises: it is the check that catches "HP's arm64 slice needs
   something they never shipped." Map symbol prefixes to libraries
   (`idn2_` → libidn2) to derive `required_dylibs` instead of hardcoding them.
7. Emit a draft manifest plus a human-readable report.

The GUI exposes this: unknown version → "not yet supported" → **Generate report**
→ report saved and copied to clipboard, with a link to open a GitHub issue. That
is the contribution path that keeps the project current.

---

## 5. Package layout

```
clickgraft/
  clickgraft/
    asar.py       format, rebuild, post-conditions   <- the correctness core
    macho.py      lipo/otool/nm wrappers; arch + undefined-symbol scans
    manifest.py   schema, load, validate
    probe.py      unknown-version analysis -> draft manifest
    patches.py    patch engine (exactly-once anchors, uniformly enforced)
    deps.py       CLT detect/install, bottle fetch, Electron fetch
    signing.py    codesign orchestration
    build.py      pipeline
    verify.py     post-build checks
    report.py     human + JSON output
    cli.py
    gui/wizard.py
  manifests/4.8.117.json
  tests/
  ClickGraft.command      double-clickable entry point
  README.md  LICENSE  CONTRIBUTING.md
```

`asar.py` keeps every post-condition from v1 — packed/unpacked counts,
byte-identity of unpatched entries, per-entry integrity, zero blob slack,
unpacked files present on disk and hash-matching. Those are non-negotiable; they
are what make a rebuild safe.

---

## 6. GUI — transparent by construction

tkinter wizard. Every screen has a **Show technical detail** disclosure, and the
full log is written to a file the user can open from the final screen.

1. **Welcome** — what this does, what it does not do, that it never modifies the
   original, that the result is unsigned and for personal use.
2. **Requirements** — CLT present? green tick or an **Install** button that runs
   Apple's installer. Disk space. Shows exactly what it checked.
3. **Choose app** — auto-scan `/Applications` for HP Click; show version,
   architecture, and asar hash for each candidate.
4. **Compatibility** — supported version, or "unsupported" with a
   **Generate report** button. Shows which manifest matched and why.
5. **Review plan** — the transparency screen. Lists, in plain English:
   *the exact patches* and what each one fixes; *every file to be downloaded*
   with its URL and expected SHA-256; *every dylib to be added* and which
   undefined symbol requires it; *where the output goes*. Nothing happens until
   this is accepted.
6. **Build** — live log, per-step progress, cancellable.
7. **Verify** — pass/fail table, one row per check.
8. **Done** — Reveal in Finder; **the two apps cannot run simultaneously**
   warning; how to undo (drag the copy to the Trash — the original was never
   touched).

Tk 8.5 looks dated. Acceptable for a utility wizard; the alternative (a local
web UI) trades a nicer look for opening a browser and is not obviously better.

---

## 7. Carried-over defects to fix in the rewrite

These were found auditing the v1 script. 1–4 are already fixed there; 5–8 are
open and must not be reintroduced:

5. **`verify` never re-checks `ElectronAsarIntegrity`.** A bundle whose
   `Info.plist` hash disagrees with its asar header passed verification. Add it
   as an explicit check.
6. **`--preload-idn2` could not be turned off** — `store_true` with
   `default=True` and no `--no-` variant. In v2 the preload set is *derived* by
   the symbol scan; expose a proper `--preload/--no-preload` pair.
7. **`patch_package_json` did not assert exactly-once** while the other three
   patches did, and its fallback branch silently reformatted the whole file.
   The patch engine must enforce the anchor contract uniformly, with no
   per-patch bespoke fallbacks.
8. **Startup-failure detection was order-dependent.** The smoke test `break`s on
   the init marker before evaluating the SyntaxError condition in that same
   iteration, so it only caught the error because it happened to be logged
   first. Scan a fixed window *after* init, and match a set of failure
   signatures (`SyntaxError`, `Error:`, `Library not loaded`, `Symbol not
   found`, crash-report appearance), not a single string.

---

## 8. Distribution reality — decide before launch

An unsigned executable downloaded from GitHub is quarantined; macOS will say it
"cannot be opened" and offer only Move to Trash. Two options:

- **(a) Ship `ClickGraft.command`** and document right-click → Open once. Free,
  one extra step, honest.
- **(b) Developer ID + notarization** ($99/yr). Proper double-click experience.

Recommend **(a)** to start, with (b) if the project gets traction. Whichever is
chosen, the README must show the exact first-run steps with a screenshot — a
"damaged, move to Trash" dialog on first launch will otherwise lose most users.

---

## 9. Handoff — what to give codegen, and what NOT to rewrite

Hand over all five of these together:

| File | Role |
|---|---|
| `DESIGN-v2.md` | this document — the target shape |
| `SPEC-repack-tool.md` | the invariants; still authoritative for asar format, signing order, macOS gotchas |
| `repack.py` | **working, verified reference implementation** |
| `manifests/4.8.117.json` | the schema, by worked example |
| `NOTES-FOR-CODEGEN.md` | habits that prevent the defects found in v1 |

**Port these from `repack.py` rather than reimplementing** — they are audited and
proven correct against a real build (exactly 4 asar entries changed, 19198
unpatched entries byte-identical, 0 slack, produced helper binaries hash-identical
to a known-good build):

- `AsarArchive` — header parse, the `(4 - headerSize % 4) % 4` padding, content base
- `patch_and_repack_asar` — the rebuild-without-extracting walk
- the post-condition block in full: entry counts, path-set equality, unpacked-flag
  stability, unpacked files present on disk and hash-matching, per-entry integrity,
  byte-identity of unpatched entries, zero blob slack
- the signing order and the APPE recursive-signing pass
- `assert_stock_source`'s fail-loud shape (generalised to manifest lookup)

**Rewrite these:**

- patches: from Python functions to the manifest `ops` schema. Op types are
  `replace` (assert anchor occurs **exactly once** — uniformly, no per-patch
  bespoke fallbacks), `append`, and `json_set` (dotted path). `json_set` exists
  specifically because string-matching JSON is fragile and because the
  wrong-nesting bug it replaces shipped once already.
- version handling: constants → manifest lookup + `probe`.
- install/restore: **delete entirely**. Copy-to-new-bundle replaces them.
- `--preload-idn2`: derive the preload set from the manifest's `required_dylibs[].preload`,
  and expose a real `--preload/--no-preload` pair.

**Acceptance tests** (the v1 set, plus v2 additions):

1. Missing CLT / unknown version / unreadable source → distinct actionable message each.
2. Build twice → identical output modulo signatures.
3. Corrupt any patch anchor → hard error naming the file, nothing written.
4. Built artifact passes `verify`, every check.
5. Built asar differs from source in **exactly** the manifest's patched entries;
   packed/unpacked counts unchanged; zero slack.
6. **Source bundle is byte-identical before and after a full run** (hash it) —
   this is the v2 headline guarantee and deserves its own test.
7. Deliberately break the output's signature → `verify` **fails**. (v1 printed
   "clean" unconditionally here; this test exists to keep that from returning.)
8. Swap the stock asar into a built bundle → `verify` fails on the SyntaxError.
9. `probe` against 4.8.117 with its manifest removed reproduces that manifest's
   `electron_version`, entry counts, `required_dylibs` and `expected_x86_only`.

## 10. Project boundaries — restate in the README

- Ships **no HP or Adobe bytes**. Operates on the user's own lawfully installed
  copy, produces output only on that machine.
- Not affiliated with or endorsed by HP. "HP Click" is used only to name what
  the tool operates on.
- LGPL dylibs are fetched from Homebrew's CDN at run time, never redistributed.
- **Never add signature-bypass logic.** We modify `app.asar` while
  `app.asar.sign` exists only because HP does not verify it at runtime. If a
  version turns out to enforce a check, the tool stops and reports — it does not
  learn to defeat it. This is the line that keeps this interoperability work.
- Users should not redistribute the patched output.
