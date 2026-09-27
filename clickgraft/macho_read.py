"""
clickgraft.macho_read — Reading Mach-O files without Apple's command-line tools.

WHY THIS EXISTS. Everything ClickGraft asked the Command Line Tools to *read* --
`lipo -archs`, `otool -L`, `otool -l`, `vtool -show-build`, `nm` -- is a walk over
a Mach-O's header, its load commands, or its symbol table. None of it needs a
compiler. It only needed the tools because parsing text was quicker to write than
parsing structs, and the price was a hard dependency on a developer toolchain that
most people making a copy have no other use for. Measured 27 September 2026 on
macOS 27: `lipo`, `otool`, `nm`, `vtool`, `install_name_tool`, `clang` and
`python3` are all the same 200,560-byte inode with 78 hard links -- one `xcrun`
shim -- while `codesign`, `ditto`, `file` and `xattr` are separate real binaries
that work with `DEVELOPER_DIR` pointed at an empty directory. So six of the seven
shims this project used can go, and the reading half is where they go.

IT IS ALSO MORE CORRECT THAN THE TOOLS IT REPLACES, in three measured ways:

  * `otool` applies `archive(member)` syntax to its argument, so a path ending in
    `)` is truncated and it exits 1. HP ships three: `HP Click Helper (GPU)`,
    `(Plugin)` and `(Renderer)`. get_load_dylibs() and get_rpaths() returned []
    for all three, so verify's Homebrew-leak audit was silently skipping them.
  * The old parser split `otool -L` output on whitespace, so
    `@rpath/Electron Framework.framework/Electron Framework` became
    `@rpath/Electron`.
  * The same split read `otool`'s per-architecture header lines as dependencies,
    inventing one named after the bundle path on every universal file.

WHAT IT DELIBERATELY DOES NOT DO. It does not write. Rewriting load commands is a
separate job with a separate hazard (a longer path does not fit where a shorter
one was), and it is not in this file. `codesign`, `ditto` and `file` stay as
subprocesses because they are base-OS and there is nothing to gain.

Target: Python 3.9+ (Standard Library only)
"""

import struct

# Fat headers are big-endian on disk whatever the slices inside them are.
FAT_MAGIC = 0xCAFEBABE
FAT_MAGIC_64 = 0xCAFEBABF
MH_MAGIC = 0xFEEDFACE
MH_CIGAM = 0xCEFAEDFE
MH_MAGIC_64 = 0xFEEDFACF
MH_CIGAM_64 = 0xCFFAEDFE

LC_REQ_DYLD = 0x80000000
LC_SYMTAB = 0x02
LC_LOAD_DYLIB = 0x0C
LC_ID_DYLIB = 0x0D
LC_LOAD_WEAK_DYLIB = 0x18 | LC_REQ_DYLD
LC_REEXPORT_DYLIB = 0x1F | LC_REQ_DYLD
LC_LOAD_UPWARD_DYLIB = 0x23 | LC_REQ_DYLD
LC_RPATH = 0x1C | LC_REQ_DYLD
LC_VERSION_MIN_MACOSX = 0x24
LC_BUILD_VERSION = 0x32

_DYLIB_COMMANDS = frozenset({
    LC_ID_DYLIB, LC_LOAD_DYLIB, LC_LOAD_WEAK_DYLIB,
    LC_REEXPORT_DYLIB, LC_LOAD_UPWARD_DYLIB,
})

# LC_BUILD_VERSION carries a platform, and only macOS counts here. A zippered
# library also declares PLATFORM_MACCATALYST, whose minimum says nothing about
# what macOS the file needs -- reading it as one is how a 13.1 library starts
# claiming it needs 16.2.
PLATFORM_MACOS = 1

CPU_ARCH_ABI64 = 0x01000000
CPU_ARCH_ABI64_32 = 0x02000000
CPU_TYPE_X86 = 7
CPU_TYPE_ARM = 12
CPU_TYPE_POWERPC = 18
CPU_SUBTYPE_MASK = 0xFF000000

