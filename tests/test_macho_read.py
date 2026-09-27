"""Does reading Mach-O files in Python give the same answers as Apple's tools?

The swap that replaced `lipo`, `otool`, `nm` and `vtool` with
clickgraft/macho_read.py is only defensible if the answers did not change, so
this asks the tools directly, on a real HP Click bundle, and compares every
question. It is the expensive test in the suite and it is meant to be: it is the
only thing standing between a header-parsing mistake and a wrong build.

Three differences are EXPECTED, and they are asserted rather than tolerated,
because a check that shrugs at a class of difference cannot tell a fixed bug
from a new one. All three are cases where the tool is wrong:

  * `otool` reads an argument ending in ")" as archive(member) syntax, truncates
    it, and exits 1 -- so it cannot read HP Click Helper (GPU), (Plugin) or
    (Renderer) at all.
  * splitting `otool -L` output on whitespace truncated
    "@rpath/Electron Framework.framework/Electron Framework".
  * the same split read otool's per-architecture headers as a dependency.

Skips when no stock bundle is installed, like the rest of the suite.
"""

import os
import struct
import subprocess

import pytest

from clickgraft import macho_read
from clickgraft.macho import get_load_dylibs, get_rpaths, is_macho
from tests.test_clickgraft import find_stock_bundle


