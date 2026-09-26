"""
clickgraft.capabilities — which HP Click a given Mac and printer actually need.

graftable.py answers one question about one bundle: can this be grafted, yes or
no. That was the whole job while ClickGraft existed to put an arm64 engine into
the one version HP still shipped. It is not the job any more. HP has dropped the
T310/T320/T350/T720/T750 (4.8.118), dropped Windows DWF (4.10.38), moved its
macOS floor from 10.10 to 12.0 somewhere in 4.7.x, and now ships a native build
that makes the graft unnecessary for everyone whose printer it still lists. The
people left over each need a *different release*, and which one depends on their
Mac, their printer and what HP still serves.

So this module answers that: for a version, what it can do; for a Mac and a
printer, which version to run and what it costs.

Two sources, never mixed.

  of_bundle()   reads a bundle in hand. Always preferred: it is the only thing
                that can be true about the copy someone actually has.
  recorded()    reads data/capabilities.json, measured by
                packaging/measure_capabilities.py from stock bundles on a stated
                date, for versions nobody has on disk.

A version in neither is None -- "don't know" -- for the reason graftable.py
already refuses to guess: telling someone their version is hopeless when it is
not is worse than saying nothing.

TWO FLOORS, AND THEY ARE NOT INTERCHANGEABLE. `declared_floor` is
LSMinimumSystemVersion, which Launch Services enforces: below it the app does not
start, full stop. `library_floor` is the highest minimum any Mach-O inside
declares, which is an upper bound on what the code might need and NOT a proven
requirement -- stock 4.8.117 declares 12.0 while its bundled libmagic declares
15.0, and whether it truly needs 15.0 has never been tested on a 12-14 machine.
Gating a launch decision on the library floor would wrongly refuse macOS 12 to a
build HP ships for macOS 12, so runs_on() uses the declared floor and reports the
library floor beside it as a caveat. macos_floor.plan_floor() is the one that
matters for a *graft*, where the replacement Electron's own floor applies.

This module holds facts and never patch operations. It is not a way around
manifest_guard: nothing here is applied to a bundle.

Target: Python 3.9+ (Standard Library only)
"""

import json
import os
import plistlib
import re

from clickgraft import graftable, macos_floor
from clickgraft.macho import get_archs

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "capabilities.json")

# The last macOS major on which Rosetta runs an Intel app like HP Click. 27
# removes Rosetta during the upgrade but still offers to reinstall it -- Finder
# shows the prompt and it takes under a minute -- and Apple has said 27 is the
# last release where that is true. So 27 is "yes, after a prompt" and 28 is "no".
ROSETTA_LAST_MACOS = 27

_UNKNOWN = "don't know"


def _load():
    try:
        with open(_DATA, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"versions": [], "reference_version": graftable.REFERENCE_VERSION}


def availability(version, timeout=20):
    """Does HP still serve this version? True, False, or None if the check failed.

    Asked over the network, never recorded. Whether HP still serves a build is a
    fact about HP's server today, not about the build -- and clickgraft/ is one of
    the trees packaging/check_release.py compares against the sources a tag
    committed, so a stored answer turns "re-check availability" into "invalidate
    the release". A stale printers-4.8.117.json in that same directory is what
    put the data files inside the gate in the first place.

    None and False stay apart: a timeout must not be read as HP having deleted a
    build.
    """
    import subprocess
    url = ("https://ftp.hp.com/pub/softlib/software13/printers/hpclick/darwin/"
           f"HPClick-{version}.zip")
    try:
        r = subprocess.run(["curl", "-4", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                            "-r", "0-0", "--max-time", str(timeout), url],
                           capture_output=True, text=True, timeout=timeout + 10)
    except (OSError, subprocess.SubprocessError):
        return None
    code = r.stdout.strip()
    if code in ("200", "206"):
        return True
    return False if code == "404" else None


def recorded(version=None):
    """The measured table, or one version's row, or None if not recorded.

    Rows are the JSON as measured; see packaging/measure_capabilities.py for what
    each field means and how it was obtained.
    """
    rows = _load().get("versions") or []
    if version is None:
        return rows
    return next((r for r in rows if r.get("version") == version), None)


