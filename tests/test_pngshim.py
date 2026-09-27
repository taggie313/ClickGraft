"""The libpng NEON shim, which ships prebuilt.

HP's arm64 build has an undefined flat-namespace reference to
png_init_filter_functions_neon. HP never trips over it because they ship an
Intel Electron and never load the arm64 slice; grafting an arm64 runtime makes
it live and the call lands on a null pointer. The shim is four lines of C that
do nothing, which is the correct behaviour rather than a fudge -- libpng
installs its portable C filters first and calls this only to override them.

It used to be compiled on the user's Mac. That made clang, one of the xcrun
shims, a hard requirement for everyone making a copy, to produce the same 16 KB
every time. The objection to shipping it instead was fair -- a binary in git
that nobody can diff is worse than four lines of C -- so these tests hold the
answer to it: the .c stays the reviewable thing, and the .dylib has to be that
.c compiled.
"""

import os
import shutil
import subprocess
import sys

import pytest

from clickgraft import macho_read
from clickgraft.build import _check_pngshim

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIM = os.path.join(ROOT, "clickgraft", "shims", "libclickgraft-pngshim.dylib")
SOURCE = os.path.join(ROOT, "clickgraft", "shims", "pngshim.c")
BUILDER = os.path.join(ROOT, "packaging", "build_pngshim.sh")


def test_both_the_source_and_the_binary_ship():
    """Either alone would be a problem: the .c with no .dylib means a build that
    cannot run, and the .dylib with no .c means a binary nobody can check."""
    assert os.path.exists(SOURCE), "pngshim.c is the reviewable half"
    assert os.path.exists(SHIM), "the prebuilt shim is what a build installs"
    assert os.path.exists(BUILDER), "build_pngshim.sh is how it is remade"


def test_the_shipped_shim_is_what_a_build_expects():
    _check_pngshim(SHIM)


def test_it_exports_the_one_symbol_it_exists_for():
    data = macho_read.read(SHIM)
    assert "_png_init_filter_functions_neon" in \
        macho_read.defined_global_symbols(data, "arm64")


def test_it_does_not_raise_the_copy_floor():
    """The copy's minimum macOS is the highest minimum inside it.

    clang's default deployment target is the macOS it runs on, so a shim built
    without -mmacosx-version-min declared 27.0 on the development Mac (measured
    22 Sep 2026) and would have taken every copy's floor with it.
    """
    data = macho_read.read(SHIM)
    assert set(macho_read.minimum_versions(data).values()) == {"11.0"}


def test_it_is_arm64_only():
    """It exists because an arm64 runtime was grafted in. A universal shim would
    be two slices where one is never loaded."""
    assert macho_read.archs(macho_read.read(SHIM)) == ["arm64"]


def test_a_build_does_not_compile_it():
    """The whole point of shipping it. If clang comes back here, the last
    developer tool comes back with it."""
    body = open(os.path.join(ROOT, "clickgraft", "build.py"), encoding="utf-8").read()
    assert '["clang"' not in body and '"clang",' not in body


# --- the gate that makes shipping a binary defensible ----------------------

def _clang_available():
    return subprocess.run(["clang", "--version"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _clang_available(), reason="no clang on this machine")
def test_rebuilding_reproduces_the_shipped_shim(tmp_path):
    """packaging/check_release.py's step, run directly.

    Compared as code, not as bytes: LC_BUILD_VERSION records the SDK, so the
    same source built against MacOSX15, MacOSX26 and MacOSX27 gives three
    different files while __TEXT,__text is identical across all three.
    """
    fresh = str(tmp_path / "rebuilt.dylib")
    run = subprocess.run([BUILDER, fresh], cwd=ROOT, capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr

    a, b = macho_read.read(SHIM), macho_read.read(fresh)
    assert macho_read.section_data(a, "arm64", "__TEXT", "__text") == \
        macho_read.section_data(b, "arm64", "__TEXT", "__text")
    assert macho_read.defined_global_symbols(a, "arm64") == \
        macho_read.defined_global_symbols(b, "arm64")
    assert macho_read.dylib_paths(a) == macho_read.dylib_paths(b)
    assert macho_read.archs(a) == macho_read.archs(b)
    assert macho_read.minimum_versions(a) == macho_read.minimum_versions(b)


@pytest.mark.skipif(not _clang_available(), reason="no clang on this machine")
def test_the_gate_refuses_a_shim_that_is_not_the_source(tmp_path):
    """The control. A gate that only ever passes is not a gate.

    One byte flipped inside __TEXT,__text -- the code itself, not metadata --
    must be caught and named.
    """
    sys.path.insert(0, os.path.join(ROOT, "packaging"))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cr_for_test", os.path.join(ROOT, "packaging", "check_release.py"))
    cr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cr)

    repo = tmp_path / "repo"
    shutil.copytree(ROOT, repo, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", "dist", "__pycache__",
                                                  "scratchpad", "*.pyc"))
    cr.check_pngshim(repo)          # unmodified: passes

    target = repo / "clickgraft/shims/libclickgraft-pngshim.dylib"
    raw = bytearray(target.read_bytes())
    code = macho_read.section_data(macho_read.read(str(target)), "arm64", "__TEXT", "__text")
    at = bytes(raw).find(code)
    assert at != -1, "could not locate __text to tamper with"
    raw[at] ^= 0xFF
    target.write_bytes(bytes(raw))

    with pytest.raises(cr.Refused) as e:
        cr.check_pngshim(repo)
    assert "pngshim.c" in str(e.value)
    assert any("code:" in line for line in e.value.details), e.value.details
