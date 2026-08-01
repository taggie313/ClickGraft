# Packaging

Produces a signed, notarized `ClickGraft.app` for distribution.

```bash
./packaging/build_app.sh              # -> dist/ClickGraft.app  (unsigned)
./packaging/sign_and_notarize.sh      # sign, notarize, staple -> dist/ClickGraft.zip
```

## One-time credential setup

`sign_and_notarize.sh` never handles your password. Store it once in your login
keychain and the script refers to the profile by name:

```bash
xcrun notarytool store-credentials clickgraft-notary \
    --apple-id "you@example.com" \
    --team-id  "U9U8JC2JT7" \
    --password "abcd-efgh-ijkl-mnop"
```

The password is an **app-specific password**, not your Apple ID password —
create one at appleid.apple.com → Sign-In and Security → App-Specific Passwords.

You need a **Developer ID Application** certificate. An "Apple Development" or
"Apple Distribution" certificate will not notarize; the script checks and says so.

## Design notes

**Why a compiled launcher.** Notarization requires the bundle's main executable
to be a Mach-O signed with the hardened runtime. A `.app` whose
`CFBundleExecutable` is a shell script cannot be notarized — which would defeat
the point of shipping a bundle instead of a `.command` file. `launcher.c` is a
~40-line stub that points `PYTHONPATH` at `Contents/Resources` and execs
`/usr/bin/python3`.

**Why `PYTHONDONTWRITEBYTECODE`.** Without it, Python writes `__pycache__`
directories into `Contents/Resources` on first run, which invalidates the
bundle's code signature. This is not hypothetical: it is exactly how HP Click
breaks its own signature — `JDFPrintProcessor` writes Adobe font caches into its
own bundle, and the broken seal only surfaces when the bundle is renamed and
Gatekeeper re-assesses it, at which point macOS calls the app "damaged".

**No entitlements.** The launcher execs `/usr/bin/python3`, and after `exec` the
process runs under Apple's signature and entitlements rather than ours, so
granting our stub entitlements would achieve nothing.

**Universal binary.** The launcher is built `arm64 + x86_64`. ClickGraft's
*output* only makes sense on Apple Silicon, but an Intel Mac should still be
able to open the app and be told so clearly rather than failing to launch.

**App Management (macOS 14+).** ClickGraft creates a *new* `.app` and never
writes inside an existing one — the build stages under a non-`.app` directory
name and renames only at the very end. That ordering is what keeps macOS from
locking the bundle mid-build, and it is why no App Management permission prompt
is required.

## Verify a release before publishing

```bash
spctl --assess --type execute --verbose=4 dist/ClickGraft.app   # expect: accepted
xcrun stapler validate dist/ClickGraft.app                      # expect: worked
codesign --verify --strict --verbose=2 dist/ClickGraft.app
```

The most useful test is the honest one: download your own release on a Mac that
has never seen the app, and double-click it.
