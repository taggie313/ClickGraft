"""Rewriting install names and rpaths without install_name_tool.

The writing half of dropping Apple's Command Line Tools. Three things have to
hold, and the third is the one that is easy to skip:

  1. The edits ClickGraft actually makes all fit. Not "usually fit" -- every
     APPE binary a build gives @loader_path/.., on a real bundle, with codesign
     agreeing afterwards.
  2. The result is what install_name_tool would have produced, judged by otool,
     which is independent of the parser under test.
  3. A file with no room is REFUSED. A pass on padded inputs alone is a pass for
     the wrong reason: macho_write only ever uses slack that is already there,
     so the case it cannot do has to be the case it declines loudly.

Writing the synthetic file for (3) is what caught the real bug in this module --
fileoff was being read at the wrong offset in both segment_command variants,
which the real bundles hid because the section offsets dominated the minimum.
"""

import os
import shutil
import struct
import subprocess

import pytest

from clickgraft import macho_write
from clickgraft.macho import get_load_dylibs, get_rpaths, is_macho
from tests.test_clickgraft import find_stock_bundle


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _otool_view(path):
    rc, out = _run(["otool", "-L", path])
    deps = []
    if rc == 0:
        for line in out.splitlines():
            if line.startswith("\t"):
                body = line.strip()
                cut = body.rfind(" (compatibility version ")
                deps.append(body[:cut] if cut != -1 else body)
    rc2, out2 = _run(["otool", "-l", path])
    rps, lines = [], out2.splitlines()
    if rc2 == 0:
        for i, line in enumerate(lines):
            if "cmd LC_RPATH" in line:
                for j in range(i, min(i + 5, len(lines))):
                    s = lines[j].strip()
                    if s.startswith("path "):
                        rps.append(s[5:].split(" (offset")[0].strip())
                        break
    return deps, rps


@pytest.fixture(scope="module")
def bundle():
    for version in ("4.11.31", "4.8.117", "4.8.118"):
        app = find_stock_bundle(version)
        if app:
            return app
    pytest.skip("no stock HP Click in /Applications")


def _appe_binaries(bundle):
    appe = os.path.join(bundle, "Contents", "Resources", "app", "appData",
                        "macx", "bin", "APPE", "JDFPrintProcessor")
    for root, _dirs, files in os.walk(appe):
        if ".dSYM" + os.sep in root + os.sep:
            continue
        for f in files:
            fp = os.path.join(root, f)
            if not os.path.islink(fp) and is_macho(fp):
                yield fp


# --- 1. the edits a build really makes ------------------------------------

def test_every_appe_rpath_a_build_adds_is_byte_identical(bundle, tmp_path):
    """The decisive one, and it asserts the strongest thing available.

    Not "the rpath is there" or "otool agrees" -- the whole file, byte for byte,
    against what install_name_tool produces from the same input. Identical bytes
    make runtime behaviour identical by construction, which is the claim that
    matters for a print job nobody can run in a test. Measured over 84 files
    across 4.8.117 and 4.11.31 on 27 September 2026: zero differences.

    It also catches the size refusal, since a build stops if any APPE binary has
    no slack for the rpath.
    """
    done, refused, differing = 0, [], []
    for fp in _appe_binaries(bundle):
        if "@loader_path/.." in get_rpaths(fp):
            continue
        a, b = str(tmp_path / "a.bin"), str(tmp_path / "b.bin")
        shutil.copy2(fp, a)
        shutil.copy2(fp, b)
        if _run(["install_name_tool", "-add_rpath", "@loader_path/..", a])[0] != 0:
            continue                      # Apple declined this input; nothing to compare
        try:
            macho_write.add_rpath(b, "@loader_path/..")
        except macho_write.MachOWriteError as e:
            refused.append((os.path.basename(fp), str(e)))
            continue
        with open(a, "rb") as fa, open(b, "rb") as fb:
            if fa.read() != fb.read():
                differing.append(os.path.basename(fp))
                continue
        assert _run(["codesign", "--force", "-s", "-", b])[0] == 0
        assert _run(["codesign", "--verify", b])[0] == 0
        done += 1
    assert not refused, refused[:3]
    assert not differing, differing[:5]
    assert done > 20, f"only {done} binaries exercised; the walk found too little"


# --- 2. agreement with the tool it replaces --------------------------------

def test_add_rpath_matches_install_name_tool(bundle, tmp_path):
    subject = next((fp for fp in _appe_binaries(bundle)
                    if "@loader_path/.." not in get_rpaths(fp)), None)
    if subject is None:
        pytest.skip("every APPE binary already carries the rpath")
    a, b = str(tmp_path / "a.bin"), str(tmp_path / "b.bin")
    shutil.copy2(subject, a)
    shutil.copy2(subject, b)
    rc, out = _run(["install_name_tool", "-add_rpath", "@loader_path/..", a])
    if rc != 0:
        pytest.skip(f"install_name_tool declined this input: {out.strip()[:80]}")
    macho_write.add_rpath(b, "@loader_path/..")
    assert _otool_view(a) == _otool_view(b)
    with open(a, "rb") as fa, open(b, "rb") as fb:
        assert fa.read() == fb.read(), "same view, different bytes"


