# Contributing

Thanks for looking. The two most useful things you can bring are **a manifest for
a version ClickGraft doesn't support yet** and **testing against real hardware**.

On that second one: printing has been verified end to end exactly once, on an
HP DesignJet T1600dr PostScript — a nested job with cut marks, three pages sent
and three printed. Every other model, and auto-rotation and multi-roll
selection on any model, is still unproven. A single sentence saying what you
printed and whether it came out right is worth more than it sounds; the app's
"Share how it went" button on the finish screen exists for exactly this.

---

## Scope — please read before opening a PR

ClickGraft does one thing: **it makes HP Click run natively on Apple Silicon
instead of under Rosetta.** Everything in the repo exists to serve that, and the
patches it applies fall into three categories, the third deliberately narrow:

- **Making the arm64 build work** — swapping the Electron runtime, supplying the
  libraries HP's own arm64 slices reference but never ship.
- **Repairing what the repack would otherwise break or leave broken** — stopping
  the updater replacing our build with HP's Intel one, and, in 4.8.117 and
  4.8.118, fixing a genuine HP bug that throws a `SyntaxError` in the renderer on
  every launch, on Intel too.
- **Keeping a published claim true, or back-porting HP's own later fix** —
  turning off crash-report upload in `app/package.json`, which ClickGraft's
  review screen says it does, and replacing the line that writes SNMPv3 printer
  passwords into HP Click's log with the line HP itself shipped in 4.11.31.

### What a new patch has to meet

A patch to HP's files is admitted only if it does one of these:

- **(a)** repairs something the repack breaks, or an error that makes
  ClickGraft's own check of the copy fail — the updater lock, and the `SyntaxError`
  fix, which is on `verify.py`'s list of smoke-launch failure signatures, so
  without it a 4.8.x copy cannot pass. That is the whole of (a) today: an HP bug
  ClickGraft's check does not trip over is not admitted by it;
- **(b)** makes true a claim ClickGraft already publishes — the crash-report fix,
  which until 1.5.8 pointed at the root `package.json`, whose copy of the setting
  HP's crash reporter never reads, so the claim on the review screen was false for
  every copy made before then;
- **(c)** back-ports, verbatim, a change HP itself shipped in a later build — the
  SNMPv3 log line, which is HP's 4.11.31 text, copied exactly.

**And** it touches nothing HP verifies. Today that means no file inside
`app.asar` appears in any version's `ThirdPartyWhitelist.xml`: 4.8.117, 4.8.118
and 4.10.42 list 22 paths and 4.11.31 lists 36, all of them on disk outside the
archive (checked 19 Sep 2026). A patch to a file HP lists there is declined,
whatever it fixes.

An obvious fix to an HP bug that meets none of (a), (b) or (c) is declined too,
however clear the right answer. Each one is another reason for HP to look at
this project, and the argument below only holds while this list stays short.

The rule is enforced, not only written down. `clickgraft/manifest_guard.py`
holds every op the shipped manifests may contain, verbatim, as `ALLOWED_OPS`.
ClickGraft runs it on every manifest it builds with, however the manifest was
loaded, and `packaging/build_app.sh` runs it before `manifests/` is copied into
the app, so an op that isn't on the list stops the release build. Adding a patch
therefore means adding it to `ALLOWED_OPS`, in a change someone can check against
this rule. (The guard can also load a list of signatures to refuse outright, from
a file named by `$CLICKGRAFT_NEVER_DISTRIBUTE`. That list is kept outside the
repository: writing it down here would publish the very thing it exists to keep
unpublished, and the allowlist already refuses anything not on it.)

**Out of scope: anything that changes what the application is permitted to do.**
Consumables checks, licensing, trial periods, feature gates, enterprise
restrictions. Patches of that kind will be declined, however well written.

### Why — this is strategy, not squeamishness

Two reasons, and the second is the one that matters.

