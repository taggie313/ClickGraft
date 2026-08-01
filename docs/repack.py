#!/usr/bin/env python3
"""
hpclick-arm64 — Scripted Apple Silicon repack of HP Click 4.8.117
Conforms strictly to SPEC-repack-tool.md
"""

import argparse
import glob
import hashlib
import json
import os
import plistlib
import re
import shutil
import signal
import struct
import subprocess
import sys
import time
import urllib.request
import zipfile

# --- Verified Constants & Hashes ---
EXPECTED_SOURCE_VERSION = "4.8.117"
ELECTRON_VERSION = "39.8.4"
ELECTRON_ZIP_URL = f"https://github.com/electron/electron/releases/download/v{ELECTRON_VERSION}/electron-v{ELECTRON_VERSION}-darwin-arm64.zip"
ELECTRON_SHASUMS_URL = f"https://github.com/electron/electron/releases/download/v{ELECTRON_VERSION}/SHASUMS256.txt"
STOCK_ASAR_SHA256 = "47709539778938bca5b6128278b545b4f490609175a7e16cade84ccbd803bb21"
EXPECTED_PACKED_COUNT = 19202
EXPECTED_UNPACKED_COUNT = 25

REQUIRED_BREW_DYLIBS = [
    "libidn2.0.dylib",
    "libunistring.5.dylib",
    "libintl.8.dylib"
]

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

# --- Helper Utilities ---

def log_info(msg):
    print(f"[+] {msg}")

def log_warn(msg):
    print(f"[!] WARNING: {msg}")

def log_error(msg):
    print(f"[ERROR] {msg}", file=sys.stderr)

def run_cmd(cmd, check=True, capture=True):
    try:
        res = subprocess.run(cmd, check=check, capture_output=capture, text=True, errors="ignore")
        return res.stdout.strip()
    except subprocess.CalledProcessError as e:
        if check:
            log_error(f"Command failed: {' '.join(cmd)}\nStderr: {e.stderr}")
            raise
        return ""

def kill_hpclick_processes():
    targets = ["HPClickExe", "HP Click Helper", "JDFPrintProcessor", "chrome_crashpad_handler"]
    for t in targets:
        subprocess.run(["pkill", "-9", "-f", t], capture_output=True)
    time.sleep(1)

# --- Custom ASAR Reader & Writer ---

class AsarArchive:
    def __init__(self, asar_path):
        self.asar_path = asar_path
        with open(asar_path, "rb") as f:
            self.raw_bytes = f.read()
        
        # Parse header
        magic, size_and_pad_plus8, size_and_pad_plus4, header_size = struct.unpack("<IIII", self.raw_bytes[:16])
        if magic != 4:
            raise ValueError(f"Invalid ASAR header magic: {magic}")
        
        self.header_size = header_size
        self.pad = (4 - (header_size % 4)) % 4
        self.content_base = 16 + header_size + self.pad
        
        header_json_bytes = self.raw_bytes[16:16 + header_size]
        self.header = json.loads(header_json_bytes.decode("utf-8"))
        self.header_json_bytes = header_json_bytes

    def count_entries(self):
        packed = 0
        unpacked = 0
        
        def _walk(node):
            nonlocal packed, unpacked
            if "files" in node:
                for child in node["files"].values():
                    _walk(child)
            else:
                if node.get("unpacked") is True:
                    unpacked += 1
                else:
                    packed += 1

        _walk(self.header)
        return packed, unpacked

    def read_file_content(self, node):
        if node.get("unpacked") is True:
            raise ValueError("Cannot read packed content for unpacked entry")
        offset = int(node["offset"])
        size = int(node["size"])
        start = self.content_base + offset
        return self.raw_bytes[start:start + size]

def _collect_leaves(node, rel_path, out):
    """Flatten a header into {path: node}, using the same path convention as
    the rebuild walk (no leading slash)."""
    if "files" in node:
        for name, child in node["files"].items():
            child_path = f"{rel_path}/{name}" if rel_path else name
            _collect_leaves(child, child_path, out)
    else:
        out[rel_path] = node