# Named exactly as `lipo -archs` prints them, because the point of this table is
# that callers cannot tell which implementation answered.
_ARCH_NAMES = {
    (CPU_TYPE_X86, 3): "i386",
    (CPU_TYPE_X86 | CPU_ARCH_ABI64, 3): "x86_64",
    (CPU_TYPE_X86 | CPU_ARCH_ABI64, 8): "x86_64h",
    (CPU_TYPE_ARM, 0): "arm",
    (CPU_TYPE_ARM, 6): "armv6",
    (CPU_TYPE_ARM, 9): "armv7",
    (CPU_TYPE_ARM, 11): "armv7s",
    (CPU_TYPE_ARM, 12): "armv7k",
    (CPU_TYPE_ARM | CPU_ARCH_ABI64, 0): "arm64",
    (CPU_TYPE_ARM | CPU_ARCH_ABI64, 1): "arm64v8",
    (CPU_TYPE_ARM | CPU_ARCH_ABI64, 2): "arm64e",
    (CPU_TYPE_ARM | CPU_ARCH_ABI64_32, 1): "arm64_32",
    (CPU_TYPE_POWERPC, 0): "ppc",
    (CPU_TYPE_POWERPC | CPU_ARCH_ABI64, 0): "ppc64",
}

# nlist_64: n_strx u32, n_type u8, n_sect u8, n_desc u16, n_value u64
N_STAB = 0xE0
N_TYPE = 0x0E
N_EXT = 0x01
N_UNDF = 0x00

# An undefined symbol's library ordinal lives in the top byte of n_desc. This
# value is what `nm -m` prints as "(dynamically looked up)": the two-level
# namespace is off for that symbol, so dyld searches every loaded image instead
# of a named one. That is the class of symbol that crashed a copy here --
# png_init_filter_functions_neon bound to nothing and the app died on import --
# which is why verify looks for them at all.
DYNAMIC_LOOKUP_ORDINAL = 0xFE


class MachOError(Exception):
    """The file is not a Mach-O, or is truncated past sensible recovery."""


def _arch_name(cputype, cpusubtype):
    """Mask to 32 bits first, and do it unsigned.

    Every main executable HP ships sets CPU_SUBTYPE_LIB64 (0x80000000) in its
    subtype, which read as a signed int is negative -- and Python's ints are
    arbitrary precision, so `negative & ~0xFF000000` is not the 32-bit masking
    the C headers mean. The first differential run caught this as six files
    whose x86_64 slice came back "unknown(16777223,-4294967293)": HPClickExe,
    ShipIt, chrome_crashpad_handler and the three HP Click Helpers.
    """
    cputype &= 0xFFFFFFFF
    subtype = cpusubtype & 0xFFFFFFFF & ~CPU_SUBTYPE_MASK
    return _ARCH_NAMES.get((cputype, subtype), f"unknown({cputype},{subtype})")


def slices(data):
    """[(arch, offset, size)] for each Mach-O in `data`, fat or thin.

    A thin file yields one slice at offset 0 whose arch still comes from its own
    header, so callers need no special case.
    """
    if len(data) < 8:
        raise MachOError("too short to hold a Mach-O header")

    magic = struct.unpack_from(">I", data, 0)[0]
    if magic in (FAT_MAGIC, FAT_MAGIC_64):
        wide = magic == FAT_MAGIC_64
        nfat = struct.unpack_from(">I", data, 4)[0]
        # A Java .class file opens with the same CAFEBABE. Its next two bytes are
        # a minor version, which gives absurd slice counts; a real fat binary has
        # a handful. The old is_macho() leaned on `file` for this, and still does.
        if nfat > 64:
            raise MachOError("CAFEBABE with an implausible slice count (Java class?)")
        out = []
        entry = ">2i2Q2I" if wide else ">4I2I"
        step = 32 if wide else 20
        for i in range(nfat):
            base = 8 + i * step
            if wide:
                cputype, cpusubtype, off, size = struct.unpack_from(">iiQQ", data, base)
            else:
                cputype, cpusubtype, off, size = struct.unpack_from(">iiII", data, base)
            out.append((_arch_name(cputype, cpusubtype), off, size))
        return out

    magic_le = struct.unpack_from("<I", data, 0)[0]
    if magic_le in (MH_MAGIC, MH_MAGIC_64):
        endian = "<"
    elif magic_le in (MH_CIGAM, MH_CIGAM_64):
        endian = ">"
    else:
        raise MachOError(f"not a Mach-O (magic {magic_le:#010x})")
    cputype, cpusubtype = struct.unpack_from(endian + "ii", data, 4)
    return [(_arch_name(cputype, cpusubtype), 0, len(data))]


