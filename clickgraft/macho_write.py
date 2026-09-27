"""
clickgraft.macho_write — Rewriting install names and rpaths without install_name_tool.

The other half of dropping Apple's Command Line Tools. clickgraft/macho_read.py
answered every question ClickGraft asked `lipo`, `otool`, `nm` and `vtool`; this
does the three things it asked `install_name_tool`:

    set_dylib_id(path, new)          install_name_tool -id
    change_dylib_path(path, old, new)  install_name_tool -change
    add_rpath(path, rpath)           install_name_tool -add_rpath

WHY IT CAN WORK AT ALL, WHICH IS NOT OBVIOUS. A load command holds its string
inline, so a longer path needs a longer command, and nothing after the load
commands can move -- every section offset, every symbol table offset and the
fat header's slice offsets are absolute. What makes it possible is that linkers
leave slack between the end of the load commands and the first section's data.
Measured on stock 4.8.117 and 4.11.31, 27 September 2026: the Qt5 rewrites need
25-29 bytes where 24 are in place, and 0 of 72 fit -- yet the affected binaries
carry 6,392 to 22,404 bytes of slack, and the APPE frameworks that need 32 bytes
for a new LC_RPATH carry 372 to 22,404. So the file never changes size, nothing
moves, and only the padding is consumed.

WHEN IT CANNOT, IT SAYS SO, and here it is genuinely weaker than Apple's tool.
install_name_tool can rebuild a file when the slack runs out -- relocating what
follows the load commands -- and this cannot; it only ever uses padding that is
already there. Measured across every Mach-O in stock 4.8.117, both refuse the
same 14 files, and on one, Electron Framework, Apple succeeds where this refuses
(9,624 bytes wanted against 4,064 of slack).

That gap does not touch what ClickGraft does, and the scope was measured rather
than assumed: over the files a build actually edits -- 39 APPE binaries per
bundle given @loader_path/.., the Qt5 dependency rewrites, and the four bundled
Homebrew dylibs given an id and @rpath dependencies -- there are ZERO refusals
in 4.8.117 and 4.11.31. If HP ever ships a binary with no room, a build stops
with a message naming the file and the shortfall instead of writing a broken one.

And on those files the output is not merely equivalent, it is BYTE-IDENTICAL to
what install_name_tool produces from the same input: 84 files across the two
bundles, zero differences, 27 September 2026. That is the claim worth having,
because identical bytes make runtime behaviour identical by construction -- which
is the only honest way to answer "but does the print engine still find its
frameworks?" without a plotter in the test suite.

WHAT IT DOES NOT DO. It does not remove commands, resize segments, or touch
anything past the load-command block. Any edit invalidates the file's code
signature, exactly as install_name_tool's does; callers re-sign afterwards.

Target: Python 3.9+ (Standard Library only)
"""

import struct

from clickgraft import macho_read
from clickgraft.macho_read import (
    LC_ID_DYLIB, LC_RPATH, MachOError, _header, load_commands, slices,
)

LC_SEGMENT = 0x01
LC_SEGMENT_64 = 0x19

# The four dylib commands whose path this module may rewrite. LC_ID_DYLIB is a
# dylib's own install name; the other three are dependencies.
_CHANGEABLE = (macho_read.LC_LOAD_DYLIB, macho_read.LC_LOAD_WEAK_DYLIB,
               macho_read.LC_REEXPORT_DYLIB, macho_read.LC_LOAD_UPWARD_DYLIB)


class MachOWriteError(MachOError):
    """The edit will not fit, or the file is not one this module can edit."""


def _align(n, is64):
    step = 8 if is64 else 4
    return (n + step - 1) // step * step


def _dylib_command(cmd, path, is64, timestamp=0, current=0x10000, compat=0x10000):
    """A dylib_command: 24 bytes of fields, then the path at offset 24."""
    raw = path.encode("utf-8") + b"\x00"
    size = _align(24 + len(raw), is64)
    body = struct.pack("<IIIIII", cmd, size, 24, timestamp, current, compat)
    return body + raw + b"\x00" * (size - 24 - len(raw))


def _rpath_command(path, is64):
    """An rpath_command: 12 bytes of fields, then the path at offset 12."""
    raw = path.encode("utf-8") + b"\x00"
    size = _align(12 + len(raw), is64)
    body = struct.pack("<III", LC_RPATH, size, 12)
    return body + raw + b"\x00" * (size - 12 - len(raw))


