"""
clickgraft.build — Main build pipeline for clickgraft.
Copies source HP Click app to HP Click (Apple Silicon).app, leaving original untouched.
Swaps Electron runtime, bundles dylibs, updates bundle IDs, patches ASAR, writes launcher script, and signs.
Target: Python 3.9+ (Standard Library only)
"""

import os
import plistlib
import shutil
import tempfile
from clickgraft.asar import AsarArchive, patch_and_repack_asar
from clickgraft.deps import fetch_electron, fetch_or_find_dylib
from clickgraft.macho import run_cmd
from clickgraft.patches import PatchEngine
from clickgraft.signing import sign_bundle


def build_apple_silicon_bundle(
    source_app_path,
    output_app_path=None,
    manifest=None,
    preload=True,
    progress_callback=None
, allow_foreign_host=False):
    """
    Executes end-to-end build pipeline.
    source_app_path: Path to existing HP Click.app
    output_app_path: Target path (default: alongside source, e.g. HP Click (Apple Silicon).app)
    manifest: Manifest dict (if None, looked up from manifests/ or probed)
    preload: True to include DYLD_INSERT_LIBRARIES in launcher script
    progress_callback: optional function(step_str, float_percentage)
    """

    def _log(msg, pct=0.0):
        if progress_callback:
            progress_callback(msg, pct)

    # Before anything is fetched or written. Everything downstream is
    # arch-independent except the smoke launch, so an Intel Mac CAN produce a
    # correct arm64 copy for another machine — deliberately, not by accident.
    from clickgraft.hostarch import is_apple_silicon
    if not allow_foreign_host and not is_apple_silicon():
        raise ValueError(
            "This Mac has an Intel processor. ClickGraft's job is putting the "
            "Apple Silicon engine into a copy of HP Click, and that copy will "
            "not run here. Nothing has been downloaded or written. If you are "
            "building for a different Mac, that is supported - pass "
            "allow_foreign_host=True.")

    source_app_path = os.path.abspath(source_app_path)
    if not os.path.exists(source_app_path):
        raise ValueError(f"Source app path does not exist: {source_app_path}")

    # Determine default output_app_path if not specified
    if output_app_path is None:
        parent_dir = os.path.dirname(source_app_path)
        output_app_path = os.path.join(parent_dir, "HP Click (Apple Silicon).app")
    output_app_path = os.path.abspath(output_app_path)

    # 1. Manifest lookup / validation
    _log("Validating manifest...", 0.05)
    if manifest is None:
        from clickgraft.manifest import ManifestManager
        mm = ManifestManager()
        archive = AsarArchive(os.path.join(source_app_path, "Contents", "Resources", "app.asar"))
        import hashlib
        with open(os.path.join(source_app_path, "Contents", "Resources", "app.asar"), "rb") as f:
            asar_hash = hashlib.sha256(f.read()).hexdigest()
        manifest = mm.find_manifest(asar_sha256=asar_hash)
        if not manifest:
            raise ValueError(f"No manifest found matching app.asar SHA-256 {asar_hash}. Use probe to draft a manifest.")
    else:
        from clickgraft.manifest import ManifestManager
        mm = ManifestManager()
        mm.validate_manifest(manifest)

    # The source MUST be stock: patches are anchored to exact strings in
    # specific minified files. Validate here regardless of how the manifest
    # arrived -- this check used to run only when manifest was None, so any
    # caller passing manifest= (the GUI always does) skipped it entirely and
    # got a 1.4 GB copy, an Electron download, and then a cryptic
    # "anchor occurred 0 times" failure minutes later.
    import hashlib
    source_asar = os.path.join(source_app_path, "Contents", "Resources", "app.asar")
    if not os.path.exists(source_asar):
        raise ValueError(f"Source bundle has no Contents/Resources/app.asar: {source_app_path}")
    with open(source_asar, "rb") as f:
        source_asar_hash = hashlib.sha256(f.read()).hexdigest()
    if source_asar_hash != manifest["asar_sha256"]:
        raise ValueError(
            f"Source is not a stock HP Click {manifest['app_version']} bundle.\n"
            f"  expected app.asar SHA-256: {manifest['asar_sha256']}\n"
            f"  this bundle's:             {source_asar_hash}\n"
            f"If this bundle has already been patched, choose your original, "
            f"untouched HP Click instead."
        )

    electron_version = manifest["electron_version"]

    # 2. Fetch Electron runtime zip
    _log(f"Fetching/verifying Electron {electron_version} arm64 runtime...", 0.10)
    electron_zip = fetch_electron(electron_version)

    # 3. Create staging directory (non-.app name to prevent App Management locks)
    staging_dir = os.path.join(os.path.dirname(output_app_path), f"staging_clickgraft_{os.getpid()}")
    if os.path.exists(staging_dir):
        shutil.rmtree(staging_dir)

    _log("Staging source app copy...", 0.20)
    run_cmd(["ditto", source_app_path, staging_dir])

    try:
        # Extract Electron arm64 runtime using ditto to preserve macOS framework symlinks
        with tempfile.TemporaryDirectory() as tmp_dir:
            el_extract_dir = os.path.join(tmp_dir, "electron_extract")
            os.makedirs(el_extract_dir, exist_ok=True)
            _log("Extracting Electron arm64 runtime...", 0.30)
            run_cmd(["ditto", "-x", "-k", electron_zip, el_extract_dir])
            el_app = os.path.join(el_extract_dir, "Electron.app")

            # 4. Swap Runtime Frameworks & Binaries
            _log("Swapping Electron runtime frameworks and helper binaries...", 0.40)
            el_fw_dir = os.path.join(el_app, "Contents", "Frameworks")
            dst_fw_dir = os.path.join(staging_dir, "Contents", "Frameworks")
            for fw_name in os.listdir(el_fw_dir):
                if fw_name.endswith(".framework"):
                    src_fw = os.path.join(el_fw_dir, fw_name)
                    dst_fw = os.path.join(dst_fw_dir, fw_name)
                    if os.path.exists(dst_fw):
                        shutil.rmtree(dst_fw)
                    run_cmd(["ditto", src_fw, dst_fw])

            # Replace helper app executables while keeping HP Info.plist
            helpers = [
                ("HP Click Helper.app", "HP Click Helper"),
                ("HP Click Helper (GPU).app", "HP Click Helper (GPU)"),
                ("HP Click Helper (Plugin).app", "HP Click Helper (Plugin)"),
                ("HP Click Helper (Renderer).app", "HP Click Helper (Renderer)")
            ]
            for helper_app, helper_exe in helpers:
                src_exe = os.path.join(el_app, "Contents", "Frameworks", "Electron Helper.app", "Contents", "MacOS", "Electron Helper")
                dst_exe = os.path.join(staging_dir, "Contents", "Frameworks", helper_app, "Contents", "MacOS", helper_exe)
                if os.path.exists(dst_exe):
                    os.remove(dst_exe)
                shutil.copy2(src_exe, dst_exe)

            # Replace main executable
            src_main_exe = os.path.join(el_app, "Contents", "MacOS", "Electron")
            dst_hp_exe = os.path.join(staging_dir, "Contents", "MacOS", "HPClickExe")
            if os.path.exists(dst_hp_exe):
                os.remove(dst_hp_exe)
            shutil.copy2(src_main_exe, dst_hp_exe)
            os.chmod(dst_hp_exe, 0o755)

        # 5. Fetch/Bundle Required Dylibs
        _log("Bundling required dylibs...", 0.50)
        dst_lib_dir = os.path.join(staging_dir, "Contents", "Resources", "app", "appData", "macx", "lib")
        os.makedirs(dst_lib_dir, exist_ok=True)

        for dylib_info in manifest.get("required_dylibs", []):
            d_name = dylib_info["name"]
            src_dylib = fetch_or_find_dylib(dylib_info)
            dst_dylib = os.path.join(dst_lib_dir, d_name)
            if os.path.exists(dst_dylib):
                os.remove(dst_dylib)
            shutil.copy2(src_dylib, dst_dylib)
            os.chmod(dst_dylib, 0o755)
            run_cmd(["install_name_tool", "-id", f"@rpath/{d_name}", dst_dylib])

        # Rewrite internal Homebrew paths inside bundled dylibs to @rpath
        for dylib_info in manifest.get("required_dylibs", []):
            dst_d = os.path.join(dst_lib_dir, dylib_info["name"])
            if os.path.exists(dst_d):
                otool_out = run_cmd(["otool", "-L", dst_d], check=False)
                for line in otool_out.splitlines()[1:]:
                    dep = line.strip().split()[0]
                    if dep.startswith("/opt/homebrew") or dep.startswith("/usr/local"):
                        dep_name = os.path.basename(dep)
                        run_cmd(["install_name_tool", "-change", dep, f"@rpath/{dep_name}", dst_d], check=False)

        # 6. Rewrite Qt5 install names to @rpath across native modules
        _log("Rewriting Qt5 install names to @rpath...", 0.60)
        qt_libs = ["libQt5Gui.5.dylib", "libQt5Network.5.dylib", "libQt5Xml.5.dylib", "libQt5Core.5.dylib"]
        for root, dirs, files in os.walk(os.path.join(staging_dir, "Contents", "Resources", "app")):
            for f in files:
                if f.endswith(".node") or f.endswith(".dylib"):
                    fp = os.path.join(root, f)
                    otool_out = run_cmd(["otool", "-L", fp], check=False)
                    for line in otool_out.splitlines()[1:]:
                        dep = line.strip().split()[0]
                        for qlib in qt_libs:
                            if dep.endswith(qlib) and not dep.startswith("@rpath/"):
                                run_cmd(["install_name_tool", "-change", dep, f"@rpath/{qlib}", fp], check=False)

        # 7. Update Bundle Identifiers in Info.plist files
        _log("Updating bundle identifiers...", 0.65)
        main_plist_p = os.path.join(staging_dir, "Contents", "Info.plist")
        if os.path.exists(main_plist_p):
            with open(main_plist_p, "rb") as pf:
                plist = plistlib.load(pf)
            plist["CFBundleIdentifier"] = "com.hp.hpclick.arm64"
            with open(main_plist_p, "wb") as pf:
                plistlib.dump(plist, pf)

        helpers_dir = os.path.join(staging_dir, "Contents", "Frameworks")
        for h in os.listdir(helpers_dir):
            if h.endswith(".app"):
                h_plist = os.path.join(helpers_dir, h, "Contents", "Info.plist")
                if os.path.exists(h_plist):
                    with open(h_plist, "rb") as pf:
                        h_p = plistlib.load(pf)
                    h_p["CFBundleIdentifier"] = "com.hp.hpclick.arm64.helper"
                    with open(h_plist, "wb") as pf:
                        plistlib.dump(h_p, pf)

        # 8. Patch ASAR
        _log("Patching app.asar...", 0.70)
        src_asar = os.path.join(source_app_path, "Contents", "Resources", "app.asar")
        dst_asar = os.path.join(staging_dir, "Contents", "Resources", "app.asar")

        patch_engine = PatchEngine(manifest["patches"])
        header_hash = patch_and_repack_asar(src_asar, dst_asar, patch_engine, manifest)

        # Update ElectronAsarIntegrity in Info.plist
        with open(main_plist_p, "rb") as pf:
            plist = plistlib.load(pf)
        plist["ElectronAsarIntegrity"] = {
            "Resources/app.asar": {
                "algorithm": "SHA256",
                "hash": header_hash
            }
        }
        with open(main_plist_p, "wb") as pf:
            plistlib.dump(plist, pf)

        # 9. Write Shell Launcher Script
        _log("Writing shell launcher script...", 0.80)
        launcher_path = os.path.join(staging_dir, "Contents", "MacOS", "HP Click")
        if os.path.exists(launcher_path):
            os.remove(launcher_path)

        preload_lines = ""
        if preload:
            preload_dylibs = []
            for dinfo in manifest.get("required_dylibs", []):
                if dinfo.get("preload") is True:
                    preload_dylibs.append(f"$APP_DATA_DIR/lib/{dinfo['name']}")
            if preload_dylibs:
                preload_str = ":".join(preload_dylibs)
                preload_lines = f'export DYLD_INSERT_LIBRARIES="{preload_str}"'

        launcher_script = f"""#!/bin/bash
DIR="$( cd "$( dirname "${{BASH_SOURCE[0]}}" )" && pwd )"
CONTENTS_DIR="$(dirname "$DIR")"
APP_DATA_DIR="$CONTENTS_DIR/Resources/app/appData/macx"

export DYLD_FRAMEWORK_PATH="$APP_DATA_DIR/Frameworks"
export DYLD_LIBRARY_PATH="$APP_DATA_DIR/lib"
{preload_lines}

exec "$DIR/HPClickExe" "$@"
"""
        with open(launcher_path, "w", encoding="utf-8") as lf:
            lf.write(launcher_script)
        os.chmod(launcher_path, 0o755)

        # 9b. Neutralise Squirrel's installer.
        #
        # HP ships Squirrel, whose ShipIt helper replaces the whole .app in
        # place. Pointed at a ClickGraft copy it would swap the arm64 build for
        # HP's Intel one -- silently undoing the patch, and any local patches on
        # top of it, without asking.
        #
        # The manifest already stubs app-updater.js so startup() returns before
        # the updater is configured, which means nothing should ever reach this
        # binary. This is the second lock: even if a future build re-enables the
        # check, the step that overwrites the bundle cannot run.
        #
        # ONLY the ShipIt executable is replaced. Squirrel.framework's dylib is
        # linked by Electron Framework itself (@rpath/Squirrel.framework/Squirrel)
        # -- delete that and the app will not launch at all.
        _log("Disabling Squirrel's in-place installer...", 0.85)
        shipit_stub = (
            "#!/bin/sh\n"
            "# Replaced by ClickGraft. The real ShipIt overwrites the .app in place.\n"
            "logger -t ClickGraft \"blocked an auto-update: ShipIt was invoked in a patched copy\"\n"
            "echo \"ClickGraft: auto-update blocked. Installing HP's update here would\" >&2\n"
            "echo \"replace this patched arm64 copy with HP's Intel build.\" >&2\n"
            "exit 1\n"
        )
        shipit_count = 0
        squirrel_root = os.path.join(staging_dir, "Contents", "Frameworks", "Squirrel.framework")
        for root, _dirs, files in os.walk(squirrel_root):
            for fn in files:
                if fn != "ShipIt":
                    continue
                target = os.path.join(root, fn)
                if os.path.islink(target):
                    continue
                os.remove(target)
                with open(target, "w", encoding="utf-8") as sf:
                    sf.write(shipit_stub)
                os.chmod(target, 0o755)
                shipit_count += 1
        if shipit_count:
            _log(f"Squirrel installer disabled ({shipit_count} ShipIt binary replaced)", 0.88)

        # 10. Code Signing
        _log("Signing application bundle inner-to-outer...", 0.90)
        sign_bundle(staging_dir)

        # 11. Rename staging directory to final target output_app_path
        _log("Finalizing application bundle...", 0.98)
        if os.path.exists(output_app_path):
            shutil.rmtree(output_app_path)
        os.rename(staging_dir, output_app_path)
        _log(f"BUILD COMPLETED SUCCESSFULLY! Native arm64 app written to: {output_app_path}", 1.0)

        return output_app_path

    finally:
        if os.path.exists(staging_dir):
            shutil.rmtree(staging_dir, ignore_errors=True)