def _header(data, offset):
    """(endian, is64, ncmds, header_size) for the slice starting at `offset`."""
    magic = struct.unpack_from("<I", data, offset)[0]
    if magic == MH_MAGIC_64:
        endian, is64 = "<", True
    elif magic == MH_CIGAM_64:
        endian, is64 = ">", True
    elif magic == MH_MAGIC:
        endian, is64 = "<", False
    elif magic == MH_CIGAM:
        endian, is64 = ">", False
    else:
        raise MachOError(f"slice at {offset} has no Mach-O magic ({magic:#010x})")
    ncmds = struct.unpack_from(endian + "I", data, offset + 16)[0]
    return endian, is64, ncmds, (32 if is64 else 28)


def load_commands(data, offset):
    """Yield (cmd, cmdsize, body_offset) for each load command in one slice.

    Stops rather than raising on a command that runs past the buffer: a
    truncated file should degrade to what could be read, not take the build down.
    """
    endian, _is64, ncmds, hsize = _header(data, offset)
    pos = offset + hsize
    for _ in range(ncmds):
        if pos + 8 > len(data):
            return
        cmd, cmdsize = struct.unpack_from(endian + "II", data, pos)
        if cmdsize < 8 or pos + cmdsize > len(data):
            return
        yield cmd, cmdsize, pos
        pos += cmdsize


def _lc_string(data, endian, cmd_off, cmdsize):
    """The path a dylib/rpath command carries, at its lc_str offset."""
    str_off = struct.unpack_from(endian + "I", data, cmd_off + 8)[0]
    if str_off >= cmdsize:
        return ""
    start = cmd_off + str_off
    end = cmd_off + cmdsize
    raw = data[start:end]
    nul = raw.find(b"\x00")
    if nul != -1:
        raw = raw[:nul]
    return raw.decode("utf-8", "replace")


def read(path):
    """The whole file. Mach-Os here are at most a few hundred MB and are walked
    several times over; one read beats reopening per question."""
    with open(path, "rb") as f:
        return f.read()


def archs(data):
    return [name for name, _off, _size in slices(data)]


def dylib_paths(data):
    """Every install name and dependency, across all slices, in file order.

    Includes LC_ID_DYLIB, which is what `otool -L` does too: its first line for a
    dylib is that dylib's own install name, and build.py's Homebrew rewrite has
    always depended on seeing it.
    """
    seen, out = set(), []
    for _name, off, _size in slices(data):
        endian, _is64, _n, _h = _header(data, off)
        for cmd, cmdsize, cmd_off in load_commands(data, off):
            if cmd in _DYLIB_COMMANDS:
                p = _lc_string(data, endian, cmd_off, cmdsize)
                if p and p not in seen:
                    seen.add(p)
                    out.append(p)
    return out


def rpaths(data):
    seen, out = set(), []
    for _name, off, _size in slices(data):
        endian, _is64, _n, _h = _header(data, off)
        for cmd, cmdsize, cmd_off in load_commands(data, off):
            if cmd == LC_RPATH:
                p = _lc_string(data, endian, cmd_off, cmdsize)
                if p and p not in seen:
                    seen.add(p)
                    out.append(p)
    return out


def _format_version(packed):
    """vtool's spelling of a packed X.Y.Z, so callers parse one format only."""
    major, minor, patch = packed >> 16, (packed >> 8) & 0xFF, packed & 0xFF
    return f"{major}.{minor}" if patch == 0 else f"{major}.{minor}.{patch}"