def _slack_limit(data, off, endian, is64):
    """How many bytes the header and load commands may occupy in this slice.

    The lowest file offset that holds real content is the ceiling: everything
    from there on sits at an absolute offset and must not move. Sections come
    first because a segment's fileoff can legitimately be 0 (__TEXT starts at
    the header). A file with neither falls back to the slice's own size, which
    is as permissive as this can honestly be.
    """
    candidates = []
    for cmd, cmdsize, cmd_off in load_commands(data, off):
        # segment_command_64: cmd 0, cmdsize 4, segname 8..24, vmaddr 24,
        # vmsize 32, fileoff 40, filesize 48, maxprot 56, initprot 60,
        # nsects 64, flags 68. section_64: sectname 0, segname 16, addr 32,
        # size 40, offset 48. The 32-bit forms are the same fields, 4 bytes
        # wide: fileoff 32, nsects 48; section size 36, offset 40.
        if cmd == LC_SEGMENT_64 and cmdsize >= 72:
            fileoff = struct.unpack_from(endian + "Q", data, cmd_off + 40)[0]
            nsects = struct.unpack_from(endian + "I", data, cmd_off + 64)[0]
            sect_base, sect_size, size_at = cmd_off + 72, 80, 40
            unpack = endian + "QI"
        elif cmd == LC_SEGMENT and cmdsize >= 56:
            fileoff = struct.unpack_from(endian + "I", data, cmd_off + 32)[0]
            nsects = struct.unpack_from(endian + "I", data, cmd_off + 48)[0]
            sect_base, sect_size, size_at = cmd_off + 56, 68, 36
            unpack = endian + "II"
        else:
            continue
        if fileoff:
            candidates.append(fileoff)
        for i in range(nsects):
            base = sect_base + i * sect_size
            if base + sect_size > len(data):
                break
            size, offset = struct.unpack_from(unpack, data, base + size_at)
            if size and offset:
                candidates.append(offset)
    return min(candidates) if candidates else None


def _rewrite_slice(data, off, transform, path_for_errors):
    """Apply `transform` to one slice's load commands, in place.

    transform(commands) -> new list of raw command bytes, where `commands` is a
    list of (cmd, raw_bytes). Returns True when anything changed.
    """
    endian, is64, _ncmds, hsize = _header(data, off)
    if endian != "<":
        raise MachOWriteError(f"{path_for_errors}: big-endian Mach-O is not supported")

    original = [(cmd, bytes(data[cmd_off:cmd_off + cmdsize]))
                for cmd, cmdsize, cmd_off in load_commands(data, off)]
    rebuilt = transform(list(original))
    if rebuilt is None:
        return False

    old_block = sum(len(raw) for _c, raw in original)
    new_block = sum(len(raw) for raw in rebuilt)
    if new_block == old_block and rebuilt == [raw for _c, raw in original]:
        return False

    limit = _slack_limit(data, off, endian, is64)
    available = (limit - hsize) if limit is not None else (len(data) - off - hsize)
    if new_block > available:
        raise MachOWriteError(
            f"{path_for_errors}: the load commands would not fit -- "
            f"{new_block} bytes needed, {available} available "
            f"({new_block - available} short). This is HP's file, not your Mac; "
            f"install_name_tool refuses the same input for the same reason.")

    # Replace exactly as many bytes as the larger of the two blocks, zero-filling
    # the remainder. Shrinking must not leave stale command bytes behind, and
    # growing must not disturb a byte past the slack that was measured above.
    block = b"".join(rebuilt)
    start = off + hsize
    region = max(old_block, len(block))
    data[start:start + region] = block + b"\x00" * (region - len(block))
    struct.pack_into(endian + "II", data, off + 16, len(rebuilt), len(block))
    return True


def _edit(path, transform):
    with open(path, "rb") as f:
        data = bytearray(f.read())
    changed = False
    for _name, off, _size in slices(bytes(data)):
        if _rewrite_slice(data, off, transform, path):
            changed = True
    if changed:
        with open(path, "wb") as f:
            f.write(data)
    return changed


def set_dylib_id(path, new_id):
    """install_name_tool -id. True when the id was changed."""
    def transform(commands):
        out, hit = [], False
        for cmd, raw in commands:
            if cmd == LC_ID_DYLIB:
                _c, _s, _o, timestamp, current, compat = struct.unpack_from("<IIIIII", raw, 0)
                out.append(_dylib_command(LC_ID_DYLIB, new_id, True,
                                          timestamp, current, compat))
                hit = True
            else:
                out.append(raw)
        return out if hit else None
    return _edit(path, transform)


def change_dylib_path(path, old, new):
    """install_name_tool -change. True when at least one command was changed."""
    def transform(commands):
        out, hit = [], False
        for cmd, raw in commands:
            if cmd in _CHANGEABLE:
                str_off = struct.unpack_from("<I", raw, 8)[0]
                body = raw[str_off:]
                nul = body.find(b"\x00")
                current = body[:nul if nul != -1 else len(body)].decode("utf-8", "replace")
                if current == old:
                    _c, _s, _o, timestamp, cur, compat = struct.unpack_from("<IIIIII", raw, 0)
                    out.append(_dylib_command(cmd, new, True, timestamp, cur, compat))
                    hit = True
                    continue
            out.append(raw)
        return out if hit else None
    return _edit(path, transform)


def add_rpath(path, rpath):
    """install_name_tool -add_rpath. False when every slice already had it.

    Appended last, which is where install_name_tool puts it and what dyld's
    search order expects.
    """
    def transform(commands):
        for cmd, raw in commands:
            if cmd == LC_RPATH:
                str_off = struct.unpack_from("<I", raw, 8)[0]
                body = raw[str_off:]
                nul = body.find(b"\x00")
                if body[:nul if nul != -1 else len(body)].decode("utf-8", "replace") == rpath:
                    return None
        return [raw for _c, raw in commands] + [_rpath_command(rpath, True)]
    return _edit(path, transform)