def reference_version():
    return _load().get("reference_version") or graftable.REFERENCE_VERSION


def _reference_names():
    path = os.path.join(os.path.dirname(_DATA), f"printers-{reference_version()}.json")
    try:
        return graftable._names(path)
    except (OSError, ValueError, KeyError, TypeError, StopIteration):
        return None


def of_bundle(app):
    """Measure a bundle in hand. Same field names as a recorded row.

    `is_stock` is False for a ClickGraft output, recognised the way
    measure_capabilities.py recognises one: an arm64-only main executable, where
    HP ships Intel-only or (from 4.11.31) universal. A copy also carries a floor
    stamped by the build, so its declared_floor describes the copy and not HP's
    release -- which is why it must never reach the recorded table.
    """
    try:
        with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
            info = plistlib.load(f)
    except Exception:                                              # noqa: BLE001
        return None
    if not isinstance(info, dict):
        return None

    exe = os.path.realpath(os.path.join(app, "Contents", "MacOS", "HPClickExe"))
    exe_archs = sorted(get_archs(exe)) if os.path.exists(exe) else []
    names = graftable._listed(app)
    ref = _reference_names()

    lib = os.path.join(app, "Contents", "Resources", "app", "appData", "macx", "lib")
    addons = {}
    for name in graftable._ADDONS:
        path = os.path.join(lib, name)
        if os.path.isfile(path):
            addons[name] = sorted(get_archs(path))

    declared = macos_floor.declared_minimum(app)
    return {
        "version": info.get("CFBundleShortVersionString"),
        "source": "bundle",
        "is_stock": exe_archs != ["arm64"],
        "declared_floor": macos_floor.format_version(declared) if declared else "",
        # Deliberately not measured here: bundle_minimums() walks every Mach-O in
        # a ~700 MB bundle and takes the better part of a minute. The recorded
        # row carries it; measure_capabilities.py is where it is paid for.
        "library_floor": None,
        "exe_archs": exe_archs,
        "addon_archs": addons,
        "electron": graftable._electron_version(app) or None,
        "hp_native": graftable.hp_native(app),
        "blockers": graftable.blockers(app),
        "printer_count": len(names) if names is not None else None,
        "printers_missing_vs_reference": sorted(ref - names) if names is not None and ref else None,
        "printers_extra_vs_reference": sorted(names - ref) if names is not None and ref else None,
        "third_party_whitelist": os.path.isfile(
            os.path.join(app, "Contents", "Resources", "ThirdPartyWhitelist.xml")),
        "asar_integrity": "ElectronAsarIntegrity" in info,
        "measured_from": app,
    }