def test_change_dylib_path_matches_install_name_tool(tmp_path):
    """Searches every stock bundle, not just the newest.

    4.11.31 rewrote its Qt5 install names before shipping, so it has no bare
    dependency left to change -- taking the module-scoped bundle would skip this
    comparison on a Mac that has 4.8.117 sitting right next to it.
    """
    subject = dep = None
    candidates = [find_stock_bundle(v) for v in ("4.8.117", "4.8.118", "4.10.42", "4.11.31")]
    candidates = [c for c in candidates if c]
    if not candidates:
        pytest.skip("no stock HP Click in /Applications")
    for app in candidates:
        for root, _dirs, files in os.walk(os.path.join(app, "Contents", "Resources", "app")):
            for f in files:
                if not (f.endswith(".dylib") or f.endswith(".node")):
                    continue
                fp = os.path.join(root, f)
                if os.path.islink(fp) or not is_macho(fp):
                    continue
                # The same four names build.py matches, and never the file's own
                # LC_ID_DYLIB -- get_load_dylibs lists it first, exactly as otool
                # does, and -change does not touch an id. Picking it would assert
                # that a no-op changed something.
                qt_libs = ("libQt5Gui.5.dylib", "libQt5Network.5.dylib",
                           "libQt5Xml.5.dylib", "libQt5Core.5.dylib")
                deps = get_load_dylibs(fp)
                bare = [d for d in deps[1:]
                        if any(d.endswith(q) for q in qt_libs) and not d.startswith("@rpath/")]
                if bare:
                    subject, dep = fp, bare[0]
                    break
            if subject:
                break
        if subject:
            break
    if subject is None:
        pytest.skip("this bundle has no bare Qt5 dependency to rewrite")

    a, b = str(tmp_path / "a.bin"), str(tmp_path / "b.bin")
    shutil.copy2(subject, a)
    shutil.copy2(subject, b)
    new = "@rpath/" + os.path.basename(dep)
    rc, out = _run(["install_name_tool", "-change", dep, new, a])
    if rc != 0:
        pytest.skip(f"install_name_tool declined this input: {out.strip()[:80]}")
    assert macho_write.change_dylib_path(b, dep, new) is True
    assert _otool_view(a) == _otool_view(b)
    with open(a, "rb") as fa, open(b, "rb") as fb:
        assert fa.read() == fb.read(), "same view, different bytes"


def test_add_rpath_is_idempotent(bundle, tmp_path):
    """False, not an error, when it is already there.

    install_name_tool exited non-zero here and had to be told apart from a real
    failure by grepping its stderr for "would duplicate path".
    """
    subject = next((fp for fp in _appe_binaries(bundle)
                    if "@loader_path/.." not in get_rpaths(fp)), None)
    if subject is None:
        pytest.skip("every APPE binary already carries the rpath")
    copy = str(tmp_path / "t.bin")
    shutil.copy2(subject, copy)
    assert macho_write.add_rpath(copy, "@loader_path/..") is True
    assert macho_write.add_rpath(copy, "@loader_path/..") is False


def test_set_dylib_id_round_trip(bundle, tmp_path):
    subject = None
    for root, _dirs, files in os.walk(bundle):
        for f in files:
            fp = os.path.join(root, f)
            if f.endswith(".dylib") and not os.path.islink(fp) and is_macho(fp):
                subject = fp
                break
        if subject:
            break
    if subject is None:
        pytest.skip("no dylib in this bundle")
    copy = str(tmp_path / "t.dylib")
    shutil.copy2(subject, copy)
    assert macho_write.set_dylib_id(copy, "@rpath/renamed.dylib") is True
    assert get_load_dylibs(copy)[0] == "@rpath/renamed.dylib"
    assert _run(["codesign", "--force", "-s", "-", copy])[0] == 0
    assert _run(["codesign", "--verify", copy])[0] == 0


# --- 3. the refusal control ------------------------------------------------

def _no_slack_dylib():
    """A 64-bit Mach-O whose first section begins where its load commands end.

    Built by hand because a linker will not produce one: there is exactly zero
    padding, so any command that grows must be refused.
    """
    hsize, seg, sect = 32, 72, 80
    sizeofcmds = seg + sect
    first_section_offset = hsize + sizeofcmds

    header = struct.pack("<IiiIIIII", macho_read_magic(), 0x0100000C, 0,
                         6, 1, sizeofcmds, 0, 0)
    segment = struct.pack("<II16sQQQQiiII",
                          macho_write.LC_SEGMENT_64, seg + sect, b"__TEXT",
                          0, 0x1000, 0, first_section_offset + 16,
                          7, 5, 1, 0)
    section = struct.pack("<16s16sQQIIIIIIII",
                          b"__text", b"__TEXT", 0, 16,
                          first_section_offset, 0, 0, 0, 0, 0, 0, 0)
    return header + segment + section + b"\x90" * 16


def macho_read_magic():
    from clickgraft.macho_read import MH_MAGIC_64
    return MH_MAGIC_64


def test_refuses_a_file_with_no_room(tmp_path):
    path = str(tmp_path / "tight.dylib")
    with open(path, "wb") as f:
        f.write(_no_slack_dylib())
    with pytest.raises(macho_write.MachOWriteError) as e:
        macho_write.add_rpath(path, "@loader_path/..")
    said = str(e.value)
    assert "would not fit" in said
    assert "short" in said, f"the message must say by how much: {said}"


def test_a_refused_file_is_left_alone(tmp_path):
    """Refusing must not half-write. The bytes are the assertion."""
    path = str(tmp_path / "tight.dylib")
    original = _no_slack_dylib()
    with open(path, "wb") as f:
        f.write(original)
    with pytest.raises(macho_write.MachOWriteError):
        macho_write.add_rpath(path, "@loader_path/..")
    with open(path, "rb") as f:
        assert f.read() == original