**The narrow case is what makes this defensible.** ClickGraft's argument is
simple and true: HP already compiles arm64 for the hard parts and ships it
packaged against an Intel runtime, so we swap that one file. It distributes none
of HP's code and runs on a copy you already installed. That argument survives
scrutiny. It stops surviving the moment the project also does something that
looks like circumventing a commercial restriction.

**HP has already built the counter-move and left it switched off.** Every
install ships `app.asar.sign`, a signature over the application archive.
Nothing verifies it at runtime. Electron's `EnableEmbeddedAsarIntegrityValidation`
fuse is likewise present in their build and **disabled**. Turning on either one
is close to free for HP, and it would reject a modified archive outright —
taking down not just whatever prompted it, but *every* patch here, including the
repairs to HP's own bugs.

So the risk is asymmetric. A change that draws attention to this project is
cheap for HP to answer and expensive for everyone relying on it. Keeping the
scope narrow is how the useful work stays possible.

### The line that does not move

**Never add logic to defeat a protection measure that is actually enforced.**

ClickGraft modifies `app.asar` today only because HP does not verify it. If a
future version starts checking its payload, the correct response is for the tool
to **stop and report that clearly** — not to learn how to get around it. A PR
that adds signature-bypass, integrity-spoofing, or fuse-flipping logic will be
declined without discussion.

---

## Adding support for a new HP Click version

This is the highest-value contribution, and `probe` does most of it:

```bash
python3 -m clickgraft.cli probe --app "/Applications/HP Click.app"
```

It reads the app and Electron versions out of the bundle, counts archive
entries, checks every patch anchor, scans for architecture gaps, and writes a
draft manifest plus a readable report. Open an issue with the report attached.

Things that decide whether a version is supportable:

- **The Electron version must match exactly.** The native modules are built
  against Nan, whose ABI is not stable across Electron majors. `39.8.x-latest`
  is not the same as `39.8.4`.
- **Every patch anchor must appear exactly once.** Zero means the code moved;
  two means the anchor is too loose. Both are errors — never a guess.
- **Watch the arm64-only undefined symbols.** `probe` reports symbols undefined
  in the arm64 slice but not the x86_64 one, and unsatisfied by anything in the
  bundle. That check is how the missing `libidn2` and `libnghttp2` were found;
  a new version may need different libraries.
- **Look at the macOS the copy will need.** It is worked out, not written in
  the manifest: the highest minimum any binary in the finished copy declares.
  The build log states it and `verify` checks it (`minimum_macos`). It is 15.0
  for every supported version (22 Sep 2026), set by HP's own `libmagic` and by
  Homebrew's bottles, so a version whose files declare more raises it for
  everyone who uses that version, and belongs in its release notes.

Write the manifest's `why` fields as if for someone who has to decide, two years
from now, whether a patch is still needed. The existing entries are the model.

---

## Running the tests

```bash
python3 -m pytest tests/ -q
```

They need a stock HP Click present and take a few minutes, because several
perform real builds — which is also why, on Apple Silicon, they need macOS 15 or
later: below the copy's minimum a build refuses to start. Use Apple's
`/usr/bin/python3` — the project targets 3.9, standard library only, no pip
installs.

**Report what you actually ran.** Don't mark a test as passing unless it
executed. This matters more than it sounds: an earlier GUI passed every
assertion in the suite while rendering a completely empty window, because the
tests inspected the widget tree and nothing checked that a pixel was ever drawn.
The test docstrings say which claims they do and do not support — keep that
habit.

---

## Code conventions

- Python 3.9, standard library only. The single dependency is Xcode Command Line
  Tools, and that is worth protecting.
- The Swift front end talks to the backend through `clickgraft/agent.py` over
  JSON. Keep build logic out of the UI so the two cannot drift apart.
- `clickgraft/asar.py` is the correctness core. Its post-conditions — entry
  counts, byte-identity of untouched entries, integrity hashes, zero blob slack,
  unpacked files present and matching — are not optional. An earlier version
  silently dropped 25 `unpacked` flags and broke printing without failing
  anything.
- Comments should record *why*, especially where the reason is a fact about
  macOS that cost someone an afternoon.
