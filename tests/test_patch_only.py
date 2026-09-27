"""
tests/test_patch_only.py — a copy that is patched but never grafted.

4.11.31 is HP's own Apple Silicon build, so there is no runtime to put in it.
What it still needs is an updater that never runs. These check the mode itself;
the end-to-end build is exercised by hand against a real 4.11.31, because it
needs one installed.
Target: Python 3.9+
"""

import json
import os

import pytest

from clickgraft.bundle_audits import check_minimum_macos
from clickgraft.manifest import ManifestManager

MANIFESTS = ManifestManager().manifests
PATCH_ONLY = {v: m for v, m in MANIFESTS.items() if m.get("mode") == "patch_only"}


def test_there_is_a_patch_only_manifest():
    assert PATCH_ONLY, "expected at least 4.11.31"


def test_a_patch_only_manifest_validates_without_graft_keys():
    """The three dropped keys all describe the arm64 runtime that is put in."""
    mm = ManifestManager()
    for version, m in PATCH_ONLY.items():
        mm.validate_manifest(m)
        for absent in ("electron_version", "electron_sha256", "required_dylibs"):
            assert absent not in m, f"{version} should not describe a fetch it never makes"


def test_a_graft_manifest_still_requires_them():
    """The relaxation must not leak into the manifests that do graft."""
    mm = ManifestManager()
    for version, m in MANIFESTS.items():
        if m.get("mode") == "patch_only":
            continue
        for needed in ("electron_version", "required_dylibs", "expected_x86_only"):
            assert needed in m, f"{version} lost {needed}"
        broken = dict(m)
        broken.pop("electron_version")
        with pytest.raises(ValueError):
            mm.validate_manifest(broken)


def test_the_updater_op_is_the_same_one_every_manifest_uses():
    """One anchor across every version, so there is one thing to re-measure."""
    seen = set()
    for m in MANIFESTS.values():
        for patch in m.get("patches", []):
            if patch["path"].endswith("app-updater.js"):
                for op in patch["ops"]:
                    seen.add((op["anchor"], op["replacement"]))
    assert seen == {("function startup(e){", "function startup(e){return;")}, seen


def test_patch_only_records_what_it_cannot_prove():
    for version, m in PATCH_ONLY.items():
        assert m.get("hp_declared_floor"), f"{version} must record HP's own floor"
        assert m.get("accepted_unprovided_symbols"), \
            f"{version} must record the symbols measured from stock"
        note = m.get("$accepted_unprovided_symbols_note", "")
        assert "stock" in note.lower(), "the note must say they were measured against stock"


def test_the_floor_check_refuses_a_lowered_floor(tmp_path):
    """The one way patch_only could still get the floor wrong."""
    import plistlib
    app = tmp_path / "HP Click.app"
    (app / "Contents").mkdir(parents=True)
    with open(app / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleShortVersionString": "4.11.31",
                       "LSMinimumSystemVersion": "11.0"}, f)
    with pytest.raises(ValueError) as e:
        check_minimum_macos(str(app), patch_only=True, hp_declared=(12, 0, 0))
    assert "must not change this" in str(e.value)


def test_the_floor_check_accepts_hps_own_floor(tmp_path):
    import plistlib
    app = tmp_path / "HP Click.app"
    (app / "Contents").mkdir(parents=True)
    with open(app / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleShortVersionString": "4.11.31",
                       "LSMinimumSystemVersion": "12.0"}, f)
    got = check_minimum_macos(str(app), patch_only=True, hp_declared=(12, 0, 0))
    assert got["declared"] == "12.0"
    assert "HP's own" in got["note"]
