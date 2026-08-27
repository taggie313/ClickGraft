"""
clickgraft.verify — Automated verification suite for built Apple Silicon HP Click bundles.
Audits architectures, rpaths, hardcoded paths, code signatures, ASAR integrity, and smoke-launch logs.
Target: Python 3.9+ (Standard Library only)
"""

import hashlib
import json
import os
import plistlib
import subprocess
import time
from clickgraft.asar import AsarArchive
from clickgraft.macho import get_archs, get_load_dylibs, is_macho, run_cmd


def kill_hpclick_processes(target_app_path=None, timeout=15.0):
    """Kill HP Click processes and WAIT for them to actually exit.

    Firing pkill and returning immediately is not enough. The main process
    spawns JDFPrintProcessor asynchronously, so a single sweep races with it:
    the sweep can complete, JDFPrintProcessor can spawn a moment later, and it
    then outlives everything as an orphan -- observed surviving after its own
    app bundle had been deleted, still holding a Qt local socket.

    Sweeps repeatedly until nothing matches, escalating to SIGKILL, so callers
    that go on to delete the bundle are not pulling it out from under a live
    process.
    """
    patterns = [("-x", "HPClickExe"), ("-f", "HP Click Helper"), ("-x", "JDFPrintProcessor")]
    if target_app_path:
        patterns.append(("-f", target_app_path))

    deadline = time.time() + timeout
    escalated = False
    while True:
        for flag, pat in patterns:
            cmd = ["pkill"] + (["-9"] if escalated else []) + [flag, pat]
            run_cmd(cmd, check=False)

        time.sleep(0.4)
        alive = any(
            subprocess.run(["pgrep"] + [flag, pat], capture_output=True).returncode == 0
            for flag, pat in patterns
        )
        if not alive:
            return True
        if time.time() > deadline:
            return False
        if time.time() > deadline - timeout / 2:
            escalated = True


