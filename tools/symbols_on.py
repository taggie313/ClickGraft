#!/usr/bin/env python3
"""Would this app's libraries link on an older macOS? Asked of that macOS itself.

    python3 tools/symbols_on.py <app> --system "/Volumes/Macintosh HD" [--arch arm64]

HP Click declares LSMinimumSystemVersion 12.0 while its own libcrypto, libssl and
libmagic declare 15.0. Which is true matters: capabilities.py gates runs_on() on
the declared floor and calls the library floor an upper bound, the site says the
disagreement has never been tested, and the decision not to stamp 15.0 onto a
frozen copy of 4.11.31 rests on it.

The failure this looks for is the one that has actually happened here. A copy
built with a Homebrew libidn2 imported _strchrnul, which macOS did not have until
15.4; the symbol is looked up at launch, binds to nothing, and the app dies. So
the question is not what a build declares, it is whether every symbol it imports
exists on the older system.

WHY IT ASKS THE SYSTEM AND NOT THE SDK. The first attempt scanned the current
SDK's headers for anything annotated newer than macOS 12 and found nothing, which
would have read as an all-clear. It was wrong: _strchrnul is not declared in that
SDK's string.h at all, so the method was blind to the exact case it existed for,
and its one hit was a false positive. Header availability is not the same
question. The dyld shared cache of the older macOS is.

Mount that macOS -- a restore image, or a VM's disk attached read-only -- and
point --system at its volume. Symbols are resolved against two sets: what that
macOS exports, and what the app ships itself, because an app's own bundled
libraries satisfy each other (HP's libssl imports 143 symbols from the libcrypto
beside it, none of which macOS has or needs to have).

WHAT A CLEAN RESULT MEANS, AND WHAT IT DOES NOT. It means nothing would stop the
libraries loading: the declared floor is honest and the higher one is a build
artifact. It is not a launch. It says nothing about behaviour, about anything
reached through dlsym or the Objective-C runtime, or about whether printing works.

Target: Python 3.9+ (Standard Library only)
"""

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clickgraft.macho import get_archs, is_macho                   # noqa: E402
from clickgraft.macos_floor import _highest, format_version, parse_version, slice_minimums  # noqa: E402


def system_symbols(volume, arch):
    """Every string in that macOS's dyld shared cache for this architecture.

    The cache is not an archive and nm cannot read it, but the exported names are
    in it as strings, and absence is what this is looking for. Coarse in the
    permissive direction only: a name that is present might be a coincidence, a
    name that is absent is genuinely not exported.
    """
    d = os.path.join(volume, "System", "Library", "dyld")
    caches = sorted(os.path.join(d, f) for f in os.listdir(d)
                    if f.startswith(f"dyld_shared_cache_{arch}") and not f.endswith(".map"))
    if not caches:
        raise SystemExit(f"no {arch} shared cache under {d}")
    out = set()
    for c in caches:
        r = subprocess.run(["strings", "-a", "-n", "3", c], capture_output=True, text=True,
                           errors="replace")
        out.update(l.strip() for l in r.stdout.splitlines() if l.strip())
    return out, caches


def own_exports(app, arch):
    """What the app's own bundled dylibs export, walked without splitting paths.

    An app path with a space in it -- "4.11.31 HP Click.app" -- silently produced
    an empty set once when this was done by splitting find's output on whitespace,
    and every symbol then looked unresolved.
    """
    out = set()
    for root, _dirs, files in os.walk(app):
        for fn in files:
            fp = os.path.join(root, fn)
            if os.path.islink(fp) or not fn.endswith(".dylib"):
                continue
            r = subprocess.run(["nm", "-g", "--defined-only", "-arch", arch, fp],
                               capture_output=True, text=True, errors="replace")
            for line in r.stdout.splitlines():
                cols = line.split()
                if len(cols) >= 3 and cols[1] in "TDSBR":
                    out.add(cols[2][1:] if cols[2].startswith("_") else cols[2])
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("app")
    ap.add_argument("--system", required=True, help="the older macOS's mounted system volume")
    ap.add_argument("--arch", default=None, help="arm64 or x86_64; guessed from the app if absent")
    ap.add_argument("--above", default="12.0", help="only check files declaring more than this")
    args = ap.parse_args(argv)

    floor = parse_version(args.above)
    exe = os.path.join(args.app, "Contents", "MacOS", "HPClickExe")
    arch = args.arch or ("arm64" if os.path.exists(exe) and "arm64" in get_archs(exe) else "x86_64")

    version_plist = os.path.join(args.system, "System", "Library", "CoreServices", "SystemVersion.plist")
    system_version = "?"
    if os.path.exists(version_plist):
        r = subprocess.run(["/usr/libexec/PlistBuddy", "-c", "Print :ProductVersion", version_plist],
                           capture_output=True, text=True)
        system_version = r.stdout.strip() or "?"

    have, caches = system_symbols(args.system, arch)
    mine = own_exports(args.app, arch)
    print(f"against macOS {system_version} ({arch}), {len(caches)} cache file(s)")
    print(f"  {len(have):,} strings in the cache; the app itself exports {len(mine):,}")

    def resolves(sym):
        return sym in have or ("_" + sym) in have or sym in mine

    # The control runs every time. A check that cannot see the case it was built
    # for is not evidence, and this one has already been wrong once.
    if resolves("strchrnul"):
        raise SystemExit(
            "control failed: _strchrnul resolves against this system, but it is the "
            "symbol that crashed a copy here and did not exist before macOS 15.4. "
            "Either this system is 15.4 or newer, or the cache was not read.")

    checked, bad = 0, {}
    for root, _dirs, files in os.walk(args.app):
        for fn in files:
            fp = os.path.join(root, fn)
            if os.path.islink(fp) or not is_macho(fp):
                continue
            declared = _highest(slice_minimums(fp))
            if not declared or declared <= floor:
                continue
            checked += 1
            r = subprocess.run(["nm", "-u", "-arch", arch, fp], capture_output=True,
                               text=True, errors="replace")
            syms = {l.strip()[1:] for l in r.stdout.splitlines() if l.strip().startswith("_")}
            missing = sorted(s for s in syms if not resolves(s))
            if missing:
                bad[os.path.relpath(fp, args.app)] = missing

    print(f"  {checked} file(s) declare more than macOS {format_version(floor)}")
    if not bad:
        print(f"  RESULT: every symbol resolves on macOS {system_version}. Nothing would "
              f"stop them loading; the higher declaration is a build artifact.")
        return 0
    print(f"  RESULT: {len(bad)} file(s) import something macOS {system_version} does not have:")
    for rel, missing in bad.items():
        print(f"    {rel}")
        for s in missing[:10]:
            print(f"       {s}")
        if len(missing) > 10:
            print(f"       … and {len(missing) - 10} more")
    return 1


if __name__ == "__main__":
    sys.exit(main())
