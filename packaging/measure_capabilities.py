#!/usr/bin/env python3
"""Rebuild clickgraft/data/capabilities.json from real HP Click bundles.

    python3 packaging/measure_capabilities.py BUNDLE [BUNDLE ...] [--out PATH]

Every field in that file is measured here, from a bundle on disk, so the table
the tool reasons from can be re-derived instead of trusted. Run it whenever a new
HP version is obtained, and commit the diff: a hand-edited row is exactly the
thing this exists to prevent.

Only STOCK bundles belong in the table. A ClickGraft output has arm64 where HP
shipped x86_64 and carries a stamped floor of its own, so one would poison the
very columns the table exists to record -- this script refuses them rather than
leaving that to whoever runs it.

Printer support is stored as a delta against the reference list in
data/printers-<REFERENCE_VERSION>.json rather than 117 names per version: the
deltas are small, and a delta makes "which printers would moving cost me" a
subtraction instead of a second source of truth that can drift from the first.

Target: Python 3.9+ (Standard Library only)
"""

import argparse
import json
import os
import plistlib
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clickgraft import graftable, macos_floor                      # noqa: E402

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "clickgraft", "data", "capabilities.json")

def _measured_floor(app):
    """(version string, the file that set it, how many tie, unreadable count).

    bundle_minimums() breaks ties by path, so the named file is stable across
    runs -- but thirty files tie at 10.12 in the old builds and eleven at 15.0 in
    4.10.42, so the count goes in the row: one filename reads as though one
    library were the obstacle.
    """
    found, unreadable = macos_floor.bundle_minimums(app)
    if not found:
        return None, None, 0, len(unreadable)
    rel, version = found[0]
    tied = sum(1 for _, v in found if v == version)
    return macos_floor.format_version(version), rel, tied, len(unreadable)


def measure(app, reference_names):
    with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
        info = plistlib.load(f)
    version = info.get("CFBundleShortVersionString")

    exe = os.path.realpath(os.path.join(app, "Contents", "MacOS", "HPClickExe"))
    from clickgraft.macho import get_archs
    exe_archs = sorted(get_archs(exe)) if os.path.exists(exe) else []

    # A stock bundle is Intel-only, or universal in 4.11.31 and later. arm64
    # alone is the signature of a ClickGraft copy.
    if exe_archs == ["arm64"]:
        raise ValueError(f"{app} is a ClickGraft output (arm64-only executable), not a stock bundle")

    lib = os.path.join(app, "Contents", "Resources", "app", "appData", "macx", "lib")
    addons = {}
    for name in graftable._ADDONS:
        path = os.path.join(lib, name)
        if os.path.isfile(path):
            addons[name] = sorted(get_archs(path))

    names = graftable._listed(app)
    floor, floor_from, tied, unreadable = _measured_floor(app)

    row = {
        "version": version,
        # No build in this project has been launched on a Mac at its floor. False
        # everywhere is the point: it keeps the untested claim visible to table()
        # and to the site instead of only to whoever reads a docstring.
        "floor_tested": False,
        "declared_floor": str(info.get("LSMinimumSystemVersion") or ""),
        "measured_floor": floor,
        "measured_floor_from": floor_from,
        "measured_floor_tied": tied,
        "unreadable_macho": unreadable,
        "exe_archs": exe_archs,
        "addon_archs": addons,
        "electron": graftable._electron_version(app) or None,
        "hp_native": graftable.hp_native(app),
        "blockers": graftable.blockers(app),
        "printer_count": len(names) if names is not None else None,
        # Against the reference, both directions, so neither question needs the
        # other version present to answer.
        "printers_missing_vs_reference": sorted(reference_names - names) if names is not None else None,
        "printers_extra_vs_reference": sorted(names - reference_names) if names is not None else None,
        "third_party_whitelist": os.path.isfile(
            os.path.join(app, "Contents", "Resources", "ThirdPartyWhitelist.xml")),
        "asar_integrity": "ElectronAsarIntegrity" in info,
        # The last two components only. A full path would put the maintainer's
        # home directory into a file that ships in a public repository, and
        # "which copy was this" needs no more than the folder and the bundle.
        "measured_from": "/".join(os.path.normpath(app).split(os.sep)[-2:]),
    }
    return row


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("bundles", nargs="+", help="stock HP Click.app bundles to measure")
    ap.add_argument("--out", default=DATA)
    args = ap.parse_args(argv)

    reference = os.path.join(os.path.dirname(DATA), f"printers-{graftable.REFERENCE_VERSION}.json")
    reference_names = graftable._names(reference)

    rows, failed = [], []
    for app in args.bundles:
        try:
            row = measure(app, reference_names)
        except Exception as e:                                      # noqa: BLE001
            failed.append(f"{app}: {type(e).__name__}: {e}")
            print(f"  skipped {app}\n    {type(e).__name__}: {e}", file=sys.stderr)
            continue
        rows.append(row)
        print(f"  {row['version']:<9} declared {row['declared_floor']:<6} "
              f"measured {str(row['measured_floor']):<7} "
              f"{'+'.join(row['exe_archs']) or '-':<13} electron {row['electron'] or '-':<8} "
              f"{row['printer_count']} printers", file=sys.stderr)

    rows.sort(key=lambda r: [int(p) for p in r["version"].split(".")])
    out = {
        "$comment": (
            "Measured by packaging/measure_capabilities.py from stock HP Click bundles. "
            "Do not hand-edit: re-run the script and commit its diff. "
            "declared_floor is what HP's Info.plist claims; measured_floor is the highest "
            "minimum any Mach-O inside declares, and the two disagree in both directions -- "
            "the old builds understate by two minors, 4.8.117 and later by three majors. "
            "Whether HP still serves a version is deliberately NOT here: it is a fact about "
            "HP's server today, and this tree is compared against the sources a tag committed."),
        "reference_version": graftable.REFERENCE_VERSION,
        "versions": rows,
    }
    if failed:
        out["$skipped"] = failed
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, sort_keys=False)
        f.write("\n")
    print(f"\nwrote {args.out}: {len(rows)} version(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
