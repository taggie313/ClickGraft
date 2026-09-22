"""Source preservation at the shared build entry point.

Until 1.5.9, --out naming its own --source replaced the stock app and deleted
it (reproduced 22 Sep 2026 on an APFS clone of 4.8.117). The refusals below use
no installed app. The two tests at the end make a real copy from a stock HP
Click, and skip without one: that the build asks before_install, after the
overlap check has run again, before anything at the output path moves; and
that a re-pointed output folder leaves no staging copy behind.
"""
import os

import pytest

from clickgraft import build
from tests.test_rollback_and_signing import NAME, _stock, _tree
from tests.test_rollback_and_signing import _copy as installed_copy


@pytest.mark.parametrize("case", [
    "same", "default_name", "symlink", "parent_symlink",
    "output_inside_source", "source_inside_output", "nested_alias",
    "case_alias", "nested_case_alias",
])
def test_build_refuses_overlapping_locations_before_writing(tmp_path, monkeypatch, case):
    source = tmp_path / "Source.app"
    source.mkdir()
    marker = source / "original.txt"
    marker.write_bytes(b"original application")
    output = source
    if case == "default_name":
        renamed = tmp_path / "HP Click (Apple Silicon).app"
        source.rename(renamed)
        source, marker, output = renamed, renamed / marker.name, None
    elif case == "symlink":
        output = tmp_path / "Alias.app"
        output.symlink_to(source, target_is_directory=True)
    elif case == "parent_symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        output = alias / source.name
    elif case == "output_inside_source":
        output = source / "Output.app"
    elif case == "source_inside_output":
        output = tmp_path
    elif case == "nested_alias":
        alias = tmp_path / "alias"
        alias.symlink_to(source, target_is_directory=True)
        output = alias / "Output.app"
    elif case in ("case_alias", "nested_case_alias"):
        alias = tmp_path / "source.APP"
        if not alias.exists() or not os.path.samefile(source, alias):
            pytest.skip("requires a case-insensitive filesystem")
        output = alias if case == "case_alias" else alias / "Output.app"

    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    identity = marker.stat().st_ino
    monkeypatch.setattr("clickgraft.hostarch.is_apple_silicon", lambda: True)

    def unexpected(*args, **kwargs):
        pytest.fail("collision must be rejected before writes or downloads")

    monkeypatch.setattr(build.os, "mkdir", unexpected)
    monkeypatch.setattr(build, "fetch_electron", unexpected)
    with pytest.raises(ValueError, match="separate, non-nested") as error:
        build.build_apple_silicon_bundle(str(source), str(output) if output else None,
                                         manifest={})
    assert "Source:" in str(error.value) and "Output:" in str(error.value)
    assert marker.read_bytes() == b"original application"
    assert marker.stat().st_ino == identity
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == before


def test_distinct_siblings_and_shared_parent_aliases_are_allowed(tmp_path):
    source = tmp_path / "Source.app"
    source.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    for output in (tmp_path / "Source.app copy.app", alias / "Output.app"):
        assert build.refuse_overlapping_paths(str(source), str(output)) is None


def test_case_distinct_siblings_on_case_sensitive_filesystems_are_allowed(tmp_path):
    source = tmp_path / "Source.app"
    source.mkdir()
    output = tmp_path / "source.app"
    if output.exists():
        pytest.skip("requires a case-sensitive filesystem")
    output.mkdir()
    assert build.refuse_overlapping_paths(str(source), str(output)) is None


class _Stop(Exception):
    """Raised from before_install to end a real build there."""


def test_the_real_build_checks_again_and_asks_before_install_last(tmp_path, monkeypatch):
    """The 22 Sep 2026 review found that nothing made the real build call
    before_install, or check the overlap a second time: the agent tests stood
    in for the whole build and called the hook themselves."""
    source, manifest = _stock()
    if source is None:
        pytest.skip("no stock HP Click with a manifest in /Applications")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    old = installed_copy(str(out_dir), "4.8.117", os.urandom(4096))
    before, inode = _tree(old), os.stat(old).st_ino
    calls = []
    real_overlap = build.refuse_overlapping_paths

    def overlap(src, out):
        calls.append("overlap")
        return real_overlap(src, out)

    def before_install():
        calls.append("before_install")
        # The copy is made and signed, in the folder being built into, and the
        # copy it would replace has not moved.
        staging = [n for n in os.listdir(out_dir) if n.startswith("staging_clickgraft_")]
        assert len(staging) == 1, os.listdir(out_dir)
        assert os.stat(old).st_ino == inode
        raise _Stop()

    monkeypatch.setattr(build, "refuse_overlapping_paths", overlap)
    with pytest.raises(_Stop):
        build.build_apple_silicon_bundle(
            source, old, manifest=manifest, before_install=before_install,
            progress_callback=lambda msg, pct: calls.append(msg))

    signed = next(i for i, c in enumerate(calls) if c.startswith("Signing application bundle"))
    assert calls[0] == "overlap"
    assert calls[signed + 1:] == ["overlap", "before_install"], calls[signed:]
    assert not any(c.startswith("Finalizing") for c in calls), "the install had started"
    assert _tree(old) == before and os.stat(old).st_ino == inode
    assert sorted(os.listdir(out_dir)) == [NAME], "staging or a set-aside copy was left"


def test_repointing_the_output_folder_mid_build_leaves_nothing_behind(tmp_path):
    """Measured 22 Sep 2026: with the output's parent symlink re-pointed part
    way through, 1.5.9 made its staging copy in one folder, looked for it in
    the other, and left 1.4 GB of staging_clickgraft_<pid> behind. The build
    now works in the folder it locked, and will not install into another."""
    source, manifest = _stock()
    if source is None:
        pytest.skip("no stock HP Click with a manifest in /Applications")
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    copies = [installed_copy(str(first), "4.8.117", b"first"),
              installed_copy(str(second), "4.8.117", b"second")]
    trees = [_tree(c) for c in copies]
    link = tmp_path / "Applications"
    link.symlink_to(first, target_is_directory=True)

    def progress(msg, pct):
        # After the staging copy is made in the first folder.
        if msg.startswith("Extracting Electron"):
            link.unlink()
            link.symlink_to(second, target_is_directory=True)

    with pytest.raises(ValueError, match="now leads somewhere else"):
        build.build_apple_silicon_bundle(source, str(link / NAME), manifest=manifest,
                                         progress_callback=progress)
    assert sorted(os.listdir(first)) == [NAME], "a staging copy was left behind"
    assert sorted(os.listdir(second)) == [NAME]
    assert [_tree(c) for c in copies] == trees