def _as_version(value):
    """A macOS version as a 3-tuple, from a string or a tuple of any length.

    parse_version() always returns three components, but a caller writing a
    version by hand reaches for (12, 0) -- and (12, 0) >= (12, 0, 0) is False in
    Python, so an unpadded argument silently refuses a release that does run.
    format_version() pads for the same reason.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return macos_floor.parse_version(value)
    try:
        return (tuple(int(p) for p in value) + (0, 0, 0))[:3]
    except (TypeError, ValueError):
        return None


def _floor_of(cap):
    key = cap.get("declared_floor") or ""
    return macos_floor.parse_version(key) if key else None


def runs_on(cap, macos=None):
    """Will this release start on that macOS? True, False, or None if unknown.

    Answered from the DECLARED floor, which is the one Launch Services enforces.
    See the module docstring on why the library floor is reported separately
    rather than used here.
    """
    macos = _as_version(macos) if macos is not None else macos_floor.host_macos()
    floor = _floor_of(cap)
    if macos is None or floor is None:
        return None
    return macos >= floor


def rosetta_available(macos=None):
    """Can this macOS still run an Intel app? True, False, or None.

    True through ROSETTA_LAST_MACOS, though on that release the user is prompted
    to reinstall Rosetta first because the upgrade removes it.
    """
    macos = _as_version(macos) if macos is not None else macos_floor.host_macos()
    if macos is None:
        return None
    return macos[0] <= ROSETTA_LAST_MACOS


def needs_translation(cap, apple_silicon=None):
    """True when this release would have to run under Rosetta on that hardware.

    None when the bundle's architectures could not be read, since "no archs"
    means lipo failed rather than "no arm64".
    """
    archs = cap.get("exe_archs")
    if not archs:
        return None
    if apple_silicon is None:
        from clickgraft.hostarch import is_apple_silicon
        apple_silicon = is_apple_silicon()
    if not apple_silicon:
        return False
    return "arm64" not in archs


def graftable_version(cap):
    """True/False for "ClickGraft can make an Apple Silicon copy of this".

    False only on positive evidence, matching graftable.blockers(); None when
    there was nothing to read. A version HP already ships natively needs no
    graft, and says so as False with hp_native True beside it.
    """
    if cap.get("is_stock") is False:
        # Already grafted. Its Electron is the replacement and its addons carry
        # arm64, so every signal blockers() reads says "graftable" -- which is
        # how this reported True for a finished copy until 26 Sep 2026.
        return False
    if cap.get("hp_native"):
        return False
    blockers = cap.get("blockers")
    if blockers:
        return False
    if blockers == [] and cap.get("electron"):
        return True
    return None


_MODEL = re.compile(r"[A-Z]*\d+[A-Z]*")


def _model_tokens(text):
    """The model designators in a printer name, uppercased.

    Compared whole and never as substrings. "T350" must not match
    "HP DesignJet T3500", and a substring test does exactly that -- it is how an
    earlier pass through this data reported the T-series as present in 4.5.21
    when what it had found was T3500 and T7200.

    Bare numbers are dropped. HP's names carry a carriage width as its own token
    ("HP DesignJet T720 36-in"), so keeping them would make a query of "24"
    match most of the list and "T720 24-in" match anything else 24 inches wide.
    A model designator always has a letter in it.
    """
    return {t for t in _MODEL.findall((text or "").upper()) if not t.isdigit()}


def lists_printer(cap, model):
    """Does this release list that printer? True, False, or None if unknown.

    `model` may be a full name ("HP DesignJet T730") or just the designator
    ("T730", "t730"). Matching is on whole model tokens, so a query for T350
    never matches a T3500.
    """
    ref = _reference_names()
    missing = cap.get("printers_missing_vs_reference")
    extra = cap.get("printers_extra_vs_reference")
    if ref is None or missing is None or extra is None:
        return None
    names = (ref - set(missing)) | set(extra)

    wanted = _model_tokens(model)
    if not wanted:
        return None
    for name in names:
        if wanted & _model_tokens(name):
            return True
    return False


def printers(cap):
    """The full printer list this release accepts, or None if unknown.

    Reconstructed from the reference list and the recorded delta, so there is one
    source of truth for the names and the deltas cannot drift from it.
    """
    ref = _reference_names()
    missing = cap.get("printers_missing_vs_reference")
    extra = cap.get("printers_extra_vs_reference")
    if ref is None or missing is None or extra is None:
        return None
    return sorted((ref - set(missing)) | set(extra))


def _no_answer(printer, macos, rows, blocked):
    """Say *why* there is no answer, because this is the case that matters most.

    A DesignJet T310/T320/T350/T720/T750 owner on macOS 10.14 is the dead end
    this whole table exists to describe: their printer is listed by 4.8.117 and
    by nothing else, 4.8.117 needs macOS 12, and no release does both. A bare
    "nothing fits" invites them to keep hunting for a build that cannot exist, so
    the sentence names the release their printer needs and the floor that rules
    it out.
    """
    here = f"macOS {macos_floor.format_version(macos)}" if macos else "this macOS"
    if not printer:
        return f"no recorded release runs on {here}"

    listing = [r for r in rows if lists_printer(r, printer) is True]
    if not listing:
        known = any(lists_printer(r, printer) is not None for r in rows)
        if not known:
            return f"nothing is recorded about {printer}"
        return f"no recorded release lists {printer} at all"

    floors = [(_floor_of(r), r["version"]) for r in listing if _floor_of(r)]
    if floors:
        lowest, version = min(floors)
        names = ", ".join(sorted({v for _, v in floors}))
        return (f"{printer} is listed only by {names}, and the lowest floor among those is "
                f"macOS {macos_floor.format_version(lowest)} ({version}), so no release both "
                f"lists {printer} and runs on {here}")
    return f"{printer} is listed only by releases ruled out for this Mac"


def recommend(printer=None, macos=None, apple_silicon=None, check_hp=False):
    """Which HP Click this person should run, and what it costs them.

    Returns a dict:
        version        the release to run, or None when nothing recorded fits
        why            one sentence, in terms of what was given
        needs_graft    True when ClickGraft has to make a copy for this Mac
        needs_rosetta  True when it would run translated instead
        obtainable     True/False/None -- does HP still serve it
        blocked        [(version, reason)] for every release ruled out, so the
                       answer can be argued with
        alternatives   other versions that also fit, newest first

    Nothing here is a promise that the release launches: every floor is a
    declaration, and no build below 12.0 has been started on real hardware of
    that vintage by anyone in this project.
    """
    macos = _as_version(macos) if macos is not None else macos_floor.host_macos()
    if apple_silicon is None:
        if macos is not None and macos < (11, 0, 0):
            # Apple Silicon starts at macOS 11, so a question about 10.x is a
            # question about an Intel Mac. Without this, asking "what should a
            # Mojave user run" while standing on an M-series Mac answers with
            # this machine's architecture and volunteers Rosetta to someone who
            # has no use for it.
            apple_silicon = False
        else:
            from clickgraft.hostarch import is_apple_silicon
            apple_silicon = is_apple_silicon()

    rows = [r for r in recorded() if r.get("version")]
    fits, blocked = [], []
    for row in rows:
        version = row["version"]
        if runs_on(row, macos) is False:
            blocked.append((version, f"needs macOS {row.get('declared_floor')}"))
            continue
        if printer and lists_printer(row, printer) is False:
            blocked.append((version, f"does not list {printer}"))
            continue
        translated = needs_translation(row, apple_silicon)
        if translated and rosetta_available(macos) is False:
            if graftable_version(row) is not True:
                blocked.append((version, "Intel-only, this macOS has no Rosetta, and it cannot be grafted"))
                continue
        fits.append(row)

    fits.sort(key=lambda r: [int(p) for p in r["version"].split(".")], reverse=True)
    if not fits:
        return {"version": None, "why": _no_answer(printer, macos, rows, blocked),
                "needs_graft": None, "needs_rosetta": None, "obtainable": None,
                "blocked": blocked, "alternatives": []}

    best = fits[0]
    translated = needs_translation(best, apple_silicon)
    graft = bool(translated and graftable_version(best) is True)
    bits = []
    if printer:
        bits.append(f"lists {printer}")
    if macos:
        bits.append(f"runs on macOS {macos_floor.format_version(macos)}")
    if best.get("hp_native"):
        bits.append("and HP ships it for Apple Silicon")
    elif graft:
        bits.append("and ClickGraft can make an Apple Silicon copy")
    why = f"{best['version']} {', '.join(bits)}" if bits else best["version"]
    return {
        "version": best["version"],
        "why": why,
        "needs_graft": graft,
        "needs_rosetta": bool(translated) and not graft,
        "obtainable": availability(best["version"]) if check_hp else None,
        "blocked": blocked,
        "alternatives": [r["version"] for r in fits[1:]],
    }


def table():
    """Rows for the docs and the site: one per recorded version, display-ready."""
    out = []
    for row in recorded():
        out.append({
            "version": row.get("version"),
            "declared_floor": row.get("declared_floor") or _UNKNOWN,
            "library_floor": row.get("measured_floor") or _UNKNOWN,
            "archs": "+".join(row.get("exe_archs") or []) or _UNKNOWN,
            "electron": row.get("electron") or _UNKNOWN,
            "printers": row.get("printer_count"),
            "graftable": graftable_version(row),
            "hp_native": row.get("hp_native"),
            "floor_tested": row.get("floor_tested", False),
        })
    return out
