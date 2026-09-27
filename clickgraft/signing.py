"""
clickgraft.signing — Inner-to-outer ad-hoc code signing orchestration.
Ported from the verified reference implementation repack.py.
Includes APPE recursive framework/binary signing pass and hardened runtime entitlements.

Every codesign here is required, and a failure stops the build with
codesign's own words (SigningError). Up to 1.5.8 each one ran with check=False
and its exit status was thrown away, so sign_bundle() returned normally with
codesign failing on every file, and the build went on to put that copy in place
of a working one. Signing runs on the staging copy, before anything is
replaced, so stopping here leaves the previous copy exactly as it was.

The two steps that may fail without stopping anything are said to be optional
where they run, with the reason; sign_bundle() returns what they said, for the
build log.
Target: Python 3.9+ (Standard Library only)
"""

import os
import subprocess
import tempfile
from clickgraft import macho_write
from clickgraft.macho import get_rpaths, is_macho
from clickgraft.macho_read import MachOError

ENTITLEMENTS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>com.apple.security.cs.allow-jit</key>
    <true/>
    <key>com.apple.security.cs.allow-unsigned-executable-memory</key>
    <true/>
    <key>com.apple.security.cs.allow-dyld-environment-variables</key>
    <true/>
    <key>com.apple.security.cs.disable-library-validation</key>
    <true/>
</dict>
</plist>
"""


class SigningError(RuntimeError):
    """A signing step the copy needs did not succeed. The message carries the
    tool's own stderr and the command, so a report says what went wrong."""


def _where(path, bundle):
    rel = os.path.relpath(path, bundle)
    return os.path.basename(bundle) if rel == "." else rel


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, errors="ignore")


def _codesign(args, path, bundle):
    """codesign `args` `path`, or SigningError with what codesign said."""
    cmd = ["codesign"] + list(args) + [path]
    r = _run(cmd)
    if r.returncode != 0:
        said = (r.stderr or r.stdout or "").strip() or f"no output, exit status {r.returncode}"
        raise SigningError(
            f"Signing failed: codesign could not sign {_where(path, bundle)}.\n"
            f"codesign said: {said}\n"
            f"Command: {' '.join(cmd)}")


def _add_rpath(path, bundle, notes):
    """Give an APPE framework binary the @loader_path/.. rpath it needs.

    Written directly rather than through install_name_tool, which is one of the
    seven xcrun shims this project is trying to stop requiring. The duplicate
    case is now a fact rather than a parsed error message: add_rpath() returns
    False when every slice already carries it, where install_name_tool exited
    non-zero and had to be told apart from a real failure by looking for
    "would duplicate path" in its stderr.
    """
    try:
        if not macho_write.add_rpath(path, "@loader_path/.."):
            notes.append(f"{_where(path, bundle)} already had the @loader_path/.. rpath")
    except (MachOError, OSError) as e:
        raise SigningError(
            f"Signing failed: could not add the @loader_path/.. rpath to "
            f"{_where(path, bundle)}, which the print engine's frameworks need to "
            f"find each other.\n{e}")