def _tool(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    return r.returncode, r.stdout


def _stock():
    for version in ("4.11.31", "4.8.117", "4.8.118"):
        app = find_stock_bundle(version)
        if app:
            return app
    return None


def _machos(app):
    for root, _dirs, files in os.walk(app):
        for fn in files:
            fp = os.path.join(root, fn)
            if is_macho(fp):
                yield fp


# --- the differential ------------------------------------------------------

@pytest.fixture(scope="module")
def bundle():
    app = _stock()
    if app is None:
        pytest.skip("no stock HP Click in /Applications")
    return app


def test_archs_match_lipo(bundle):
    checked, bad = 0, []
    for fp in _machos(bundle):
        rc, out = _tool(["lipo", "-archs", fp])
        if rc != 0:
            continue
        checked += 1
        mine = macho_read.archs(macho_read.read(fp))
        if out.split() != mine:
            bad.append((fp, out.split(), mine))
    assert checked > 50, f"only {checked} files compared; the walk found too little"
    assert not bad, bad[:5]


def test_minimums_match_vtool(bundle):
    """Compared as parsed versions, not as text: vtool prints 12.0 for 12.0.0."""
    from clickgraft.macos_floor import parse_version, slice_minimums

    checked, bad = 0, []
    for fp in _machos(bundle):
        rc, out = _tool(["vtool", "-show-build", fp])
        if rc != 0:
            continue
        theirs, arch, cmd, platform = {}, None, None, None
        for line in out.splitlines():
            if line and not line[0].isspace() and line.endswith(":"):
                marker = " (architecture "
                arch = line[line.rfind(marker) + len(marker):-2] \
                    if line.endswith("):") and marker in line else ""
                theirs.setdefault(arch, None)
                cmd = platform = None
                continue
            key, _, value = line.strip().partition(" ")
            value = value.strip()
            if key == "cmd":
                cmd, platform = value, None
            elif key == "platform":
                platform = value
            elif arch is None:
                continue
            elif ((key == "minos" and cmd == "LC_BUILD_VERSION" and platform in ("MACOS", "1"))
                  or (key == "version" and cmd == "LC_VERSION_MIN_MACOSX")):
                v = parse_version(value)
                if v is not None and (theirs[arch] is None or v > theirs[arch]):
                    theirs[arch] = v
        checked += 1
        if theirs != slice_minimums(fp):
            bad.append((fp, theirs, slice_minimums(fp)))
    assert checked > 50
    assert not bad, bad[:5]


def test_symbols_match_nm(bundle):
    checked, bad = 0, []
    for fp in _machos(bundle):
        data = macho_read.read(fp)
        if "arm64" not in macho_read.archs(data):
            continue
        rc, u = _tool(["nm", "-m", "-arch", "arm64", "-u", fp])
        if rc != 0:
            continue
        theirs_flat = set()
        for line in u.splitlines():
            if "dynamically looked up" in line:
                for tok in line.split():
                    if tok.startswith("_"):
                        theirs_flat.add(tok)
                        break
        rc2, d = _tool(["nm", "-arch", "arm64", "-g", "--defined-only", fp])
        if rc2 != 0:
            continue
        theirs_def = {c[2] for c in (l.split() for l in d.splitlines())
                      if len(c) >= 3 and c[2].startswith("_")}
        checked += 1
        mine_flat = macho_read.flat_undefined_symbols(data, "arm64")
        mine_def = {s for s in macho_read.defined_global_symbols(data, "arm64")
                    if s.startswith("_")}
        if theirs_flat != mine_flat:
            bad.append((fp, "flat", sorted(theirs_flat ^ mine_flat)[:4]))
        if theirs_def != mine_def:
            bad.append((fp, "defined", sorted(theirs_def ^ mine_def)[:4]))
    assert checked > 50
    assert not bad, bad[:5]


# --- the cases where the tools are wrong -----------------------------------

def test_reads_the_binaries_otool_cannot(bundle):
    """HP ships three executables whose names end in ")".

    otool treats that as archive(member) syntax and exits 1, so get_load_dylibs()
    used to return [] for all three and verify's Homebrew-leak audit skipped them
    by default rather than on evidence. Assert both halves: the tool still fails,
    and the reader still works.
    """
    helpers = [fp for fp in _machos(bundle) if fp.endswith(")")]
    if not helpers:
        pytest.skip("this bundle ships no helper whose name ends in ')'")
    for fp in helpers:
        rc, _out = _tool(["otool", "-L", fp])
        assert rc != 0, f"otool unexpectedly read {fp} — has Apple fixed it?"
        assert get_load_dylibs(fp), f"the reader got nothing from {fp}"


def test_dependency_paths_with_spaces_are_whole(bundle):
    """"@rpath/Electron Framework.framework/Electron Framework", not "@rpath/Electron"."""
    for fp in _machos(bundle):
        for dep in get_load_dylibs(fp):
            assert dep != "@rpath/Electron", f"truncated dependency in {fp}"
            assert not dep.endswith(".app"), f"a header line read as a dependency in {fp}"


def test_no_dependency_is_the_bundle_path(bundle):
    """otool's per-architecture headers used to be parsed as dependencies."""
    for fp in _machos(bundle):
        assert bundle not in get_load_dylibs(fp), f"bundle path as a dependency in {fp}"


# --- units that need no bundle ---------------------------------------------

def _thin_header(cputype, cpusubtype):
    """A minimal 64-bit Mach-O header with no load commands."""
    return struct.pack("<IiiIIII", macho_read.MH_MAGIC_64, cputype, cpusubtype,
                       2, 0, 0, 0) + b"\x00" * 4


def test_lib64_subtype_is_not_unknown():
    """Every main executable HP ships sets CPU_SUBTYPE_LIB64 in its subtype.

    Read signed and masked with Python's arbitrary-precision ints, that came out
    as "unknown(16777223,-4294967293)" rather than x86_64 — which the first
    differential run caught on six files, HPClickExe among them.
    """
    data = _thin_header(0x01000007, -0x7FFFFFFD)   # x86_64 | CPU_SUBTYPE_LIB64
    assert macho_read.archs(data) == ["x86_64"]


def test_arm64_and_arm64e_are_distinguished():
    assert macho_read.archs(_thin_header(0x0100000C, 0)) == ["arm64"]
    assert macho_read.archs(_thin_header(0x0100000C, 2)) == ["arm64e"]


def test_java_class_is_refused():
    """A .class file opens with the same CAFEBABE a fat Mach-O does."""
    java = b"\xca\xfe\xba\xbe" + struct.pack(">HH", 0, 65) + b"\x00" * 64
    with pytest.raises(macho_read.MachOError):
        macho_read.slices(java)


def test_not_a_macho_at_all():
    with pytest.raises(macho_read.MachOError):
        macho_read.slices(b"#!/bin/sh\necho hello\n")


def test_truncated_file_does_not_raise_past_the_header():
    """A short read should give back what could be parsed, not take a build down."""
    data = _thin_header(0x0100000C, 0)
    assert macho_read.dylib_paths(data) == []
    assert macho_read.rpaths(data) == []
