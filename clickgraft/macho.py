"""
clickgraft.macho — Mach-O binary tools (lipo, otool, nm, codesign wrappers).
Includes scanning for arm64-only, flat-namespace, unsatisfied symbols.
Target: Python 3.9+ (Standard Library only)
"""

import os
import subprocess


def run_cmd(cmd, check=True):
    try:
        res = subprocess.run(cmd, check=check, capture_output=True, text=True, errors="ignore")
        return res.stdout.strip()
    except subprocess.CalledProcessError as e:
        if check:
            raise RuntimeError(f"Command failed: {' '.join(cmd)}\nStderr: {e.stderr}")
        return ""


def is_macho(path):
    if not os.path.isfile(path) or os.path.islink(path):
        return False
    res = run_cmd(["file", path], check=False)
    return "Mach-O" in res and "CodeResources" not in path


def get_archs(path):
    if not is_macho(path):
        return []
    out = run_cmd(["lipo", "-archs", path], check=False)
    if not out:
        return []
    return out.split()


def get_load_dylibs(path):
    if not is_macho(path):
        return []
    out = run_cmd(["otool", "-L", path], check=False)
    lines = out.splitlines()[1:]
    dylibs = []
    for line in lines:
        line = line.strip()
        if line:
            dylibs.append(line.split()[0])
    return dylibs


def get_rpaths(path):
    if not is_macho(path):
        return []
    out = run_cmd(["otool", "-l", path], check=False)
    rpaths = []
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if "cmd LC_RPATH" in line:
            for j in range(i, min(i + 5, len(lines))):
                if "path " in lines[j]:
                    rp = lines[j].split("path ")[1].split(" (offset")[0].strip()
                    rpaths.append(rp)
    return rpaths


def find_bundle_exported_symbols(bundle_path):
    """
    Scans all Mach-O binaries in bundle (dylib, framework, .node, Electron Framework)
    and collects all exported global symbols (nm -arch arm64 -gU).
    """
    exports = set()
    for root, dirs, files in os.walk(bundle_path):
        for f in files:
            fp = os.path.join(root, f)
            if is_macho(fp):
                archs = get_archs(fp)
                if "arm64" in archs:
                    out = run_cmd(["nm", "-arch", "arm64", "-gU", fp], check=False)
                    for line in out.splitlines():
                        parts = line.strip().split()
                        if len(parts) >= 3 and parts[1] in ("T", "D", "B", "R", "S", "C"):
                            exports.add(parts[2])
    return exports


def find_unsatisfied_arm64_symbols(macho_path, bundle_exports=None):
    """
    Scans a universal Mach-O binary for symbols that satisfy all three D2 filters:
    1. arm64-only: symbol undefined in arm64 slice but NOT in x86_64 slice
    2. flat-namespace: marked (dynamically looked up) in nm -arch arm64 -m
    3. unsatisfied: not exported by any binary/dylib/framework in the bundle (including Electron Framework)
    """
    archs = get_archs(macho_path)
    if "arm64" not in archs or "x86_64" not in archs:
        return []

    # Filter 1: Undefined in x86_64 slice
    x86_out = run_cmd(["nm", "-arch", "x86_64", "-u", macho_path], check=False)
    x86_syms = set(line.strip().split()[-1] for line in x86_out.splitlines() if line.strip())

    # Filter 2: Flat-namespace (dynamically looked up) in arm64 slice
    arm64_m_out = run_cmd(["nm", "-arch", "arm64", "-m", macho_path], check=False)
    flat_syms = set()
    for line in arm64_m_out.splitlines():
        if "(dynamically looked up)" in line:
            left = line.split("(dynamically looked up)")[0].strip()
            parts = left.split()
            if parts:
                flat_syms.add(parts[-1])

    arm64_only_flat = flat_syms - x86_syms

    # Filter 3: Not in bundle exports
    if bundle_exports is not None:
        unsatisfied = arm64_only_flat - bundle_exports
    else:
        unsatisfied = arm64_only_flat

    return sorted(list(unsatisfied))
