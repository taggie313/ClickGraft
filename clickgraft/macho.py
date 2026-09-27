"""
clickgraft.macho — What a Mach-O declares: architectures, dependencies, symbols.

These questions used to be answered by shelling out to `lipo`, `otool` and `nm`.
They are answered by clickgraft/macho_read.py now, which parses the headers
directly, because those three tools are not tools -- they are hard links to one
`xcrun` shim, and so is `python3`. Dropping them is most of what it takes for
ClickGraft to stop demanding a developer toolchain from people who only want to
make a copy of an app. The public names and return shapes here did not change,
so nothing that imports this file had to.

The swap was gated on a differential, not on confidence: every question below,
asked of every Mach-O in stock 4.8.117, 4.8.118 and 4.11.31 and in a grafted
copy -- about 440 files -- answered identically to the tool it replaces, on
27 September 2026. The one asymmetry is the three files `otool` cannot read at
all; see get_load_dylibs.

`file` and `codesign` are still subprocesses on purpose: unlike the others they
are real base-OS binaries, measured to work with DEVELOPER_DIR pointing nowhere.

Target: Python 3.9+ (Standard Library only)
"""

import os
import subprocess

from clickgraft import macho_read


def run_cmd(cmd, check=True):
    try:
        res = subprocess.run(cmd, check=check, capture_output=True, text=True, errors="ignore")
        return res.stdout.strip()
    except subprocess.CalledProcessError as e:
        if check:
            raise RuntimeError(f"Command failed: {' '.join(cmd)}\nStderr: {e.stderr}")
        return ""


_MACHO_MAGICS = frozenset({
    b"\xce\xfa\xed\xfe",  # MH_MAGIC     32-bit, little-endian
    b"\xfe\xed\xfa\xce",  # MH_CIGAM     32-bit, big-endian
    b"\xcf\xfa\xed\xfe",  # MH_MAGIC_64  64-bit, little-endian
    b"\xfe\xed\xfa\xcf",  # MH_CIGAM_64  64-bit, big-endian
    b"\xca\xfe\xba\xbe",  # FAT_MAGIC    also Java .class - ambiguous
    b"\xbe\xba\xfe\xca",  # FAT_CIGAM
    b"\xca\xfe\xba\xbf",  # FAT_MAGIC_64
    b"\xbf\xba\xfe\xca",  # FAT_CIGAM_64
})


def _could_be_macho(path):
    """False only when the first four bytes rule Mach-O out entirely."""
    try:
        with open(path, "rb") as f:
            header = f.read(4)
        if len(header) < 4:
            return False
        return header in _MACHO_MAGICS
    except OSError:
        return True


def is_macho(path):
    if not os.path.isfile(path) or os.path.islink(path):
        return False
    if not _could_be_macho(path):
        return False
    res = run_cmd(["file", path], check=False)
    return "Mach-O" in res and "CodeResources" not in path


def _read(path):
    """Bytes of a Mach-O, or None when it is not one or cannot be read."""
    if not is_macho(path):
        return None
    try:
        return macho_read.read(path)
    except OSError:
        return None


def get_archs(path):
    data = _read(path)
    if data is None:
        return []
    try:
        return macho_read.archs(data)
    except macho_read.MachOError:
        return []


def get_load_dylibs(path):
    """Install name and dependencies, in file order.

    Three of HP's binaries could never be read here before. `otool` treats an
    argument ending in `)` as archive(member) syntax, truncates it, and exits 1 --
    so `HP Click Helper (GPU)`, `(Plugin)` and `(Renderer)` returned [], and
    verify's Homebrew-leak audit passed them by default rather than on evidence.
    Two narrower bugs went with it: splitting otool's output on whitespace cut
    `@rpath/Electron Framework.framework/Electron Framework` down to
    `@rpath/Electron`, and read otool's per-architecture header lines as a
    dependency named after the bundle.
    """
    data = _read(path)
    if data is None:
        return []
    try:
        return macho_read.dylib_paths(data)
    except macho_read.MachOError:
        return []


def get_rpaths(path):
    data = _read(path)
    if data is None:
        return []
    try:
        return macho_read.rpaths(data)
    except macho_read.MachOError:
        return []


def find_bundle_exported_symbols(bundle_path):
    """Every global symbol the arm64 side of a bundle defines.

    The set a grafted copy is checked against: a flat-namespace symbol that
    nothing in the bundle exports binds to NULL at launch, which is how
    png_init_filter_functions_neon killed the app on PNG import.
    """
    exports = set()
    for root, _dirs, files in os.walk(bundle_path):
        for f in files:
            data = _read(os.path.join(root, f))
            if data is None:
                continue
            try:
                if "arm64" not in macho_read.archs(data):
                    continue
                exports |= macho_read.defined_global_symbols(data, "arm64")
            except macho_read.MachOError:
                continue
    return exports


def find_unsatisfied_arm64_symbols(macho_path, bundle_exports=None):
    """
    Scans a universal Mach-O binary for symbols that satisfy all three D2 filters:
    1. arm64-only: symbol undefined in arm64 slice but NOT in x86_64 slice
    2. flat-namespace: library ordinal DYNAMIC_LOOKUP in the arm64 slice
    3. unsatisfied: not exported by any binary/dylib/framework in the bundle (including Electron Framework)
    """
    data = _read(macho_path)
    if data is None:
        return []
    try:
        archs = macho_read.archs(data)
        if "arm64" not in archs or "x86_64" not in archs:
            return []
        x86_syms = macho_read.undefined_symbols(data, "x86_64")
        flat_syms = macho_read.flat_undefined_symbols(data, "arm64")
    except macho_read.MachOError:
        return []

    arm64_only_flat = flat_syms - x86_syms
    if bundle_exports is not None:
        return sorted(arm64_only_flat - bundle_exports)
    return sorted(arm64_only_flat)