def sign_bundle(app_bundle_path):
    """
    Performs inner-to-outer code signing across all components of the bundle.

    Raises SigningError when a required step fails. Returns a list of notes
    from the optional steps, for the log; empty when there is nothing to say.
    """
    notes = []
    with tempfile.TemporaryDirectory() as tmp_dir:
        entitlements_path = os.path.join(tmp_dir, "entitlements.plist")
        with open(entitlements_path, "w", encoding="utf-8") as f:
            f.write(ENTITLEMENTS_XML)

        # 1. Sign inner dylibs and .node modules
        inner_dirs = [
            os.path.join(app_bundle_path, "Contents", "Resources", "app", "appData", "macx", "lib"),
            os.path.join(app_bundle_path, "Contents", "Resources", "app", "appData", "macx", "Frameworks"),
            os.path.join(app_bundle_path, "Contents", "Resources", "app.asar.unpacked")
        ]

        for d in inner_dirs:
            if os.path.exists(d):
                for root, dirs, files in os.walk(d):
                    for file_name in files:
                        fp = os.path.join(root, file_name)
                        # Not through a symlink. codesign follows one and signs
                        # the file it points at, which is signed under its own
                        # name anyway: every one of the 69-70 symlinks in these
                        # folders in 4.8.117, 4.8.118 and 4.10.42 points at a
                        # file in them (22 Sep 2026). A dangling one has nothing
                        # to sign, and now that a failed codesign stops the
                        # build it must not be asked to.
                        if os.path.islink(fp):
                            continue
                        if file_name.endswith(".dylib") or file_name.endswith(".node") or is_macho(fp):
                            _codesign(["--force", "-s", "-"], fp, app_bundle_path)

        # 2. APPE recursive-signing pass
        appe_dir = os.path.join(
            app_bundle_path,
            "Contents",
            "Resources",
            "app",
            "appData",
            "macx",
            "bin",
            "APPE",
            "JDFPrintProcessor"
        )
        if os.path.exists(appe_dir):
            for root, dirs, files in os.walk(appe_dir):
                for file_name in files:
                    fp = os.path.join(root, file_name)
                    if is_macho(fp):
                        # Not in a .dSYM. A dSYM companion holds debug symbols
                        # and is never loaded, so it has no use for an rpath.
                        # It also could not be given one: install_name_tool
                        # reported "string table not at the end of the file
                        # (can't be processed)" on AIDE.framework.dSYM in
                        # 4.8.117 and 4.10.42 (22 Sep 2026), and failed there on
                        # every build up to 1.5.8, unseen. That second reason is
                        # now history -- macho_write has no such limit -- but
                        # the first still holds, so the exclusion stays. It is
                        # still signed, as it always was.
                        if ".framework" in fp and ".dSYM" + os.sep not in fp:
                            rpaths = get_rpaths(fp)
                            if "@loader_path/.." not in rpaths:
                                _add_rpath(fp, app_bundle_path, notes)
                        _codesign(["--force", "--deep", "-s", "-"], fp, app_bundle_path)

        # 3. Top-level frameworks in Contents/Frameworks/
        fw_dir = os.path.join(app_bundle_path, "Contents", "Frameworks")
        if os.path.exists(fw_dir):
            for item in os.listdir(fw_dir):
                item_path = os.path.join(fw_dir, item)
                if item.endswith(".framework"):
                    _codesign(["--force", "--timestamp=none", "-s", "-"], item_path, app_bundle_path)

        # 4. Helper apps
        if os.path.exists(fw_dir):
            for item in os.listdir(fw_dir):
                item_path = os.path.join(fw_dir, item)
                if item.endswith(".app"):
                    _codesign(["--force", "--options", "runtime", "--entitlements", entitlements_path,
                               "-s", "-"], item_path, app_bundle_path)

        # 5. Main executables
        exe_path = os.path.join(app_bundle_path, "Contents", "MacOS", "HPClickExe")
        if os.path.exists(exe_path):
            _codesign(["--force", "--options", "runtime", "--entitlements", entitlements_path,
                       "-s", "-"], exe_path, app_bundle_path)

        main_launcher = os.path.join(app_bundle_path, "Contents", "MacOS", "HP Click")
        if os.path.exists(main_launcher) and is_macho(main_launcher):
            _codesign(["--force", "--options", "runtime", "--entitlements", entitlements_path,
                       "-s", "-"], main_launcher, app_bundle_path)

        # 6. Outer application bundle
        _codesign(["--force", "--options", "runtime", "--entitlements", entitlements_path,
                   "-s", "-"], app_bundle_path, app_bundle_path)

    # 7. Clear quarantine attributes.
    #
    # Optional. The copy is made with ditto, which carries over the source's
    # quarantine flag if it has one. Left in place it costs the owner one
    # "Open Anyway" in System Settings the first time, not a copy that works:
    # the signature above is what macOS needs to run it. xattr -dr exits 0
    # whether or not there was a flag to remove (measured 22 Sep 2026), so a
    # failure here is a file it could not change.
    r = _run(["xattr", "-dr", "com.apple.quarantine", app_bundle_path])
    if r.returncode != 0:
        notes.append("could not clear the quarantine flag, so macOS may ask before "
                     "the first launch: " + ((r.stderr or "").strip() or
                                             f"exit status {r.returncode}"))
    return notes
