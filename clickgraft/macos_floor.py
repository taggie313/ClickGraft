"""
clickgraft.macos_floor — the oldest macOS a copy can actually start on.

HP's Info.plist says LSMinimumSystemVersion 12.0, and until 1.5.9 every copy
said the same whatever went into it. It was not true. Measured 22 Sep 2026: the
Homebrew libraries a build downloaded came from the first bottle Homebrew listed,
which that day was arm64_golden_gate, built on macOS 27; the ones already in
~/.cache/clickgraft were tahoe bottles declaring minos 26.0; and that libidn2
imports _strchrnul strongly, a symbol Apple's SDK marks __OSX_AVAILABLE(15.4)
(MacOSX15.4.sdk/usr/include/_string.h:195). A copy made that way aborts at
launch on macOS 12.0-15.3 while claiming 12.0. macOS refuses to open an app
whose LSMinimumSystemVersion is newer than it is, with a clear message of its
own; a missing symbol gives a crash at launch and nothing to go on.

So the copy states the truth instead: its minimum is the highest minimum any
Mach-O inside it declares, and never lower than HP's own. That is a sound
static proxy for "nothing strongly imports a symbol newer than the floor",
because Apple's toolchain cannot produce the alternative -- an
availability-annotated symbol newer than the deployment target becomes a weak
import. It is also conservative. In 4.10.42 HP's own libmagic.1.dylib and
OpenSSL declare minos 15.0, and in 4.8.117 and 4.8.118 libmagic declares 15.0
and OpenSSL 13.0, all under an Info.plist that says 12.0; so every copy of all
three needs 15.0. A rough grep of the SDK headers for the 4.10.42 files'
libSystem imports found none newer than macOS 11, so HP's 15.0 may be a build
setting rather than a need. It still counts: a declared minimum is all that
can be checked without running the copy on every macOS.

Minimums are read with vtool, which prints LC_BUILD_VERSION and the older
LC_VERSION_MIN_MACOSX alike and ships with the Command Line Tools that
ClickGraft already requires.

Target: Python 3.9+ (Standard Library only)
"""

import os
import plistlib
import subprocess

from clickgraft.macho import is_macho


# The parts of HP's bundle a build throws away: Contents/MacOS holds the
# launcher and HPClickExe, and Contents/Frameworks holds Electron's frameworks
# and the four helper apps, every executable of which build.py replaces with
# Electron's arm64 one. What stands in their place is counted at the end of the
# build, from the finished copy.
REPLACED_BY_BUILD = ("Contents/MacOS", "Contents/Frameworks")


class MacOSTooOldError(ValueError):
    """This Mac's macOS is older than the copy would need.

    Carries .needs and .this_mac as version strings, and .reasons (see
    floor_reasons) so a caller can say what sets the floor. .after_build is
    True when the refusal came at the end of the build (build.py step 9c), so
    the download did happen and a copy was made and thrown away.
    """

    def __init__(self, message, needs, this_mac, reasons=None, after_build=False):
        super().__init__(message)
        self.needs = needs
        self.this_mac = this_mac
        self.reasons = list(reasons or [])
        self.after_build = bool(after_build)


def parse_version(text):
    """'15.0' -> (15, 0, 0). None for anything that is not a dotted number."""
    if not isinstance(text, str):
        return None
    parts = text.strip().split(".")
    if not parts or not all(p.isdigit() for p in parts) or len(parts) > 3:
        return None
    nums = [int(p) for p in parts] + [0, 0]
    return tuple(nums[:3])


def format_version(version):
    """(15, 0, 0) -> '15.0'; (10, 13, 4) -> '10.13.4'. None -> None."""
    if version is None:
        return None
    major, minor, patch = (tuple(version) + (0, 0, 0))[:3]
    return f"{major}.{minor}.{patch}" if patch else f"{major}.{minor}"


