# clickgraft — defect report, round 2

Findings from auditing the generated `clickgraft/` package. The artifact was
verified independently before the code was read; a real build was produced from
stock 4.8.117 and every acceptance test in `DESIGN-v2.md` §9 was executed.

---

## First: what already works — do not regress it

These were verified against a real build. Changes must keep all of them true.

| Behaviour | Evidence |
|---|---|
| Source bundle untouched by a full build | recursive file-hash fingerprint identical before and after |
| asar delta is exactly the manifest's patched entries | 4 changed, 19202 packed / 25 unpacked, 0 integrity mismatches, 0 blob slack |
| Broken output signature fails verification | added a stray file to `Contents/Resources`; `verify` exited 1 |
| Probe re-derives a known manifest | version, Electron 39.8.4, asar SHA-256, entry counts, all anchors exactly-once, the one expected x86_64 binary |
| Separate bundle identity | `com.hp.hpclick.arm64` / `.arm64.helper`, renderers still spawn (smoke launch passes) |
| Old defects 5–8 fixed | ElectronAsarIntegrity checked; `--no-preload` exists; `replace` enforces exactly-once uniformly; failure-signature scan covers the full window |

`asar.py`'s rebuild is correct. Do not restructure it — fix only what is listed
below.

---

## D1 — `json_set` invents missing parents (HIGH)

`patches.py` walks the dotted path creating dicts as it goes:

```python
for key in key_path[:-1]:
    if key not in curr or not isinstance(curr[key], dict):
        curr[key] = {}          # <-- silently fabricates
    curr = curr[key]
```

**Reproduction** (manifest path typo'd to `hp_configsTYPO.crashAutoSubmit`):

```
{"name":"hpclick","hp_configs":{"crashAutoSubmit":true},
                  "hp_configsTYPO":{"crashAutoSubmit":false}}
real key still true : True
bogus key invented  : True
```

The patch reports success while the app keeps reading the old value. This is
**the exact bug `json_set` was introduced to make unrepresentable** — a
top-level `crashAutoSubmit` that nothing reads shipped once already.

**Fix:** every parent segment must already exist and be a dict; otherwise raise
naming the missing segment and the file. Never create. Also require the leaf key
to already exist (we are *changing* config, not adding it) unless the op sets
`"create": true` explicitly.

---

## D2 — the probe symbol scan is unusable as shipped (HIGH)

Currently reports **86 binaries, ~70 symbols each**, overwhelmingly libSystem
noise (`___chkstk_darwin`, `_bzero`, `_stat`, `_opendir`, `_select`). These are
normal: they are arch-specific libSystem entry points, bound two-level and
always satisfied. The one signal that matters — `_idn2_*` — appears buried
mid-line in entry 69. No human triaging a new version would find it.

This check is the whole mechanism for "HP's arm64 slice needs something they
never shipped." At this signal-to-noise it does not work.

**Fix — intersect three filters (verified to work):**

1. **arm64-only**: symbol undefined in the arm64 slice but *not* in the x86_64
   slice (`nm -arch arm64 -u` minus `nm -arch x86_64 -u`).
2. **flat-namespace**: `nm -arch arm64 -m` line contains `(dynamically looked up)`.
   Two-level symbols show `(from libSystem)` and are always satisfied.
3. **unsatisfied**: not exported (`nm -arch arm64 -gU`) by any dylib/framework/
   `.node` in the bundle **including `Electron Framework`**, which is the host
   process for `.node` addons and legitimately supplies their V8/Node symbols.

Filters 1+2 alone reduce 86 binaries to 2. Filter 3 is what separates "the host
provides it" from "nobody provides it", and is required for the result to be
trustworthy.

Report the surviving symbols grouped by binary, and map known prefixes to
formulae (`idn2_` → `libidn2`, `nghttp2_` → `libnghttp2`) to draft
`required_dylibs`.

---

## D3 — the manifest is incomplete; the corrected scan finds a real gap (needs a decision)

Running the corrected filter against stock 4.8.117 surfaces more than idn2. Both
`.node` modules carry arm64-only, flat-namespace, unsatisfied symbols:

- `_idn2_*` — 4 symbols — **satisfied** in our build by the bundled `libidn2.0.dylib`
- `_nghttp2_*` — **36 symbols** — nothing in the built bundle exports them
- `_png_init_filter_functions_neon` — nothing exports it

Confirmed against the built output:

```
_idn2_check_version              -> libidn2.0.dylib     (satisfied)
_nghttp2_version                 -> NOTHING EXPORTS IT
_png_init_filter_functions_neon  -> NOTHING EXPORTS IT
```

The Electron Framework exports none of them, so the host does not cover it.

Same class as idn2: HP's arm64 build links a curl with HTTP/2 support and ships
no nghttp2. **Latent, not fatal** — flat-namespace lookup resolves at *call*
time, so the app launches and runs until an HTTP/2 (or NEON-libpng) path is
exercised, at which point dyld aborts the process.

**This is a research task, not a mechanical fix.** Required:

1. Determine whether HP's curl actually negotiates HTTP/2 in practice (printer
   discovery, `us1.api.ws-hp.com` url-retrieval, firmware/ICC downloads). If it
   does, this is a real crash waiting on a code path we have not exercised.
