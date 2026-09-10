"""
tests/test_macho_prescreen.py — Prescreen Mach-O magic numbers before shelling out to `file`.
Target: Python 3.9+
"""

import os
import shutil
import stat
import tempfile

import pytest

from clickgraft import macho

_ALL_MAGICS = [
    b"\xce\xfa\xed\xfe",  # MH_MAGIC     32-bit, little-endian
    b"\xfe\xed\xfa\xce",  # MH_CIGAM     32-bit, big-endian
    b"\xcf\xfa\xed\xfe",  # MH_MAGIC_64  64-bit, little-endian
    b"\xfe\xed\xfa\xcf",  # MH_CIGAM_64  64-bit, big-endian
    b"\xca\xfe\xba\xbe",  # FAT_MAGIC    also Java .class - ambiguous
    b"\xbe\xba\xfe\xca",  # FAT_CIGAM
    b"\xca\xfe\xba\xbf",  # FAT_MAGIC_64
    b"\xbf\xba\xfe\xca",  # FAT_CIGAM_64
]


@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp()
    try:
        yield d
    finally:
        for root, dirs, files in os.walk(d):
            for name in files + dirs:
                try:
                    os.chmod(os.path.join(root, name), stat.S_IRWXU)
                except OSError:
                    pass
        shutil.rmtree(d, ignore_errors=True)


def test_rejects_text_without_calling_file(tmp_dir, monkeypatch):
    p = os.path.join(tmp_dir, "sample.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write("This is plain ASCII text, definitely not Mach-O.")
    counter = 0

    def fake_run_cmd(cmd, check=True):
        nonlocal counter
        counter += 1
        raise AssertionError(f"run_cmd called unexpectedly: {cmd}")

    monkeypatch.setattr(macho, "run_cmd", fake_run_cmd)
    assert macho.is_macho(p) is False
    assert counter == 0


def test_rejects_short_file(tmp_dir, monkeypatch):
    p = os.path.join(tmp_dir, "short.bin")
    with open(p, "wb") as f:
        f.write(b"\xce\xfa")
    counter = 0

    def fake_run_cmd(cmd, check=True):
        nonlocal counter
        counter += 1
        raise AssertionError(f"run_cmd called unexpectedly: {cmd}")

    monkeypatch.setattr(macho, "run_cmd", fake_run_cmd)
    assert macho.is_macho(p) is False
    assert counter == 0


def test_rejects_empty_file(tmp_dir, monkeypatch):
    p = os.path.join(tmp_dir, "empty.bin")
    open(p, "wb").close()
    counter = 0

    def fake_run_cmd(cmd, check=True):
        nonlocal counter
        counter += 1
        raise AssertionError(f"run_cmd called unexpectedly: {cmd}")

    monkeypatch.setattr(macho, "run_cmd", fake_run_cmd)
    assert macho.is_macho(p) is False
    assert counter == 0


@pytest.mark.parametrize("magic", _ALL_MAGICS)
def test_each_magic_reaches_the_file_command(tmp_dir, monkeypatch, magic):
    p = os.path.join(tmp_dir, f"macho_{magic.hex()}.bin")
    with open(p, "wb") as f:
        f.write(magic + b"\x00" * 60)
    counter = 0

    def fake_run_cmd(cmd, check=True):
        nonlocal counter
        counter += 1
        return "x: Mach-O 64-bit executable arm64"

    monkeypatch.setattr(macho, "run_cmd", fake_run_cmd)
    assert macho.is_macho(p) is True
    assert counter == 1


def test_java_class_magic_still_defers_to_file(tmp_dir, monkeypatch):
    p = os.path.join(tmp_dir, "App.class")
    with open(p, "wb") as f:
        f.write(b"\xca\xfe\xba\xbe" + b"\x00" * 60)
    counter = 0

    def fake_run_cmd(cmd, check=True):
        nonlocal counter
        counter += 1
        return "x: compiled Java class data"

    monkeypatch.setattr(macho, "run_cmd", fake_run_cmd)
    assert macho.is_macho(p) is False
    assert counter == 1


def test_coderesources_still_excluded(tmp_dir, monkeypatch):
    p = os.path.join(tmp_dir, "CodeResources")
    with open(p, "wb") as f:
        f.write(b"\xcf\xfa\xed\xfe" + b"\x00" * 60)
    counter = 0

    def fake_run_cmd(cmd, check=True):
        nonlocal counter
        counter += 1
        return "x: Mach-O 64-bit executable arm64"

    monkeypatch.setattr(macho, "run_cmd", fake_run_cmd)
    assert macho.is_macho(p) is False


def test_symlink_and_directory_and_missing(tmp_dir, monkeypatch):
    target = os.path.join(tmp_dir, "valid_macho.bin")
    with open(target, "wb") as f:
        f.write(b"\xcf\xfa\xed\xfe" + b"\x00" * 60)

    symlink_path = os.path.join(tmp_dir, "link_to_macho")
    os.symlink(target, symlink_path)

    dir_path = os.path.join(tmp_dir, "sub_directory")
    os.mkdir(dir_path)

    missing_path = os.path.join(tmp_dir, "nonexistent.bin")

    counter = 0

    def fake_run_cmd(cmd, check=True):
        nonlocal counter
        counter += 1
        return "x: Mach-O 64-bit executable arm64"

    monkeypatch.setattr(macho, "run_cmd", fake_run_cmd)

    assert macho.is_macho(symlink_path) is False
    assert macho.is_macho(dir_path) is False
    assert macho.is_macho(missing_path) is False
    assert counter == 0


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_unreadable_file_defers_rather_than_guessing(tmp_dir):
    p = os.path.join(tmp_dir, "unreadable.bin")
    with open(p, "wb") as f:
        f.write(b"some content")
    os.chmod(p, 0o000)
    assert macho._could_be_macho(p) is True


def test_degenerate_inputs_do_not_raise(tmp_dir):
    # Empty path string
    assert macho.is_macho("") is False
    assert macho._could_be_macho("") is True

    # Broken symlink
    broken_link = os.path.join(tmp_dir, "broken_link")
    os.symlink(os.path.join(tmp_dir, "does_not_exist"), broken_link)
    assert macho.is_macho(broken_link) is False

    # FIFO created with os.mkfifo
    fifo_path = os.path.join(tmp_dir, "test_fifo")
    os.mkfifo(fifo_path)
    assert macho.is_macho(fifo_path) is False

    # File deleted between isfile check and read
    vanish = os.path.join(tmp_dir, "vanish.bin")
    with open(vanish, "wb") as f:
        f.write(b"data")
    assert macho._could_be_macho(vanish) is False
    os.unlink(vanish)
    assert macho._could_be_macho(vanish) is True


def test_degenerate_file_deleted_between_isfile_and_read(tmp_dir, monkeypatch):
    p = os.path.join(tmp_dir, "vanish_mid_check.bin")
    with open(p, "wb") as f:
        f.write(b"\xcf\xfa\xed\xfe" + b"\x00" * 60)
    orig_isfile = os.path.isfile

    def isfile_and_unlink(path):
        res = orig_isfile(path)
        if path == p and os.path.exists(p):
            os.unlink(p)
        return res

    monkeypatch.setattr(os.path, "isfile", isfile_and_unlink)
    assert macho.is_macho(p) is False