def host_macos():
    """This Mac's macOS version, or None if it cannot be read.

    sw_vers and not platform.mac_ver(): a Python linked against an SDK older
    than 11 is told "10.16" by macOS's compatibility shim, and a floor check
    that believed it would refuse every build on every Apple Silicon Mac.
    """
    try:
        r = subprocess.run(["/usr/bin/sw_vers", "-productVersion"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_version(r.stdout) if r.returncode == 0 else None


def slice_minimums(path):
    """{arch: version or None} for each slice of a Mach-O, via vtool.

    A thin file's one slice is keyed "". Returns {} when vtool cannot read
    the file at all. Only the macOS platform counts: a zippered library also
    carries a Mac Catalyst LC_BUILD_VERSION, which says nothing about macOS.
    """
    try:
        r = subprocess.run(["vtool", "-show-build", path], capture_output=True,
                           text=True, errors="replace")
    except OSError:
        return {}
    if r.returncode != 0:
        return {}
    out = {}
    arch, cmd, platform = None, None, None
    for line in r.stdout.splitlines():
        if line and not line[0].isspace() and line.endswith(":"):
            # "<path> (architecture arm64):" or, for a thin file, "<path>:"
            marker = " (architecture "
            arch = line[line.rfind(marker) + len(marker):-2] if line.endswith("):") \
                and marker in line else ""
            out.setdefault(arch, None)
            cmd = platform = None
            continue
        key, _, value = line.strip().partition(" ")
        value = value.strip()
        if key == "cmd":
            cmd, platform = value, None
        elif key == "platform":
            platform = value
        elif arch is None:
            continue
        elif ((key == "minos" and cmd == "LC_BUILD_VERSION" and platform in ("MACOS", "1"))
              or (key == "version" and cmd == "LC_VERSION_MIN_MACOSX")):
            v = parse_version(value)
            if v is not None and (out[arch] is None or v > out[arch]):
                out[arch] = v
    return out


def macho_minimum(path):
    """The highest minimum macOS any slice of a Mach-O declares.

    Every slice, not only arm64. In a copy the x86_64 slices of HP's universal
    files normally never run, but they do whenever something starts one of
    them under Rosetta, and "the highest anything declares" needs no argument
    about which code can run. On 22 Sep 2026 no file in any of the three stock
    versions, or in a copy of 4.10.42 (19 arm64 files, 93 universal, no
    x86_64-only ones), declared different minimums for its two slices, so this
    costs nothing today. None when nothing is declared or vtool cannot read
    the file.
    """
    return _highest(slice_minimums(path))


def _highest(slices):
    declared = [v for v in slices.values() if v is not None]
    return max(declared) if declared else None


def bundle_minimums(app_path, skip=()):
    """Every Mach-O in app_path -> ([(relative path, version)], [unreadable]).

    Highest first, then by path. `unreadable` lists Mach-O files vtool could not read,
    which a caller must not silently count as "no requirement". `skip` holds
    relative directory paths whose contents are ignored.
    """
    found, unreadable = [], []
    skip = tuple(s.rstrip("/") + "/" for s in skip)
    for root, dirs, files in os.walk(app_path):
        dirs.sort()
        for name in sorted(files):
            fp = os.path.join(root, name)
            rel = os.path.relpath(fp, app_path)
            if skip and (rel + "/").startswith(skip):
                continue
            if not is_macho(fp):
                continue
            slices = slice_minimums(fp)
            if not slices:
                unreadable.append(rel)
                continue
            v = _highest(slices)
            if v is not None:
                found.append((rel, v))
    found.sort(key=lambda item: (tuple(-n for n in item[1]), item[0]))
    return found, unreadable


def declared_minimum(app_path):
    """LSMinimumSystemVersion from the bundle's own Info.plist, or None."""
    try:
        with open(os.path.join(app_path, "Contents", "Info.plist"), "rb") as f:
            plist = plistlib.load(f)
    except Exception:                                              # noqa: BLE001
        return None
    return parse_version(plist.get("LSMinimumSystemVersion")) if isinstance(plist, dict) else None


def stamp_minimum(app_path, version):
    """Write version into the bundle's own Contents/Info.plist."""
    plist_p = os.path.join(app_path, "Contents", "Info.plist")
    with open(plist_p, "rb") as f:
        plist = plistlib.load(f)
    plist["LSMinimumSystemVersion"] = format_version(version)
    with open(plist_p, "wb") as f:
        plistlib.dump(plist, f)


def exact_floor(app_path, hp_declared=None):
    """The finished copy's floor: (version, [(rel, version) that reach it]).

    The highest minimum over every Mach-O in the bundle, and never lower than
    HP's own LSMinimumSystemVersion (pass hp_declared, or it is read from the
    bundle). Raises ValueError if any Mach-O cannot be read.
    """
    if hp_declared is None:
        hp_declared = declared_minimum(app_path)
    found, unreadable = bundle_minimums(app_path)
    if unreadable:
        raise ValueError(
            f"Could not read the minimum macOS of {len(unreadable)} file(s) in "
            f"{app_path}, so the copy's own minimum cannot be worked out: "
            f"{', '.join(unreadable[:5])}")
    floor = max([v for _rel, v in found] + ([hp_declared] if hp_declared else []),
                default=None)
    return floor, [(rel, v) for rel, v in found if v == floor]


def plan_floor(source_app_path, manifest, bottles=None, cache_dir=None, timeout=6):
    """The copy's floor worked out before anything is downloaded.

    Counts HP's LSMinimumSystemVersion, every Mach-O of HP's that the build
    keeps, and the macOS each Homebrew bottle the build will use is published
    for (deps.choose_bottles; pinned in shipped manifests, with API discovery
    only for unpinned development manifests). Electron's runtime is not counted here because it has not been
    downloaded yet; build.py counts it from the finished copy and refuses there
    too. Electron 39.8.4's arm64 files all declare 12.0 (measured 22 Sep 2026),
    below HP's own.

    timeout is short because the wizard asks for this on its Review screen
    and waits for the answer: `agent plan` took 2.1s in all on 22 Sep 2026.
    Should Homebrew be slow, the plan says so (complete False) rather than
    hold the screen, and the build asks again with a longer one.

    Returns a dict (all versions as tuples):
        floor      : the highest of everything below, or None
        declared   : HP's LSMinimumSystemVersion
        hp         : (rel, version) of HP's highest-minimum file, or None
        bottles    : {formula: {"tag", "macos", "url", "sha256"}}
        complete   : False when the bottles could not be chosen
        problem    : why not, in words
    """
    from clickgraft import deps

    declared = declared_minimum(source_app_path)
    found, _unreadable = bundle_minimums(source_app_path, skip=REPLACED_BY_BUILD)
    hp_top = found[0] if found else None

    complete, problem = True, ""
    if bottles is None:
        try:
            bottles = deps.choose_bottles(manifest, cache_dir=cache_dir, timeout=timeout)
        except (OSError, ValueError) as exc:
            bottles, complete, problem = {}, False, str(exc)

    terms = [v for v in (declared, hp_top[1] if hp_top else None) if v]
    terms += [b["macos"] for b in bottles.values()]
    return {
        "floor": max(terms) if terms else None,
        "declared": declared,
        "hp": hp_top,
        "bottles": bottles,
        "complete": complete,
        "problem": problem,
    }


def floor_reasons(plan):
    """What sets plan["floor"], as short phrases for a person to read."""
    floor = plan.get("floor")
    reasons = []
    if floor is None:
        return reasons
    if plan.get("hp") and plan["hp"][1] == floor:
        reasons.append("files HP ships inside HP Click itself")
    if any(b["macos"] == floor for b in plan.get("bottles", {}).values()):
        reasons.append("the support files ClickGraft adds from Homebrew")
    if not reasons and plan.get("declared") == floor:
        reasons.append("HP Click itself")
    return reasons


def too_old_message(needs, this_mac, reasons=(), app_version=None, after_build=False):
    """The refusal a person reads when their Mac is older than the copy needs.

    after_build: refused at the end of the build, once the copy was made
    (build.py step 9c), so the download did happen and must not be denied.
    """
    what = f"HP Click {app_version}" if app_version else "HP Click"
    need = f"macOS {format_version(needs)}"
    parts = [r for r in reasons if r != "HP Click itself"]
    if parts:
        because = (f" That comes from {' and '.join(parts)}, which are built for "
                   f"{need} or later.")
    elif reasons:
        because = f" That is the macOS {what} itself asks for."
    else:
        because = ""
    if after_build:
        opening = (f"This Mac has macOS {format_version(this_mac)}, and the copy of "
                   f"{what} that ClickGraft made needs {need} or later. ClickGraft "
                   f"has thrown it away rather than put it in place, and nothing "
                   f"here was replaced.")
    else:
        opening = (f"This Mac has macOS {format_version(this_mac)}, and a copy of "
                   f"{what} would need {need} or later, so ClickGraft has not made "
                   f"one. Nothing has been downloaded or written.")
    return (opening + because + "\n\n"
            f"Your HP Click is unchanged and works as it did. Once this Mac is on "
            f"{need} or later, ClickGraft can make the copy.")


def refuse_if_too_old(needs, reasons=(), app_version=None, this_mac=None, after_build=False):
    """Raise MacOSTooOldError when this Mac is older than `needs`.

    An unreadable version is let through: the copy's own LSMinimumSystemVersion
    still stops macOS opening it, with a message of its own, so guessing here
    could only refuse someone who was fine.
    """
    if this_mac is None:
        this_mac = host_macos()
    if needs is None or this_mac is None or this_mac >= needs:
        return
    raise MacOSTooOldError(
        too_old_message(needs, this_mac, reasons, app_version, after_build),
        format_version(needs), format_version(this_mac), reasons, after_build)