2. If needed, add `libnghttp2` to `required_dylibs` — sourced the same way as
   libidn2 (Homebrew bottle, never vendored), and check its own license terms.
3. Decide on `_png_init_filter_functions_neon` separately; it likely comes from
   a statically-linked libpng and may need a different remedy.
4. Whatever is decided, record the reasoning in the manifest the way the idn2
   entry does — including if the decision is "accept the risk, document it".

Do not silently add nghttp2 without establishing (1). Do not silently ignore it
either.

---

## D4 — asar post-condition #4 is weaker than its own docstring

`patch_and_repack_asar`'s docstring promises:

> 25 unpacked files exist under `app.asar.unpacked/` **and hash-match**

The code only counts files, in the **source** tree, with no path correspondence
and no hashes:

```python
unpacked_disk_count += len(files)
if unpacked_disk_count != expected_unpacked: raise
```

It passes if the files are the wrong ones, corrupted, or at wrong paths.

**Fix:** for each header entry marked `unpacked`, resolve its path under the
**output** bundle's `app.asar.unpacked/`, assert the file exists, and assert its
SHA-256 equals the entry's `integrity.hash`. Either implement what the docstring
says or change the docstring — never leave them disagreeing.

---

## D5 — `verify`'s manifest lookup is dead code

```python
manifest = mm.find_manifest(asar_sha256=archive.header.get("hash"))
```

`archive.header` is the asar header; it has no top-level `hash`. This always
returns `None` and silently falls back to the 4.8.117 manifest. It works today
only because there is exactly one manifest, and breaks silently on the second.

**Fix:** hash the asar file itself (`sha256` of the bytes on disk, which is what
`asar_sha256` means in the manifest) and look up on that. If no manifest
matches, say so explicitly rather than falling back.

---

## D6 — per-path unpacked-flag stability is not asserted

Post-conditions check entry counts and path-set equality. A compensating swap —
one entry going packed→unpacked while another goes the other way — satisfies
both and slips through.

**Fix:** assert `bool(new.get("unpacked")) == bool(src.get("unpacked"))` per
path.

---

## D7 — minor

- `codesign --verify --deep` is used in `verify.py`; `--deep` is deprecated for
  verification and the spec says to avoid it. Nested code is signed explicitly
  during build.
- The signature check runs `codesign` **twice**, and its first branch only
  behaves because `run_cmd` returns stdout while codesign writes "valid on disk"
  to stderr. Correct today by accident. Collapse to one call and gate on the
  return code alone.
- `kill_hpclick_processes()` uses `pkill -f "HP Click"`, which matches any
  process with that string in its arguments. It does **not** kill the tool
  itself (macOS `pkill` does not signal its own ancestors — tested), but it can
  hit unrelated user processes. Narrow it to the executable names
  (`HPClickExe`, `HP Click Helper`, `JDFPrintProcessor`) plus the specific
  bundle path under test.

---

## Acceptance tests to add

10. `json_set` with a non-existent parent path → **hard error**, no output written.
11. Probe against stock 4.8.117 → surviving unsatisfied-symbol set is exactly
    the `.node` modules, and `_idn2_*` appears in it. Assert the report is short
    enough to read (e.g. ≤5 binaries).
12. Corrupt one unpacked file under `app.asar.unpacked/` → build **fails** the
    post-condition (currently passes, since only the count is checked).
13. Two manifests present, build/verify against one → the correct manifest is
    selected, and an unmatched hash reports "unsupported" rather than falling back.

As before: report which tests you actually ran, with their real output. Do not
mark a test passed unless it was executed.