def patch_and_repack_asar(source_asar_path, target_asar_path, patches, fake_version=False):
    log_info(f"Reading source ASAR: {source_asar_path}")
    archive = AsarArchive(source_asar_path)
    
    packed_count, unpacked_count = archive.count_entries()
    log_info(f"Source ASAR entries: {packed_count} packed, {unpacked_count} unpacked")

    if packed_count != EXPECTED_PACKED_COUNT or unpacked_count != EXPECTED_UNPACKED_COUNT:
        raise ValueError(f"Unexpected entry count: {packed_count} packed (expected {EXPECTED_PACKED_COUNT}), {unpacked_count} unpacked (expected {EXPECTED_UNPACKED_COUNT})")

    # Deep copy header
    new_header = json.loads(json.dumps(archive.header))
    new_content = bytearray()
    
    current_offset = 0

    def _process_node(rel_path, node):
        nonlocal current_offset, new_content
        if "files" in node:
            for name, child in node["files"].items():
                child_path = f"{rel_path}/{name}" if rel_path else name
                _process_node(child_path, child)
        else:
            if node.get("unpacked") is True:
                # Unpacked entry: no offset change, no content blob contribution
                return
            
            orig_content = archive.read_file_content(node)
            final_content = orig_content

            if rel_path in patches:
                log_info(f"Applying patch to ASAR entry: {rel_path}")
                patch_func = patches[rel_path]
                final_content = patch_func(orig_content)

            if fake_version and rel_path == "package.json":
                pkg = json.loads(final_content.decode("utf-8"))
                pkg["version"] = "99.99.999"
                final_content = json.dumps(pkg, indent=2).encode("utf-8")

            # Calculate integrity for patched/unpatched file
            sha256_hash = hashlib.sha256(final_content).hexdigest()
            block_size = 4 * 1024 * 1024
            blocks = []
            for i in range(0, len(final_content), block_size):
                chunk = final_content[i:i + block_size]
                blocks.append(hashlib.sha256(chunk).hexdigest())

            node["offset"] = str(current_offset)
            node["size"] = len(final_content)
            node["integrity"] = {
                "algorithm": "SHA256",
                "hash": sha256_hash,
                "blockSize": block_size,
                "blocks": blocks
            }

            new_content.extend(final_content)
            current_offset += len(final_content)

    _process_node("", new_header)

    # Compact header JSON
    header_json_bytes = json.dumps(new_header, separators=(",", ":")).encode("utf-8")
    header_size = len(header_json_bytes)
    pad = (4 - (header_size % 4)) % 4

    header_uint32_1 = 4
    header_uint32_2 = header_size + pad + 8
    header_uint32_3 = header_size + pad + 4
    header_uint32_4 = header_size

    header_binary = struct.pack("<IIII", header_uint32_1, header_uint32_2, header_uint32_3, header_uint32_4) + header_json_bytes + (b"\x00" * pad)

    os.makedirs(os.path.dirname(target_asar_path), exist_ok=True)
    with open(target_asar_path, "wb") as f:
        f.write(header_binary)
        f.write(new_content)

    # --- Post-conditions ---
    # The rebuild being correct today is not the same as it staying correct.
    # A previous attempt at this repack silently dropped all 25 unpacked flags,
    # which moved DjConnServices' resources inside the archive where the native
    # module -- plain POSIX I/O -- cannot read them. Nothing crashed at startup;
    # it broke printer paths only. These assertions are the safety net.
    rebuilt = AsarArchive(target_asar_path)
    r_packed, r_unpacked = rebuilt.count_entries()
    if r_packed != EXPECTED_PACKED_COUNT or r_unpacked != EXPECTED_UNPACKED_COUNT:
        raise ValueError(f"Rebuilt ASAR post-condition failed: {r_packed} packed, {r_unpacked} unpacked")

    src_leaves, new_leaves = {}, {}
    _collect_leaves(archive.header, "", src_leaves)
    _collect_leaves(rebuilt.header, "", new_leaves)
    if set(src_leaves) != set(new_leaves):
        raise ValueError("Rebuilt ASAR post-condition failed: entry path set changed")

    unpacked_root = os.path.join(os.path.dirname(target_asar_path), "app.asar.unpacked")
    max_end = 0
    checked_identical = 0
    for path, node in new_leaves.items():
        src_node = src_leaves[path]
        if bool(node.get("unpacked")) != bool(src_node.get("unpacked")):
            raise ValueError(f"Rebuilt ASAR post-condition failed: unpacked flag changed for {path}")

        if node.get("unpacked") is True:
            disk_path = os.path.join(unpacked_root, *path.split("/"))
            if not os.path.exists(disk_path):
                raise ValueError(f"Rebuilt ASAR post-condition failed: unpacked entry missing on disk: {path}")
            with open(disk_path, "rb") as fh:
                disk_hash = hashlib.sha256(fh.read()).hexdigest()
            if disk_hash != node["integrity"]["hash"]:
                raise ValueError(f"Rebuilt ASAR post-condition failed: unpacked entry hash mismatch on disk: {path}")
            continue

        content = rebuilt.read_file_content(node)
        if hashlib.sha256(content).hexdigest() != node["integrity"]["hash"]:
            raise ValueError(f"Rebuilt ASAR post-condition failed: integrity mismatch for {path}")

        was_patched = path in patches or (fake_version and path == "package.json")
        if not was_patched:
            if content != archive.read_file_content(src_node):
                raise ValueError(f"Rebuilt ASAR post-condition failed: unpatched entry changed: {path}")
            checked_identical += 1

        max_end = max(max_end, int(node["offset"]) + node["size"])

    blob_len = len(rebuilt.raw_bytes) - rebuilt.content_base
    if blob_len != max_end:
        raise ValueError(f"Rebuilt ASAR post-condition failed: content blob has {blob_len - max_end} bytes of slack")

    log_info(f"ASAR post-conditions passed: {r_packed} packed / {r_unpacked} unpacked, "
             f"{checked_identical} unpatched entries byte-identical, 0 slack")

    header_hash = hashlib.sha256(header_json_bytes).hexdigest()
    log_info(f"Patched ASAR generated successfully. Header SHA256: {header_hash}")
    return header_hash

# --- Subcommand Implementations ---

def find_brew_dylib(name):
    prefixes = ["/opt/homebrew/lib", "/usr/local/lib"]
    for p in prefixes:
        candidate = os.path.join(p, name)
        if os.path.exists(candidate):
            return candidate
    
    brew_path = shutil.which("brew")
    if brew_path:
        prefix = run_cmd([brew_path, "--prefix"], check=False)
        if prefix:
            cand = os.path.join(prefix, "lib", name)
            if os.path.exists(cand):
                return cand
            cellar_matches = glob.glob(os.path.join(prefix, "Cellar", "*", "*", "lib", name))
            if cellar_matches:
                return cellar_matches[0]
    return None

