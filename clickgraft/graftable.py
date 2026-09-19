"""
clickgraft.graftable — Tell "not supported yet" apart from "can never be".

An HP Click with no manifest has always been shown one panel: ClickGraft doesn't
know this version, send a report and support can be added. For a new HP release
that is true. For an old one it is not, and the first report from such a version
proved it: HP Click 4.7.28 (Canada, 14 Sep 2026) is built on Electron 8.2.3 and
all 70 of HP's own binaries are x86_64 only, DjCoreServicesNative included.
ClickGraft works because HP already compiles its printing code for arm64 and only
the Electron runtime is Intel; in 4.7.28 there is no arm64 code to switch on. The
person was invited to wait for support that no release can deliver.

So the evidence is read from the app itself rather than inferred from a version
number. Nothing is flagged without positive proof: a missing or unreadable file
means "don't know", and "don't know" keeps the old panel, because wrongly telling
someone their version is hopeless would be worse than the dead end this replaces.
Target: Python 3.9+ (Standard Library only)
"""

import json
import os
import plistlib

from clickgraft.macho import get_archs

# The oldest HP Click ClickGraft supports, and so the one to move to.
REFERENCE_VERSION = "4.8.117"

# Electron's first release with a darwin-arm64 build.
FIRST_ARM64_ELECTRON = 11

_ADDONS = ("DjCoreServicesNative-Electron.node", "DjConnServicesNative-Electron.node")


def _electron_version(app):
    plist = os.path.join(app, "Contents", "Frameworks", "Electron Framework.framework",
                         "Resources", "Info.plist")
    try:
        with open(plist, "rb") as f:
            p = plistlib.load(f)
        return p.get("CFBundleVersion") or p.get("CFBundleShortVersionString") or ""
    except Exception:
        return ""


def blockers(app):
    """Reasons this bundle can never be grafted, each backed by something read
    from the bundle. Empty when nothing conclusive was found."""
    found = []
    ev = _electron_version(app)
    try:
        major = int(ev.split(".")[0])
    except ValueError:
        major = None
    if major is not None and major < FIRST_ARM64_ELECTRON:
        found.append(f"It is built on Electron {ev}, and Electron had no Apple Silicon "
                     f"build before version {FIRST_ARM64_ELECTRON}.")

    lib = os.path.join(app, "Contents", "Resources", "app", "appData", "macx", "lib")
    intel_only = []
    for name in _ADDONS:
        path = os.path.join(lib, name)
        if not os.path.isfile(path):
            continue
        archs = get_archs(path)
        # No archs means lipo could not read it: unknown, not Intel-only.
        if archs and "arm64" not in archs:
            intel_only.append(name)
    if intel_only:
        found.append(f"HP's {' and '.join(intel_only)} "
                     f"{'is' if len(intel_only) == 1 else 'are'} Intel-only in this version.")
    return found


def hp_native(app):
    """True when HP itself shipped this HP Click for Apple Silicon.

    4.11.31 (published 17 Sep 2026) was the first: every Mach-O in it carries
    arm64, and it runs untranslated. Recognised by the two pieces ClickGraft
    exists to replace -- the main executable and the Electron framework -- each
    carrying BOTH architectures. A ClickGraft copy has arm64 alone, so the two
    cannot be confused; an unreadable binary is "not native", which leaves the
    older paths to handle it.
    """
    exe = os.path.join(app, "Contents", "MacOS", "HPClickExe")
    fw = os.path.join(app, "Contents", "Frameworks", "Electron Framework.framework",
                      "Electron Framework")
    for path in (exe, fw):
        # realpath: the framework's top-level binary is a symlink into
        # Versions/A, and is_macho() deliberately refuses symlinks -- so without
        # this, 4.11.31 read as not native.
        path = os.path.realpath(path)
        archs = get_archs(path) if os.path.exists(path) else []
        if not ("arm64" in archs and "x86_64" in archs):
            return False
    return True


def printers_dropped_since_reference(app):
    """Printers 4.8.117 accepts that this bundle does not, sorted.

    The other direction from printers_lost_by_moving(): for someone on a newer
    HP Click, which printers they would need 4.8.117 for. None if unreadable.
    """
    reference = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                             f"printers-{REFERENCE_VERSION}.json")
    for path in _printer_lists(app):
        try:
            return sorted(_names(reference) - _names(path))
        except (OSError, ValueError, KeyError, TypeError, StopIteration):
            return None
    return None


def _printer_lists(app):
    candidates = [
        os.path.join(app, "Contents", "Resources", "printersValidate.json"),
        os.path.join(app, "Contents", "Resources", "app.asar.unpacked", "app",
                     "node_modules", "DjConnServices", "resources", "printersValidate.json"),
    ]
    # An unpacked asar entry can be a zero-byte placeholder; skip those.
    return [p for p in candidates if os.path.isfile(p) and os.path.getsize(p) > 0]


def _names(path):
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    if isinstance(data, dict):
        rows = data.get("names")
        if rows is None:
            rows = next((v for v in data.values() if isinstance(v, list)), [])
    else:
        rows = data
    return {r if isinstance(r, str) else r["name"] for r in rows}


def printers_lost_by_moving(app):
    """Printers this bundle accepts that 4.8.117 does not, sorted.

    [] means moving costs nothing. None means the bundle's own list could not be
    read, so no claim should be made either way.
    """
    reference = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                             f"printers-{REFERENCE_VERSION}.json")
    for path in _printer_lists(app):
        try:
            return sorted(_names(path) - _names(reference))
        except (OSError, ValueError, KeyError, TypeError, StopIteration):
            return None
    return None