def minimum_versions(data):
    """{arch: "12.0" or None} -- the macOS minimum each slice declares.

    A thin file's one slice is keyed "", matching what vtool's output gave and
    what macos_floor has always been handed. The highest wins within a slice: a
    file can carry both LC_VERSION_MIN_MACOSX and LC_BUILD_VERSION.
    """
    all_slices = slices(data)
    thin = len(all_slices) == 1 and all_slices[0][1] == 0
    out = {}
    for name, off, _size in all_slices:
        key = "" if thin else name
        out.setdefault(key, None)
        endian, _is64, _n, _h = _header(data, off)
        best = None
        for cmd, cmdsize, cmd_off in load_commands(data, off):
            packed = None
            if cmd == LC_BUILD_VERSION and cmdsize >= 24:
                platform, minos = struct.unpack_from(endian + "II", data, cmd_off + 8)
                if platform == PLATFORM_MACOS:
                    packed = minos
            elif cmd == LC_VERSION_MIN_MACOSX and cmdsize >= 16:
                packed = struct.unpack_from(endian + "I", data, cmd_off + 8)[0]
            if packed is not None and (best is None or packed > best):
                best = packed
        if best is not None:
            out[key] = _format_version(best)
    return out


def _symtab(data, off, endian, is64):
    """(symoff, nsyms, stroff, strsize) for a slice, or None when it has none."""
    for cmd, cmdsize, cmd_off in load_commands(data, off):
        if cmd == LC_SYMTAB and cmdsize >= 24:
            return struct.unpack_from(endian + "IIII", data, cmd_off + 8)
    return None


def _symbols(data, arch):
    """Yield (name, n_type, n_desc) for one architecture's symbol table."""
    for name, off, _size in slices(data):
        if name != arch:
            continue
        endian, is64, _n, _h = _header(data, off)
        tab = _symtab(data, off, endian, is64)
        if tab is None:
            return
        symoff, nsyms, stroff, strsize = tab
        # Offsets in a fat file's load commands are relative to the slice.
        symbase, strbase = off + symoff, off + stroff
        width = 16 if is64 else 12
        strings = data[strbase:strbase + strsize]
        fmt = endian + ("IBBHQ" if is64 else "IBBhI")
        for i in range(nsyms):
            rec = symbase + i * width
            if rec + width > len(data):
                return
            n_strx, n_type, _n_sect, n_desc, _n_value = struct.unpack_from(fmt, data, rec)
            if n_strx == 0 or n_strx >= len(strings):
                continue
            end = strings.find(b"\x00", n_strx)
            if end == -1:
                continue
            yield strings[n_strx:end].decode("utf-8", "replace"), n_type, n_desc
        return


def flat_undefined_symbols(data, arch="arm64"):
    """Undefined symbols looked up in the flat namespace -- `nm -m -u`'s
    "(dynamically looked up)". Names keep their leading underscore, as nm prints
    them, because every caller here compares them against nm-shaped sets."""
    out = set()
    for name, n_type, n_desc in _symbols(data, arch):
        if n_type & N_STAB:
            continue
        if (n_type & N_TYPE) != N_UNDF:
            continue
        if ((n_desc >> 8) & 0xFF) == DYNAMIC_LOOKUP_ORDINAL:
            out.add(name)
    return out


def undefined_symbols(data, arch="arm64"):
    """Every undefined symbol -- `nm -u`, whatever the namespace."""
    return {name for name, n_type, _d in _symbols(data, arch)
            if not (n_type & N_STAB) and (n_type & N_TYPE) == N_UNDF}


def defined_global_symbols(data, arch="arm64"):
    """External symbols this slice defines -- `nm -g --defined-only`."""
    return {name for name, n_type, _d in _symbols(data, arch)
            if not (n_type & N_STAB) and (n_type & N_EXT)
            and (n_type & N_TYPE) != N_UNDF}