def assert_stock_source(source_app, cmd_name):
    """Refuse anything but stock 4.8.117.

    Patch targets are exact strings in minified files, so a different build
    corrupts silently or fails late. `build` calls this too -- with the default
    --source pointing at /Applications/HP Click.app, an already-repacked
    machine would otherwise read the wrong source and only discover it after a
    1.4 GB copy.
    """
    source_asar = os.path.join(source_app, "Contents", "Resources", "app.asar")
    if not os.path.exists(source_asar):
        log_error(f"{cmd_name} failed: Source app.asar not found at '{source_asar}'")
        sys.exit(1)

    with open(source_asar, "rb") as f:
        asar_hash = hashlib.sha256(f.read()).hexdigest()

    log_info(f"Source app.asar SHA-256: {asar_hash}")
    if asar_hash != STOCK_ASAR_SHA256:
        log_error(f"{cmd_name} failed: Source app.asar SHA-256 mismatch — this is not stock "
                  f"HP Click {EXPECTED_SOURCE_VERSION}.\nExpected: {STOCK_ASAR_SHA256}\n"
                  f"Found:    {asar_hash}\n"
                  f"If this machine is already repacked, point --source at your untouched copy.")
        sys.exit(1)
    return asar_hash


def cmd_preflight(args):
    log_info("=== Running Preflight Checks ===")
    
    # 1. macOS & arm64 check
    if sys.platform != "darwin":
        log_error("preflight failed: OS must be macOS (darwin)")
        sys.exit(1)
    
    arch = run_cmd(["uname", "-m"])
    if arch != "arm64":
        log_error("preflight failed: Host architecture must be arm64 (Apple Silicon)")
        sys.exit(1)

    # 2. Xcode CLI tools
    required_tools = ["codesign", "install_name_tool", "lipo", "otool", "ditto"]
    for t in required_tools:
        if not shutil.which(t):
            log_error(f"preflight failed: Missing required tool '{t}'. Please install Xcode Command Line Tools (`xcode-select --install`).")
            sys.exit(1)

    # 3. Source App
    source_app = os.path.abspath(args.source)
    if not os.path.exists(source_app):
        log_error(f"preflight failed: Source app not found at '{source_app}'")
        sys.exit(1)

    assert_stock_source(source_app, "preflight")

    # 4. Required Homebrew dylibs
    missing_dylibs = []
    for d in REQUIRED_BREW_DYLIBS:
        found = find_brew_dylib(d)
        if not found:
            missing_dylibs.append(d)
        else:
            log_info(f"Found dylib {d}: {found}")

    if missing_dylibs:
        log_error(f"preflight failed: Missing required dylibs: {', '.join(missing_dylibs)}")
        log_error("Remediation: Run `brew install libidn2 gettext` to install required dependencies.")
        sys.exit(1)

    log_info("ALL PREFLIGHT CHECKS PASSED SUCCESSFULLY!")

def download_and_verify_electron(cache_dir):
    os.makedirs(cache_dir, exist_ok=True)
    zip_path = os.path.join(cache_dir, f"electron-v{ELECTRON_VERSION}-darwin-arm64.zip")
    shasums_path = os.path.join(cache_dir, f"SHASUMS256-{ELECTRON_VERSION}.txt")

    if not os.path.exists(shasums_path):
        log_info(f"Fetching Electron SHASUMS256.txt from {ELECTRON_SHASUMS_URL}")
        urllib.request.urlretrieve(ELECTRON_SHASUMS_URL, shasums_path)

    expected_hash = None
    with open(shasums_path, "r", encoding="utf-8") as f:
        for line in f:
            if f"electron-v{ELECTRON_VERSION}-darwin-arm64.zip" in line:
                expected_hash = line.split()[0]
                break

    if not expected_hash:
        raise ValueError("Could not find hash for electron zip in SHASUMS256.txt")

    if os.path.exists(zip_path):
        with open(zip_path, "rb") as f:
            actual_hash = hashlib.sha256(f.read()).hexdigest()
        if actual_hash == expected_hash:
            log_info(f"Verified cached Electron zip ({zip_path})")
            return zip_path
        else:
            log_warn("Cached Electron zip hash mismatch. Re-downloading...")

    log_info(f"Downloading Electron v{ELECTRON_VERSION} darwin-arm64...")
    urllib.request.urlretrieve(ELECTRON_ZIP_URL, zip_path)
    
    with open(zip_path, "rb") as f:
        actual_hash = hashlib.sha256(f.read()).hexdigest()

    if actual_hash != expected_hash:
        raise ValueError(f"Downloaded Electron zip hash mismatch!\nExpected: {expected_hash}\nActual:   {actual_hash}")

    log_info("Electron zip downloaded and verified successfully.")
    return zip_path

