"""
clickgraft.probe — Unknown-version analyzer & manifest generator.
Scans an unmanifested HP Click app bundle to generate a draft manifest and report.
Uses 3-filter symbol analysis (arm64-only, flat-namespace, unsatisfied by bundle exports).
Target: Python 3.9+ (Standard Library only)
"""

import hashlib
import json
import os
import plistlib
from clickgraft.asar import AsarArchive
from clickgraft.macho import (
    find_bundle_exported_symbols,
    find_unsatisfied_arm64_symbols,
    get_archs,
    is_macho
)


def probe_app_bundle(source_app_path):
    """
    Analyzes an HP Click bundle and returns (draft_manifest_dict, report_str).
    """
    source_app_path = os.path.abspath(source_app_path)
    if not os.path.exists(source_app_path):
        raise ValueError(f"Source app path does not exist: {source_app_path}")

    asar_path = os.path.join(source_app_path, "Contents", "Resources", "app.asar")
    if not os.path.exists(asar_path):
        raise ValueError(f"Source app does not contain Resources/app.asar: {source_app_path}")

    # 1. Read app version and asar entries
    archive = AsarArchive(asar_path)
    packed_count, unpacked_count = archive.count_entries()

    # Read package.json from asar
    all_nodes = archive.get_all_file_nodes()
    pkg_node = all_nodes.get("package.json")
    if not pkg_node:
        raise ValueError("Could not find package.json in source app.asar")

    pkg_bytes = archive.read_file_content(pkg_node)
    pkg_data = json.loads(pkg_bytes.decode("utf-8"))
    app_version = pkg_data.get("version", "unknown")

    # Hash asar
    with open(asar_path, "rb") as f:
        asar_sha256 = hashlib.sha256(f.read()).hexdigest()

    # 2. Read Electron version from Electron Framework.framework Info.plist
    el_plist_path = os.path.join(
        source_app_path,
        "Contents",
        "Frameworks",
        "Electron Framework.framework",
        "Resources",
        "Info.plist"
    )
    electron_version = None
    if os.path.exists(el_plist_path):
        with open(el_plist_path, "rb") as pf:
            plist = plistlib.load(pf)
            electron_version = plist.get("CFBundleVersion") or plist.get("CFBundleShortVersionString")

    if not electron_version:
        raise ValueError("Could not determine Electron version from Electron Framework.framework/Resources/Info.plist")

    # 3. Check standard patch anchors
    patch_anchors = [
        ("package.json", "hp_configs.crashAutoSubmit", None),
        ("app/node/main/app-updater.js", "function startup(e){", "function startup(e){return;"),
        ("app/shared/constants.js", "export var SharedConstants;", "var SharedConstants;"),
        ("app/shared/industries.js", "export const Industries = [", "const Industries = [")
    ]

    patches = []
    patches.append({
        "path": "package.json",
        "why": "hp_configs.crashAutoSubmit stays true otherwise",
        "ops": [{ "type": "json_set", "path": "hp_configs.crashAutoSubmit", "value": False }]
    })

    anchor_report = []
    for rel_path, anchor, replacement in patch_anchors[1:]:
        node = all_nodes.get(rel_path)
        if node:
            content = archive.read_file_content(node).decode("utf-8")
            count = content.count(anchor)
            anchor_report.append(f"  - {rel_path}: anchor '{anchor}' occurred {count} time(s)")
            if count == 1:
                ops = [{ "type": "replace", "anchor": anchor, "replacement": replacement }]
                if rel_path in ("app/shared/constants.js", "app/shared/industries.js"):
                    export_name = "SharedConstants" if "constants" in rel_path else "Industries"
                    ops.append({ "type": "append", "text": f"\nif (typeof exports !== 'undefined') {{ exports.{export_name} = {export_name}; }}\n" })
                patches.append({
                    "path": rel_path,
                    "why": f"Patch for {rel_path}",
                    "ops": ops
                })

    # 4. Scan Mach-O binaries for x86_64-only binaries and unsatisfied arm64 symbols
    x86_only = []
    undefined_sym_map = {}

    # Collect exported symbols across entire bundle (including Electron Framework and all dylibs)
    bundle_exports = find_bundle_exported_symbols(source_app_path)

    for root, dirs, files in os.walk(source_app_path):
        for f in files:
            fp = os.path.join(root, f)
            if is_macho(fp):
                archs = get_archs(fp)
                rel_p = os.path.relpath(fp, source_app_path)
                if archs == ["x86_64"]:
                    if rel_p.startswith("Contents/Frameworks/"):
                        continue
                    if "Electron Framework" not in rel_p and "Electron Helper" not in rel_p and "HP Click Helper" not in rel_p and rel_p != "Contents/MacOS/HPClickExe":
                        x86_only.append(rel_p)
                elif "arm64" in archs and "x86_64" in archs:
                    undef_syms = find_unsatisfied_arm64_symbols(fp, bundle_exports)
                    if undef_syms:
                        undefined_sym_map[rel_p] = undef_syms

    # 5. Map undefined symbols to required dylibs
    required_dylibs = []
    has_idn2 = False
    has_nghttp2 = False

    for sym_list in undefined_sym_map.values():
        for s in sym_list:
            if "_idn2_" in s or "idn2_" in s:
                has_idn2 = True
            if "_nghttp2_" in s or "nghttp2_" in s:
                has_nghttp2 = True

    if has_idn2:
        required_dylibs.append({
            "name": "libidn2.0.dylib",
            "brew_formula": "libidn2",
            "why": "DjCore/ConnServicesNative undefined _idn2_ symbols in arm64 slice",
            "preload": True
        })
        required_dylibs.append({ "name": "libunistring.5.dylib", "brew_formula": "libunistring", "why": "dependency of libidn2", "preload": False })
        required_dylibs.append({ "name": "libintl.8.dylib", "brew_formula": "gettext", "why": "dependency of libidn2", "preload": False })

    if has_nghttp2:
        required_dylibs.append({
            "name": "libnghttp2.14.dylib",
            "brew_formula": "nghttp2",
            "why": "DjCore/ConnServicesNative undefined _nghttp2_ symbols in arm64 slice",
            "preload": False
        })

    # Generate draft manifest
    draft_manifest = {
        "app_version": app_version,
        "asar_sha256": asar_sha256,
        "electron_version": electron_version,
        "asar_entries": { "packed": packed_count, "unpacked": unpacked_count },
        "patches": patches,
        "required_dylibs": required_dylibs,
        "expected_x86_only": x86_only,
        "verified_by": "Generated by clickgraft probe",
        "verified_on": "draft"
    }

    # Generate concise human readable report
    report_lines = [
        f"=== Probe Report for {source_app_path} ===",
        f"App Version: {app_version}",
        f"Electron Version: {electron_version}",
        f"ASAR SHA-256: {asar_sha256}",
        f"ASAR Entries: {packed_count} packed, {unpacked_count} unpacked",
        "",
        "--- Patch Anchor Status ---"
    ]
    report_lines.extend(anchor_report)
    report_lines.extend([
        "",
        f"--- Mach-O Audit ---",
        f"x86_64-only binaries (HP proprietary): {len(x86_only)}"
    ])
    for x in x86_only:
        report_lines.append(f"  - {x}")

    report_lines.append(f"\nBinaries with arm64-only unsatisfied symbols: {len(undefined_sym_map)}")
    for bin_p, syms in undefined_sym_map.items():
        report_lines.append(f"  - {bin_p} ({len(syms)} symbols): {', '.join(syms[:5])}{'...' if len(syms) > 5 else ''}")

    report_str = "\n".join(report_lines)
    return draft_manifest, report_str
