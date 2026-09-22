"""Static bundle policy checks, independent of process discovery and test launches.

The reader callback keeps archive policy testable without unpacking a bundle.
Minimum-macOS auditing delegates Mach-O inspection to macos_floor.
"""
import json
import os
import re

# Built-copy outcomes. The build applies the manifest's ops; these check that
# the result is what the ops were for, because an op can apply cleanly to a key
# nothing reads. That is not hypothetical: every release before 1.5.8 set
# crashAutoSubmit in the ROOT package.json, whose copy of the key HP's crash
# reporter never reads, the build succeeded, and every copy went on trying to
# upload crash dumps, because app/main.js require()s app/package.json (found
# 19 Sep 2026: a 4.8.117 copy logged "auto-submit: true", and both its Crashpad
# dumps were marked upload_count: 1, uploaded: 0 -- an attempt each, not a
# confirmed upload).
CRASH_PACKAGE_JSON = "app/package.json"

# Present in HP's SNMPv3 credential log line in 4.8.x and 4.10.42 bundle.js, and
# gone from 4.11.31, where HP replaced the line with one that logs no values.
SNMP_CREDENTIAL_FRAGMENT = 'authenticationPassword: "+'


def _manifest_patches_snmp_line(manifest):
    for patch in manifest.get("patches", []):
        if patch.get("path") != "app/bundle.js":
            continue
        for op in patch.get("ops", []):
            if op.get("type") == "replace" and SNMP_CREDENTIAL_FRAGMENT in op.get("anchor", ""):
                return True
    return False


def check_patch_outcomes(read_file, manifest):
    """Check the built asar says what the patches were for, not just that
    they applied. read_file(rel_path) returns the file's text, or None when
    the archive has no such file. Returns results entries; raises ValueError.

    Only the ops that exist for every version are assumed. The SNMPv3 line is
    checked where the manifest carries that op, and nothing here depends on
    the constants.js/industries.js ops, which 4.10.42 no longer has.
    """
    results = {}

    pkg_text = read_file(CRASH_PACKAGE_JSON)
    if pkg_text is None:
        raise ValueError(
            f"{CRASH_PACKAGE_JSON} is missing from the built asar. HP's crash "
            f"reporter reads crashAutoSubmit from it, so this build cannot be "
            f"shown to have crash uploads off.")
    try:
        pkg = json.loads(pkg_text)
    except ValueError as exc:
        raise ValueError(f"{CRASH_PACKAGE_JSON} in the built asar is not valid JSON: {exc}") from None
    hp_configs = pkg.get("hp_configs") if isinstance(pkg, dict) else None
    if not isinstance(hp_configs, dict) or "crashAutoSubmit" not in hp_configs:
        found = "no hp_configs.crashAutoSubmit at all"
    else:
        found = f"hp_configs.crashAutoSubmit = {json.dumps(hp_configs['crashAutoSubmit'])}"
    if not (isinstance(hp_configs, dict) and hp_configs.get("crashAutoSubmit") is False):
        raise ValueError(
            f"Crash reports are still set to upload: {CRASH_PACKAGE_JSON} has "
            f"{found}, not false. app/main.js passes that value to "
            f"crashReporter.start as uploadToServer, so this copy would try to "
            f"upload crash dumps to HP's server over plain HTTP.")
    results["crash_reports"] = (
        f"PASSED ({CRASH_PACKAGE_JSON} has hp_configs.crashAutoSubmit false, "
        f"so HP Click's crash reporter starts with uploads off)")

    if _manifest_patches_snmp_line(manifest):
        bundle_text = read_file("app/bundle.js")
        if bundle_text is None:
            raise ValueError("app/bundle.js is missing from the built asar, so the "
                             "SNMPv3 log-line fix cannot be checked.")
        left = bundle_text.count(SNMP_CREDENTIAL_FRAGMENT)
        if left:
            raise ValueError(
                f"app/bundle.js still contains HP's SNMPv3 credential log line "
                f"({left} occurrence(s) of '{SNMP_CREDENTIAL_FRAGMENT}'), which "
                f"writes the user name and both passwords into HP Click's log.")
        results["snmp_log_line"] = (
            "PASSED (app/bundle.js no longer has the line that wrote SNMPv3 "
            "passwords to the log)")

    return results