def cmd_build(args):
    log_info("=== Building HP Click arm64 Bundle ===")
    source_app = os.path.abspath(args.source)
    out_dir = os.path.abspath(args.out)

    assert_stock_source(source_app, "build")

    if args.dry_run:
        log_info("[DRY RUN] Would build arm64 bundle to " + out_dir)
        return

    # Cache & Staging Setup
    cache_dir = os.path.expanduser("~/.cache/hpclick-repack")
    electron_zip = download_and_verify_electron(cache_dir)

    staging_dir = os.path.join(out_dir, "staging_hpclick_build")
    final_app = os.path.join(out_dir, "HP Click.app")

    if os.path.exists(staging_dir):
        shutil.rmtree(staging_dir)
    if os.path.exists(final_app):
        shutil.rmtree(final_app)

    os.makedirs(out_dir, exist_ok=True)

    # 1. Stage Source App using ditto under non-.app directory name
    log_info(f"Staging source app to temporary directory: {staging_dir}")
    run_cmd(["ditto", source_app, staging_dir])

    # 2. Extract Electron arm64 runtime using ditto to preserve macOS framework symlinks
    electron_extract_dir = os.path.join(out_dir, "electron_temp")
    if os.path.exists(electron_extract_dir):
        shutil.rmtree(electron_extract_dir)
    os.makedirs(electron_extract_dir, exist_ok=True)

    run_cmd(["ditto", "-x", "-k", electron_zip, electron_extract_dir])

    # 3. Swap Runtime Frameworks & Binaries
    log_info("Replacing Electron runtime frameworks & helper binaries...")
    el_app = os.path.join(electron_extract_dir, "Electron.app")
    
    # Replace all top-level frameworks (Electron Framework, Squirrel, Mantle, ReactiveObjC)
    el_fw_dir = os.path.join(el_app, "Contents", "Frameworks")
    dst_fw_dir = os.path.join(staging_dir, "Contents", "Frameworks")
    for fw_name in os.listdir(el_fw_dir):
        if fw_name.endswith(".framework"):
            src_fw = os.path.join(el_fw_dir, fw_name)
            dst_fw = os.path.join(dst_fw_dir, fw_name)
            if os.path.exists(dst_fw):
                shutil.rmtree(dst_fw)
            run_cmd(["ditto", src_fw, dst_fw])

    # Replace Helper Mach-O binaries (keeping HP helper Info.plist & bundle names)
    helpers = [
        ("HP Click Helper.app", "HP Click Helper"),
        ("HP Click Helper (GPU).app", "HP Click Helper (GPU)"),
        ("HP Click Helper (Plugin).app", "HP Click Helper (Plugin)"),
        ("HP Click Helper (Renderer).app", "HP Click Helper (Renderer)")
    ]
    for app_name, bin_name in helpers:
        src_bin = os.path.join(el_app, "Contents", "Frameworks", "Electron Helper.app", "Contents", "MacOS", "Electron Helper")
        dst_bin = os.path.join(staging_dir, "Contents", "Frameworks", app_name, "Contents", "MacOS", bin_name)
        if os.path.exists(dst_bin):
            os.remove(dst_bin)
        shutil.copy2(src_bin, dst_bin)
        os.chmod(dst_bin, 0o755)

    # Replace main HPClickExe binary
    src_main = os.path.join(el_app, "Contents", "MacOS", "Electron")
    dst_main = os.path.join(staging_dir, "Contents", "MacOS", "HPClickExe")
    if os.path.exists(dst_main):
        os.remove(dst_main)
    shutil.copy2(src_main, dst_main)
    os.chmod(dst_main, 0o755)

    # Clean temp electron extract dir
    shutil.rmtree(electron_extract_dir)

    # Copy & Relocate Homebrew Dylibs (libidn2, libunistring, libintl)
    log_info("Bundling Homebrew dylibs for arm64...")
    dst_lib_dir = os.path.join(staging_dir, "Contents", "Resources", "app", "appData", "macx", "lib")
    os.makedirs(dst_lib_dir, exist_ok=True)

    for d in REQUIRED_BREW_DYLIBS:
        src_d = find_brew_dylib(d)
        dst_d = os.path.join(dst_lib_dir, d)
        if os.path.exists(dst_d):
            os.remove(dst_d)
        shutil.copy2(src_d, dst_d)
        os.chmod(dst_d, 0o755)
        run_cmd(["install_name_tool", "-id", f"@rpath/{d}", dst_d])

    # Rewrite internal Homebrew paths inside libidn2.0.dylib to @rpath
    dst_idn2 = os.path.join(dst_lib_dir, "libidn2.0.dylib")
    if os.path.exists(dst_idn2):
        otool_idn2 = run_cmd(["otool", "-L", dst_idn2], check=False)
        for line in otool_idn2.splitlines()[1:]:
            dep = line.strip().split()[0]
            if dep.startswith("/opt/homebrew") or dep.startswith("/usr/local"):
                dep_name = os.path.basename(dep)
                run_cmd(["install_name_tool", "-change", dep, f"@rpath/{dep_name}", dst_idn2], check=False)

    # Rewrite Qt5 install names to @rpath in all native modules & dylibs
    log_info("Rewriting Qt5 install names to @rpath across native modules...")
    qt_libs = [
        "libQt5Gui.5.15.17.dylib", "libQt5Network.5.15.17.dylib", "libQt5Xml.5.15.17.dylib", "libQt5Core.5.15.17.dylib",
        "libQt5Gui.5.dylib", "libQt5Network.5.dylib", "libQt5Xml.5.dylib", "libQt5Core.5.dylib",
        "libQt5Gui.dylib", "libQt5Network.dylib", "libQt5Xml.dylib", "libQt5Core.dylib"
    ]
    for root, dirs, files in os.walk(dst_lib_dir):
        for f in files:
            if f.endswith(".node") or f.endswith(".dylib"):
                fp = os.path.join(root, f)
                for q in qt_libs:
                    run_cmd(["install_name_tool", "-change", q, f"@rpath/{q}", fp], check=False)

    # 5. ASAR Patching
    log_info("Patching app.asar...")

    def patch_package_json(raw_bytes):
        text = raw_bytes.decode("utf-8")
        if '"crashAutoSubmit": true' in text:
            text = text.replace('"crashAutoSubmit": true', '"crashAutoSubmit": false')
        elif '"crashAutoSubmit":false' in text or '"crashAutoSubmit": false' in text:
            pass
        else:
            pkg = json.loads(text)
            if "hp_configs" in pkg:
                pkg["hp_configs"]["crashAutoSubmit"] = False
            text = json.dumps(pkg, indent=2)
        return text.encode("utf-8")

    def patch_app_updater(raw_bytes):
        text = raw_bytes.decode("utf-8")
        old_str = "function startup(e){"
        if old_str not in text:
            raise ValueError(f"Patch target '{old_str}' not found in app-updater.js")
        if text.count(old_str) != 1:
            raise ValueError(f"Patch target '{old_str}' occurs {text.count(old_str)} times in app-updater.js (expected 1)")
        text = text.replace(old_str, "function startup(e){return;")
        return text.encode("utf-8")

    def patch_constants(raw_bytes):
        text = raw_bytes.decode("utf-8")
        old_str = "export var SharedConstants;"
        if old_str not in text:
            raise ValueError(f"Patch target '{old_str}' not found in constants.js")
        if text.count(old_str) != 1:
            raise ValueError(f"Patch target '{old_str}' occurs {text.count(old_str)} times in constants.js (expected 1)")
        text = text.replace(old_str, "var SharedConstants;")
        text += "\nif (typeof exports !== 'undefined') { exports.SharedConstants = SharedConstants; }\n"
        return text.encode("utf-8")

    def patch_industries(raw_bytes):
        text = raw_bytes.decode("utf-8")
        old_str = "export const Industries = ["
        if old_str not in text:
            raise ValueError(f"Patch target '{old_str}' not found in industries.js")
        if text.count(old_str) != 1:
            raise ValueError(f"Patch target '{old_str}' occurs {text.count(old_str)} times in industries.js (expected 1)")
        text = text.replace(old_str, "const Industries = [")
        text += "\nif (typeof exports !== 'undefined') { exports.Industries = Industries; }\n"
        return text.encode("utf-8")

    patches = {
        "package.json": patch_package_json,
        "app/node/main/app-updater.js": patch_app_updater,
        "app/shared/constants.js": patch_constants,
        "app/shared/industries.js": patch_industries
    }

    src_asar = os.path.join(source_app, "Contents", "Resources", "app.asar")
    dst_asar = os.path.join(staging_dir, "Contents", "Resources", "app.asar")

    new_header_hash = patch_and_repack_asar(src_asar, dst_asar, patches, fake_version=args.fake_version)

    # Update Info.plist ElectronAsarIntegrity header hash
    info_plist_path = os.path.join(staging_dir, "Contents", "Info.plist")
    with open(info_plist_path, "rb") as f:
        plist_data = plistlib.load(f)

    if "ElectronAsarIntegrity" in plist_data:
        if "Resources/app.asar" in plist_data["ElectronAsarIntegrity"]:
            plist_data["ElectronAsarIntegrity"]["Resources/app.asar"]["hash"] = new_header_hash
            with open(info_plist_path, "wb") as f:
                plistlib.dump(plist_data, f)
            log_info(f"Updated Info.plist ElectronAsarIntegrity hash: {new_header_hash}")

    # 6. Write Shell Launcher Script
    log_info("Writing shell launcher script...")
    launcher_path = os.path.join(staging_dir, "Contents", "MacOS", "HP Click")
    preload_cmd = ""
    if args.preload_idn2:
        preload_cmd = 'export DYLD_INSERT_LIBRARIES="$DIR"/../Resources/app/appData/macx/lib/libunistring.5.dylib:"$DIR"/../Resources/app/appData/macx/lib/libidn2.0.dylib\n'

    launcher_content = f"""#!/bin/bash
DIR="$( cd "$( dirname "${{BASH_SOURCE[0]}}" )" && pwd )"
export DYLD_FRAMEWORK_PATH="$DIR"/../Resources/app/appData/macx/Frameworks
export DYLD_LIBRARY_PATH="$DIR"/../Resources/app/appData/macx/lib
{preload_cmd}exec "$DIR"/HPClickExe
"""
    with open(launcher_path, "w", encoding="utf-8") as f:
        f.write(launcher_content)
    os.chmod(launcher_path, 0o755)

    # 7. Write Entitlements file
    ent_path = os.path.join(out_dir, "entitlements.plist")
    with open(ent_path, "w", encoding="utf-8") as f:
        f.write(ENTITLEMENTS_XML)

    # 8. Inner-to-Outer Code Signing
    log_info("Signing application bundle inner-to-outer...")

    # Sign native dylibs and node modules in appData
    for root, dirs, files in os.walk(os.path.join(staging_dir, "Contents", "Resources", "app", "appData")):
        for f in files:
            fp = os.path.join(root, f)
            if not os.path.islink(fp):
                res = run_cmd(["file", fp], check=False)
                if "Mach-O" in res:
                    run_cmd(["codesign", "--force", "--sign", "-", fp])

    # Sign inner frameworks inside APPE/JDFPrintProcessor
    appe_fw_dir = os.path.join(staging_dir, "Contents", "Resources", "app", "appData", "macx", "bin", "APPE", "JDFPrintProcessor", "Frameworks")
    if os.path.exists(appe_fw_dir):
        for fw in glob.glob(os.path.join(appe_fw_dir, "*.framework")):
            fw_name = os.path.basename(fw).replace(".framework", "")
            fw_bin = os.path.join(fw, fw_name)
            if not os.path.exists(fw_bin) and os.path.exists(os.path.join(fw, "Versions", "Current", fw_name)):
                fw_bin = os.path.join(fw, "Versions", "Current", fw_name)
            if os.path.exists(fw_bin):
                run_cmd(["install_name_tool", "-add_rpath", "@loader_path/..", fw_bin], check=False)
            run_cmd(["codesign", "--force", "--sign", "-", fw])

    # Sign top-level Frameworks
    top_fw_dir = os.path.join(staging_dir, "Contents", "Frameworks")
    for fw in glob.glob(os.path.join(top_fw_dir, "*.framework")):
        run_cmd(["codesign", "--force", "--sign", "-", "--timestamp=none", fw])

    # Sign Helper apps with entitlements
    for app_name, _ in helpers:
        h_app = os.path.join(top_fw_dir, app_name)
        run_cmd(["codesign", "--force", "--sign", "-", "--options", "runtime", "--entitlements", ent_path, h_app])

    # Sign outer bundle
    run_cmd(["codesign", "--force", "--sign", "-", "--options", "runtime", "--entitlements", ent_path, staging_dir])
    run_cmd(["xattr", "-dr", "com.apple.quarantine", staging_dir], check=False)

    # 9. Atomic Rename Staging Directory to HP Click.app
    log_info(f"Renaming staging directory to final bundle: {final_app}")
    os.rename(staging_dir, final_app)
    
    # Re-sign after rename to ensure LaunchServices seal is valid
    run_cmd(["codesign", "--force", "--sign", "-", "--options", "runtime", "--entitlements", ent_path, final_app])

    log_info("BUILD COMPLETED SUCCESSFULLY!")