def verify_app_bundle(target_app_path, manifest=None):
    """
    Runs complete verification suite against target_app_path.
    Returns (True, details_dict) on success, or raises ValueError on failure.
    """
    target_app_path = os.path.abspath(target_app_path)
    if not os.path.exists(target_app_path):
        raise ValueError(f"Target app path does not exist: {target_app_path}")

    asar_p = os.path.join(target_app_path, "Contents", "Resources", "app.asar")
    if not os.path.exists(asar_p):
        raise ValueError(f"Target app does not contain Resources/app.asar: {target_app_path}")

    # Identify the manifest for this bundle.
    #
    # NOT by asar SHA-256: the manifest's asar_sha256 fingerprints the *stock
    # source*, and by definition a built bundle's asar has been patched, so it
    # can never match. Looking up on it made `verify --app <built bundle>` --
    # the documented workflow -- fail 100% of the time.
    #
    # Identify by app version read from the target's own asar instead, which is
    # stable across patching. Accept the stock hash too, so verifying an
    # unmodified source bundle still works.
    if manifest is None:
        from clickgraft.manifest import ManifestManager
        mm = ManifestManager()
        archive_for_id = AsarArchive(asar_p)

        app_version = None
        try:
            pkg_node = archive_for_id.get_all_file_nodes().get("package.json")
            if pkg_node is not None:
                pkg = json.loads(archive_for_id.read_file_content(pkg_node).decode("utf-8"))
                app_version = pkg.get("version")
        except Exception:
            app_version = None

        manifest = mm.find_manifest(app_version=app_version) if app_version else None

        if not manifest:
            with open(asar_p, "rb") as f:
                asar_disk_sha256 = hashlib.sha256(f.read()).hexdigest()
            manifest = mm.find_manifest(asar_sha256=asar_disk_sha256)

        if not manifest:
            raise ValueError(
                f"No manifest matches this bundle (app version "
                f"{app_version or 'unreadable'}). Supported versions: "
                f"{', '.join(sorted(mm.manifests)) or 'none loaded'}."
            )

    results = {}

    # 1. Mach-O Architectures Check
    main_exe = os.path.join(target_app_path, "Contents", "MacOS", "HPClickExe")
    exe_archs = get_archs(main_exe)
    if "arm64" not in exe_archs:
        raise ValueError(f"Main executable {main_exe} is not native arm64: {exe_archs}")

    el_fw = os.path.join(target_app_path, "Contents", "Frameworks", "Electron Framework.framework", "Versions", "A", "Electron Framework")
    el_archs = get_archs(el_fw)
    if "arm64" not in el_archs:
        raise ValueError(f"Electron Framework binary is not native arm64: {el_archs}")

    results["architectures"] = "PASSED (Native arm64 Electron runtime)"

    # 2. Full-bundle Mach-O & RPATH Audit
    expected_x86 = set(manifest.get("expected_x86_only", []))
    x86_only_found = []
    homebrew_refs = []

    for root, dirs, files in os.walk(target_app_path):
        for f in files:
            fp = os.path.join(root, f)
            rel_p = os.path.relpath(fp, target_app_path)

            if is_macho(fp):
                archs = get_archs(fp)
                if archs == ["x86_64"]:
                    if rel_p not in expected_x86:
                        x86_only_found.append(rel_p)

                dylibs = get_load_dylibs(fp)
                for dep in dylibs:
                    if dep.startswith("/opt/homebrew") or dep.startswith("/usr/local"):
                        homebrew_refs.append((rel_p, dep))

    # Audit launcher script for hardcoded Homebrew paths
    launcher = os.path.join(target_app_path, "Contents", "MacOS", "HP Click")
    if os.path.exists(launcher):
        with open(launcher, "r", encoding="utf-8", errors="ignore") as lf:
            l_text = lf.read()
        if "/opt/homebrew" in l_text or "/usr/local" in l_text:
            homebrew_refs.append(("Contents/MacOS/HP Click", "Hardcoded Homebrew path in launcher script"))

    if x86_only_found:
        raise ValueError(f"Unexpected x86_64-only Mach-O binaries found in bundle: {x86_only_found}")

    if homebrew_refs:
        raise ValueError(f"Found hardcoded Homebrew dependency paths: {homebrew_refs}")

    results["bundle_audit"] = "PASSED (0 unexpected x86_64 binaries, 0 Homebrew path leaks)"

    # 2b. Unprovided flat-namespace symbols.
    #
    # This exists because of a real crash: importing a PNG killed the app with
    # PC=0x0 and LR inside DjCoreServicesNative. The arm64 slice referenced
    # png_init_filter_functions_neon, nothing in the bundle exported it, and the
    # call bound to null. HP never meets this because they ship an Intel Electron
    # and never load the arm64 slice -- grafting one makes that code live.
    #
    # Only FLAT-namespace undefined symbols can do this. A two-level symbol names
    # its library, so dyld fails loudly at load if it is missing; a flat one is
    # looked up across everything loaded, and resolves to null when nobody has it.
    # That distinction is the whole check: the bundle has ~1100 flat undefined
    # symbols and all but a handful are Adobe C++ resolving between its own
    # sibling dylibs, which is fine.
    #
    # Known-unprovided symbols are listed in the manifest and accepted. Anything
    # NOT on that list is a new time bomb of exactly the kind that already went
    # off once, so it fails the build rather than a customer's print job.
    accepted_missing = set(manifest.get("accepted_unprovided_symbols", []))
    flat_undef, exported = set(), set()
    for root, _dirs, files in os.walk(target_app_path):
        for f in files:
            fp = os.path.join(root, f)
            if not is_macho(fp) or "arm64" not in get_archs(fp):
                continue
            u = subprocess.run(["nm", "-m", "-arch", "arm64", "-u", fp],
                               capture_output=True, text=True)
            for line in u.stdout.splitlines():
                if "dynamically looked up" not in line:
                    continue
                parts = line.split()
                for tok in parts:
                    if tok.startswith("_") and len(tok) > 1:
                        flat_undef.add(tok[1:])
                        break
            d = subprocess.run(["nm", "-arch", "arm64", "-g", "--defined-only", fp],
                               capture_output=True, text=True)
            for line in d.stdout.splitlines():
                cols = line.split()
                if len(cols) >= 3 and cols[2].startswith("_"):
                    exported.add(cols[2][1:])

    unprovided = flat_undef - exported
    unexpected = sorted(unprovided - accepted_missing)
    if unexpected:
        raise ValueError(
            "Flat-namespace symbols that nothing in the bundle provides: "
            f"{unexpected}. These bind to NULL and crash when called -- the same "
            "failure as png_init_filter_functions_neon. Either add a shim, or "
            "record them in the manifest's accepted_unprovided_symbols with a "
            "reason if they are genuinely never reached."
        )
    results["flat_symbols"] = (
        f"PASSED ({len(flat_undef)} flat undefined, {len(unprovided)} unprovided, "
        f"all accepted)"
    )

    # 3. Code Signatures & Entitlements (D7: single call, no --deep)
    cs_res = subprocess.run(["codesign", "--verify", target_app_path], capture_output=True, text=True)
    if cs_res.returncode != 0:
        raise ValueError(f"Code signature verification FAILED for {target_app_path}\nStderr: {cs_res.stderr}")

    results["code_signature"] = "PASSED (Ad-hoc signature valid on disk)"

    # 4. ASAR Integrity & Entry Count Checks
    archive = AsarArchive(asar_p)
    packed_c, unpacked_c = archive.count_entries()

    exp_packed = manifest["asar_entries"]["packed"]
    exp_unpacked = manifest["asar_entries"]["unpacked"]
    if packed_c != exp_packed or unpacked_c != exp_unpacked:
        raise ValueError(f"ASAR entry count mismatch: {packed_c} packed, {unpacked_c} unpacked (expected {exp_packed}/{exp_unpacked})")

    main_plist_p = os.path.join(target_app_path, "Contents", "Info.plist")
    with open(main_plist_p, "rb") as pf:
        plist = plistlib.load(pf)

    header_hash = hashlib.sha256(archive.header_json_bytes).hexdigest()
    plist_hash = plist.get("ElectronAsarIntegrity", {}).get("Resources/app.asar", {}).get("hash")
    if plist_hash != header_hash:
        raise ValueError(f"Info.plist ElectronAsarIntegrity hash mismatch: plist={plist_hash} != header={header_hash}")

    results["asar_integrity"] = "PASSED (Packed/unpacked counts & Info.plist integrity hash verified)"

    # 5. Automated Smoke Launch & Multi-signature Error Detection Test
    #
    # Only possible on Apple Silicon. There is no reverse Rosetta: an Intel Mac
    # cannot execute the arm64 binary we just grafted in, and Popen raises
    # OSError 86 "Bad CPU type in executable". That is not a defect in the
    # copy — a cross-build for another Mac is a supported thing to do — so it
    # is reported as not-checked rather than failed.
    from clickgraft.hostarch import is_apple_silicon
    if not is_apple_silicon():
        results["smoke_launch"] = (
            "SKIPPED (this Mac has an Intel processor and cannot run an Apple "
            "Silicon app, so the copy could not be test-launched here)")
        return True, results

    kill_hpclick_processes(target_app_path)

    tmp_dir = run_cmd(["getconf", "DARWIN_USER_TEMP_DIR"]).strip()
    hp_log_dir = os.path.join(tmp_dir, "HP", "HP Click", "logs")
    main_log_p = os.path.join(hp_log_dir, "HP Click App.main.log")
    alt_log_p = os.path.join(hp_log_dir, "HP Click.log")

    if os.path.exists(main_log_p):
        os.remove(main_log_p)
    if os.path.exists(alt_log_p):
        os.remove(alt_log_p)

    exe_path = os.path.join(target_app_path, "Contents", "MacOS", "HPClickExe")
    lib_dir = os.path.join(target_app_path, "Contents", "Resources", "app", "appData", "macx", "lib")
    fw_dir = os.path.join(target_app_path, "Contents", "Resources", "app", "appData", "macx", "Frameworks")

    env = os.environ.copy()
    env["DYLD_FRAMEWORK_PATH"] = fw_dir
    env["DYLD_LIBRARY_PATH"] = lib_dir

    preload_dylibs = []
    for dinfo in manifest.get("required_dylibs", []):
        if dinfo.get("preload") is True:
            preload_dylibs.append(os.path.join(lib_dir, dinfo["name"]))
    if preload_dylibs:
        env["DYLD_INSERT_LIBRARIES"] = ":".join(preload_dylibs)

    proc = subprocess.Popen([exe_path], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    start_t = time.time()
    initialized = False
    failure_signatures = []

    # 90s, not 12s. The milestone is a renderer-side event, and the FIRST launch
    # after a fresh HP Click install does first-run work — building its profile,
    # cold caches, no printer configured yet — that a warmed-up launch does not.
    # Measured on an M5 Max: ~8s warm, over 12s cold. A budget that only fits the
    # warm case fails the one launch every new user makes, and reports a working
    # build as broken.
    TIMEOUT_S = 90.0
    GRACE_S = 3.0          # keep reading after the milestone, to catch late errors
    POLL_S = 0.5
    reached_at = None

    while time.time() - start_t < TIMEOUT_S:
        time.sleep(POLL_S)
        log_content = ""
        for lp in (main_log_p, alt_log_p):
            if os.path.exists(lp):
                with open(lp, "r", encoding="utf-8", errors="ignore") as lf:
                    log_content += lf.read()

        if not initialized and (
                "successful initialization" in log_content
                or "DjCoreServices initialized successfully" in log_content):
            initialized = True
            reached_at = time.time() - start_t

        for fail_sig in ["SyntaxError", "Library not loaded", "Symbol not found", "Uncaught Exception"]:
            if fail_sig in log_content and fail_sig not in failure_signatures:
                failure_signatures.append(fail_sig)

        # Stop as soon as there is an answer. The old loop only broke when it had
        # BOTH a milestone and an error, so a clean run always burned the whole
        # budget and a broken one waited for a success that never came.
        if failure_signatures:
            break
        if initialized and (time.time() - start_t) - reached_at >= GRACE_S:
            break

    kill_hpclick_processes(target_app_path)

    # Adobe's print engine writes font caches (ACRFonts/**/AdobeFnt16.lst) INSIDE
    # the bundle the first time it runs, which is what the smoke launch just did.
    # Those files are not in the signature's resource seal, so `codesign --verify`
    # now reports "a sealed resource is missing or invalid" on a bundle that was
    # valid ninety seconds ago and works perfectly. HP's own shipped app does the
    # same thing on first launch.
    #
    # Re-seal it here so the user is handed a bundle whose signature matches what
    # is actually on disk. This runs after the launch on purpose: sign first and
    # the very next run invalidates it again.
    from clickgraft.signing import sign_bundle
    try:
        sign_bundle(target_app_path)
        results["resealed"] = "PASSED (re-signed after first-run font caches were written)"
    except Exception as exc:                                       # noqa: BLE001
        results["resealed"] = f"WARNING: could not re-sign after smoke launch: {exc}"

    if failure_signatures:
        raise ValueError(f"Smoke launch FAILED with error signatures in log: {failure_signatures}")

    if not initialized:
        raise ValueError(
            f"Smoke launch FAILED: the app did not report successful initialization "
            f"within {TIMEOUT_S:.0f}s. The build itself completed; this is the "
            f"post-build check. Its log is in {hp_log_dir}.")

    results["smoke_launch"] = f"PASSED (Initialization milestone reached in {reached_at:.2f}s)"

    return True, results