def check_minimum_macos(target_app_path):
    """No Mach-O in the copy may need a newer macOS than the copy says it needs.

    Returns the results entry; raises ValueError naming each file that does.

    build.py stamps LSMinimumSystemVersion from the highest minimum in the
    bundle (clickgraft/macos_floor.py), and this reads it back, so a copy that
    says 12.0 while carrying a library built for 27.0 cannot pass again. Before
    1.5.9 every copy said HP's 12.0 whatever went into it, and one made from the
    Homebrew bottles listed first carried a libidn2 that imports _strchrnul,
    new in macOS 15.4: on macOS 12.0-15.3 it aborts at launch, where macOS
    would have refused to open it, clearly, had the Info.plist said so.

    Static, so it runs without launching anything and on an Intel Mac.
    """
    from clickgraft.macos_floor import bundle_minimums, declared_minimum, format_version

    declared = declared_minimum(target_app_path)
    if declared is None:
        raise ValueError(
            f"{target_app_path} has no readable LSMinimumSystemVersion in its "
            f"Info.plist, so it says nothing about which macOS it needs.")
    found, unreadable = bundle_minimums(target_app_path)
    if unreadable:
        raise ValueError(
            f"Could not read the minimum macOS of {len(unreadable)} file(s), so "
            f"the copy's LSMinimumSystemVersion cannot be checked: "
            f"{', '.join(unreadable[:5])}")
    over = [(rel, v) for rel, v in found if v > declared]
    if over:
        shown = "; ".join(f"{rel} needs {format_version(v)}" for rel, v in over[:5])
        more = f" (and {len(over) - 5} more)" if len(over) > 5 else ""
        raise ValueError(
            f"{len(over)} file(s) in the copy need a newer macOS than its "
            f"Info.plist says (LSMinimumSystemVersion {format_version(declared)}): "
            f"{shown}{more}. On a Mac in between, macOS would open the copy instead "
            f"of refusing it with a clear message, and it may fail at launch.")
    top = ""
    if found:
        at_top = [rel for rel, v in found if v == found[0][1]]
        top = (f"; the highest, {format_version(found[0][1])}, is declared by "
               f"{len(at_top)} of them, e.g. {at_top[0]}")
    return (f"PASSED (Info.plist says macOS {format_version(declared)} or later, and "
            f"none of its {len(found)} Mach-O files needs newer{top})")




# The copy's CFBundleExecutable: build.py step 9 writes it, and macOS runs it
# whenever the copy is opened. It sets the DYLD_* variables and execs HPClickExe.
LAUNCHER = "Contents/MacOS/HP Click"
LIB_DIR = "Contents/Resources/app/appData/macx/lib"
PNG_SHIM = "libclickgraft-pngshim.dylib"


def check_launcher(target_app_path, manifest):
    """The launcher must insert every library the manifest marks preload:true,
    and the PNG shim when the copy has one. Returns the results entry; raises
    ValueError naming what is missing.

    Flat-namespace symbols resolve only against images already loaded, and
    nothing in HP's code has a load command for libidn2 or libnghttp2, so a
    library the launcher does not insert sits in the bundle unused and the
    first call to it aborts the app (the manifests' $preload_note). None of
    those calls happen at startup, so a launch cannot catch a missing preload,
    and flat_symbols counts a library in the bundle as a provider whether or
    not anything loads it. Measured 22 Sep 2026: a --no-preload 4.8.117 copy,
    whose launcher had no DYLD_INSERT_LIBRARIES line at all, passed every
    check. Reading the launcher is the check that can see it.

    --no-preload makes a diagnostic copy, and this is where it fails.
    """
    path = os.path.join(target_app_path, *LAUNCHER.split("/"))
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as exc:
        raise ValueError(f"Could not read the copy's launcher, {LAUNCHER}, which is "
                         f"what macOS starts when the copy is opened: {exc}") from None
    inserted = []
    for line in text.splitlines():
        m = re.match(r"""\s*export\s+DYLD_INSERT_LIBRARIES=(["']?)(.*)\1\s*$""", line)
        if m:
            inserted = [p.rsplit("/", 1)[-1] for p in m.group(2).split(":") if p]
    lib_dir = os.path.join(target_app_path, *LIB_DIR.split("/"))
    wanted = [d["name"] for d in manifest.get("required_dylibs", [])
              if d.get("preload") is True]
    if os.path.exists(os.path.join(lib_dir, PNG_SHIM)):
        wanted.append(PNG_SHIM)
    missing = [name for name in wanted if name not in inserted]
    if missing:
        raise ValueError(
            f"The copy's launcher ({LAUNCHER}) does not preload {', '.join(missing)}. "
            f"HP's code finds these only in libraries that are already loaded, so "
            f"without them it aborts the first time it needs one. A copy built with "
            f"--no-preload leaves them out on purpose: it is a diagnostic build, and "
            f"it does not pass verification.")
    absent = [name for name in inserted if not os.path.isfile(os.path.join(lib_dir, name))]
    if absent:
        raise ValueError(
            f"The copy's launcher ({LAUNCHER}) preloads {', '.join(absent)}, which "
            f"the copy does not have in {LIB_DIR}, so macOS would refuse to start it.")
    return (f"PASSED (the launcher preloads {', '.join(inserted)})" if inserted
            else "PASSED (nothing needs preloading)")