def cmd_verify(args):
    log_info("=== Running Verification Suite ===")
    target_app = os.path.abspath(args.app if args.app else "/Applications/HP Click.app")
    if not os.path.exists(target_app):
        log_error(f"verify failed: App not found at '{target_app}'")
        sys.exit(1)

    # 1. Architecture Check (lipo -archs)
    log_info("1. Checking Mach-O architectures...")
    framework_bin = os.path.join(target_app, "Contents", "Frameworks", "Electron Framework.framework", "Versions", "A", "Electron Framework")
    main_bin = os.path.join(target_app, "Contents", "MacOS", "HPClickExe")
    helpers = [
        "HP Click Helper.app/Contents/MacOS/HP Click Helper",
        "HP Click Helper (GPU).app/Contents/MacOS/HP Click Helper (GPU)",
        "HP Click Helper (Plugin).app/Contents/MacOS/HP Click Helper (Plugin)",
        "HP Click Helper (Renderer).app/Contents/MacOS/HP Click Helper (Renderer)"
    ]

    for b in [framework_bin, main_bin] + [os.path.join(target_app, "Contents", "Frameworks", h) for h in helpers]:
        archs = run_cmd(["lipo", "-archs", b])
        if archs != "arm64":
            log_error(f"verify failed: Binary '{b}' architecture is '{archs}' (expected 'arm64')")
            sys.exit(1)

    log_info("   -> All Electron runtime binaries are native arm64")

    # 2. Check Native Node Modules
    node_modules = [
        os.path.join(target_app, "Contents", "Resources", "app", "appData", "macx", "lib", "DjCoreServicesNative-Electron.node"),
        os.path.join(target_app, "Contents", "Resources", "app", "appData", "macx", "lib", "DjConnServicesNative-Electron.node")
    ]
    for nm in node_modules:
        archs = run_cmd(["lipo", "-archs", nm])
        if "x86_64" not in archs or "arm64" not in archs:
            log_error(f"verify failed: Native module '{nm}' architecture is '{archs}' (expected 'x86_64 arm64')")
            sys.exit(1)

    log_info("   -> Proprietary native modules intact (x86_64 arm64)")

    # 3. Full-Bundle Mach-O & RPATH Audit
    log_info("2. Performing full-bundle Mach-O & RPATH audit...")
    x86_only = []
    homebrew_refs = []

    for root, dirs, files in os.walk(target_app):
        for f in files:
            fp = os.path.join(root, f)
            if not os.path.islink(fp):
                fres = run_cmd(["file", fp], check=False)
                if "Mach-O" in fres:
                    archs = run_cmd(["lipo", "-archs", fp], check=False)
                    if not archs:
                        continue
                    rel_p = os.path.relpath(fp, target_app)
                    if archs == "x86_64":
                        x86_only.append(rel_p)

                    otool_l = run_cmd(["otool", "-L", fp], check=False)
                    for dline in otool_l.splitlines()[1:]:
                        dep = dline.strip().split()[0]
                        if dep.startswith("/opt/homebrew") or dep.startswith("/usr/local"):
                            homebrew_refs.append((rel_p, dep))

    # Audit launcher script for hardcoded paths
    launcher_p = os.path.join(target_app, "Contents", "MacOS", "HP Click")
    if os.path.exists(launcher_p):
        with open(launcher_p, "r", encoding="utf-8") as f:
            ltext = f.read()
        if "/opt/homebrew" in ltext or "/usr/local" in ltext:
            homebrew_refs.append(("Contents/MacOS/HP Click (launcher)", "Hardcoded Homebrew path in launcher script"))

    log_info(f"   -> x86_64-only Mach-O binaries found: {len(x86_only)}")
    for x in x86_only:
        log_info(f"      [info] {x}")

    expected_x86 = [
        "Contents/Resources/app/appData/macx/Frameworks/AdobeAXE16SharedExpat.framework/Versions/A/AdobeAXE16SharedExpat"
    ]
    for x in x86_only:
        if x not in expected_x86:
            log_warn(f"Unexpected x86_64 binary found: {x}")

    if homebrew_refs:
        log_error(f"verify failed: Found hardcoded Homebrew dependencies: {homebrew_refs}")
        sys.exit(1)
    else:
        log_info("   -> 0 hardcoded Homebrew paths in binaries or launcher script (Clean)")

    # 4. Code Signature Verification
    log_info("3. Verifying code signatures...")
    # Must gate on the exit code. This previously ran with check=False and
    # printed "clean" unconditionally, so it reported a pass for a bundle
    # codesign rejects with "a sealed resource is missing or invalid" -- the
    # check most likely to catch a botched signing pass could not fail.
    # (--deep is deprecated for verification; the nested code is signed
    # explicitly during build.)
    cs = subprocess.run(["codesign", "--verify", "--verbose=2", target_app],
                        capture_output=True, text=True, errors="ignore")
    if cs.returncode != 0:
        detail = (cs.stderr or cs.stdout).strip()
        log_error(f"verify failed: code signature invalid (codesign exit {cs.returncode})\n{detail}")
        sys.exit(1)
    log_info("   -> Code signature valid")

    # 5. ASAR Post-Condition Re-Check
    log_info("4. Checking ASAR entry counts...")
    asar_p = os.path.join(target_app, "Contents", "Resources", "app.asar")
    rebuilt_asar = AsarArchive(asar_p)
    p_cnt, u_cnt = rebuilt_asar.count_entries()
    if p_cnt != EXPECTED_PACKED_COUNT or u_cnt != EXPECTED_UNPACKED_COUNT:
        log_error(f"verify failed: ASAR entry count mismatch: {p_cnt} packed, {u_cnt} unpacked")
        sys.exit(1)

    log_info(f"   -> ASAR entries verified: {p_cnt} packed, {u_cnt} unpacked")

    # 6. Automated Smoke Launch Test
    log_info("5. Running automated smoke-launch test...")
    kill_hpclick_processes()

    tmp_dir = run_cmd(["getconf", "DARWIN_USER_TEMP_DIR"])
    hp_log_dir = os.path.join(tmp_dir.strip(), "HP", "HP Click", "logs")
    main_log_p = os.path.join(hp_log_dir, "HP Click App.main.log")
    
    if os.path.exists(main_log_p):
        os.remove(main_log_p)

    exe_path = os.path.join(target_app, "Contents", "MacOS", "HPClickExe")
    lib_dir = os.path.join(target_app, "Contents", "Resources", "app", "appData", "macx", "lib")
    fw_dir = os.path.join(target_app, "Contents", "Resources", "app", "appData", "macx", "Frameworks")
    
    env = os.environ.copy()
    env["DYLD_FRAMEWORK_PATH"] = fw_dir
    env["DYLD_LIBRARY_PATH"] = lib_dir
    env["DYLD_INSERT_LIBRARIES"] = f"{os.path.join(lib_dir, 'libunistring.5.dylib')}:{os.path.join(lib_dir, 'libidn2.0.dylib')}"

    start_t = time.time()
    proc = subprocess.Popen([exe_path], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    initialized = False
    has_syntax_error = False
    quiescence_time = 0

    log_info("   -> Monitoring application startup log...")
    for _ in range(40):
        time.sleep(0.5)
        if os.path.exists(main_log_p):
            with open(main_log_p, "r", encoding="utf-8", errors="ignore") as lf:
                lcontent = lf.read()
            if 'successful initialization' in lcontent or 'DjCoreServices initialized successfully' in lcontent:
                initialized = True
                quiescence_time = time.time() - start_t
                break
            if "SyntaxError" in lcontent:
                has_syntax_error = True

    kill_hpclick_processes()

    if not initialized:
        log_error("verify failed: Application did not reach successful initialization milestone within 15 seconds")
        sys.exit(1)

    if has_syntax_error:
        log_error("verify failed: Found V8 SyntaxError in application startup log")
        sys.exit(1)

    log_info(f"   -> Smoke launch PASSED! Time to initialization quiescence: {quiescence_time:.2f}s")
    log_info("ALL VERIFICATION CHECKS PASSED SUCCESSFULLY!")

def cmd_install(args):
    log_info("=== Installing HP Click arm64 Build ===")
    target_app = os.path.abspath(args.app if args.app else "/Applications/HP Click.app")
    backup_app = "/Applications/HP Click (x86_64 Backup).app"
    built_app = os.path.abspath(os.path.join(args.out, "HP Click.app"))

    if not os.path.exists(built_app):
        log_error(f"install failed: Built app not found at '{built_app}'. Run `./repack.py build` first.")
        sys.exit(1)

    if args.dry_run:
        log_info(f"[DRY RUN] Would backup '{target_app}' to '{backup_app}' and install '{built_app}'")
        return

    kill_hpclick_processes()

    if os.path.exists(target_app) and not os.path.exists(backup_app):
        # Only ever label something the x86_64 backup if it actually is one.
        # Without this, a second `install` run -- or a run after the backup has
        # been moved -- copies the arm64 repack over the name reserved for HP's
        # original, destroying the only untouched copy.
        target_main = os.path.join(target_app, "Contents", "MacOS", "HPClickExe")
        archs = run_cmd(["lipo", "-archs", target_main], check=False).split()
        if "x86_64" not in archs:
            log_error(
                f"install failed: refusing to create '{backup_app}' from a bundle whose "
                f"HPClickExe is '{' '.join(archs) or 'unreadable'}' — that is not the stock "
                f"x86_64 build, and backing it up under that name would destroy your only "
                f"copy of the original.\nMove your real backup into place, or pass an explicit "
                f"--app target.")
            sys.exit(1)
        log_info(f"Creating backup of original app at: {backup_app}")
        run_cmd(["ditto", target_app, backup_app])

    log_info(f"Installing arm64 build to: {target_app}")
    if os.path.exists(target_app):
        shutil.rmtree(target_app)

    run_cmd(["ditto", built_app, target_app])

    log_info("Re-signing installed bundle to update LaunchServices seal...")
    ent_path = os.path.join(args.out, "entitlements.plist")
    if not os.path.exists(ent_path):
        ent_path = "/tmp/entitlements.plist"
        with open(ent_path, "w", encoding="utf-8") as f:
            f.write(ENTITLEMENTS_XML)

    run_cmd(["codesign", "--force", "--sign", "-", "--options", "runtime", "--entitlements", ent_path, target_app])
    run_cmd(["xattr", "-dr", "com.apple.quarantine", target_app], check=False)
    
    lsreg = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
    if os.path.exists(lsreg):
        run_cmd([lsreg, "-f", target_app], check=False)

    log_info("INSTALLATION COMPLETED SUCCESSFULLY!")

def cmd_restore(args):
    log_info("=== Restoring Original HP Click Backup ===")
    target_app = os.path.abspath(args.app if args.app else "/Applications/HP Click.app")
    backup_app = "/Applications/HP Click (x86_64 Backup).app"

    if not os.path.exists(backup_app):
        log_error(f"restore failed: Backup app not found at '{backup_app}'")
        sys.exit(1)

    if args.dry_run:
        log_info(f"[DRY RUN] Would restore '{backup_app}' to '{target_app}'")
        return

    kill_hpclick_processes()

    log_info(f"Restoring backup app to: {target_app}")
    if os.path.exists(target_app):
        shutil.rmtree(target_app)

    run_cmd(["ditto", backup_app, target_app])

    ent_path = "/tmp/entitlements.plist"
    with open(ent_path, "w", encoding="utf-8") as f:
        f.write(ENTITLEMENTS_XML)

    run_cmd(["codesign", "--force", "--sign", "-", "--options", "runtime", "--entitlements", ent_path, target_app])
    run_cmd(["xattr", "-dr", "com.apple.quarantine", target_app], check=False)

    lsreg = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
    if os.path.exists(lsreg):
        run_cmd([lsreg, "-f", target_app], check=False)

    log_info("RESTORE COMPLETED SUCCESSFULLY!")

# --- Main Entry Point ---

def main():
    base_parser = argparse.ArgumentParser(add_help=False)
    base_parser.add_argument("--source", default="/Applications/HP Click.app", help="Path to source HP Click 4.8.117 app")
    base_parser.add_argument("--out", default="./build", help="Output directory for build staging and artifacts")
    base_parser.add_argument("--dry-run", action="store_true", help="Simulate actions without writing")
    base_parser.add_argument("--yes", "-y", action="store_true", help="Auto-confirm all prompts")
    base_parser.add_argument("--preload-idn2", action="store_true", default=True, help="Enable DYLD_INSERT_LIBRARIES for libidn2 (default: True)")
    base_parser.add_argument("--fake-version", action="store_true", default=False, help="Set package.json version to 99.99.999 (default: False)")

    parser = argparse.ArgumentParser(parents=[base_parser], description="hpclick-arm64 — Scripted Apple Silicon repack of HP Click 4.8.117")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sp_preflight = subparsers.add_parser("preflight", parents=[base_parser], help="Check environment & dependencies")
    sp_preflight.set_defaults(func=cmd_preflight)

    sp_build = subparsers.add_parser("build", parents=[base_parser], help="Build the native arm64 bundle in staging dir")
    sp_build.set_defaults(func=cmd_build)

    sp_verify = subparsers.add_parser("verify", parents=[base_parser], help="Run structural, signature, and smoke verification tests")
    sp_verify.add_argument("--app", help="Path to app bundle to verify")
    sp_verify.set_defaults(func=cmd_verify)

    sp_install = subparsers.add_parser("install", parents=[base_parser], help="Back up original app and install arm64 build to /Applications")
    sp_install.add_argument("--app", help="Target installation path")
    sp_install.set_defaults(func=cmd_install)

    sp_restore = subparsers.add_parser("restore", parents=[base_parser], help="Restore original x86_64 backup app")
    sp_restore.add_argument("--app", help="Target app path to restore")
    sp_restore.set_defaults(func=cmd_restore)

    args = parser.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()
