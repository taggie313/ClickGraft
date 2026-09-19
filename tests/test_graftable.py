"""
tests/test_graftable.py — "not supported yet" versus "can never be".
Target: Python 3.9+
"""

import json
import os
import plistlib
import shutil
import subprocess
import tempfile

import pytest

from clickgraft import graftable

CLANG = shutil.which("clang")


@pytest.fixture
def app():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "HP Click.app")
    os.makedirs(os.path.join(path, "Contents", "Resources", "app", "appData", "macx", "lib"))
    try:
        yield path
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _electron(app, version):
    res = os.path.join(app, "Contents", "Frameworks", "Electron Framework.framework",
                       "Resources")
    os.makedirs(res, exist_ok=True)
    with open(os.path.join(res, "Info.plist"), "wb") as f:
        plistlib.dump({"CFBundleVersion": version}, f)


def _addon(app, name, *archs):
    src = os.path.join(os.path.dirname(app), "x.c")
    with open(src, "w") as f:
        f.write("int x(void) { return 0; }\n")
    cmd = ["clang", "-dynamiclib", "-o",
           os.path.join(app, "Contents", "Resources", "app", "appData", "macx", "lib", name)]
    for a in archs:
        cmd[1:1] = ["-arch", a]
    subprocess.run(cmd + [src], check=True, capture_output=True)


def _printers(app, names):
    with open(os.path.join(app, "Contents", "Resources", "printersValidate.json"), "w") as f:
        json.dump([{"name": n, "nameExactMatch": True} for n in names], f)


def test_old_electron_is_a_blocker(app):
    _electron(app, "8.2.3")
    found = graftable.blockers(app)
    assert len(found) == 1 and "Electron 8.2.3" in found[0]


def test_current_electron_is_not(app):
    _electron(app, "39.8.4")
    assert graftable.blockers(app) == []


def test_nothing_readable_means_no_claim(app):
    # No Electron plist, no add-ons: unknown must never be reported as hopeless.
    assert graftable.blockers(app) == []


@pytest.mark.skipif(not CLANG, reason="needs clang to make Mach-O add-ons")
def test_intel_only_addon_is_a_blocker(app):
    _electron(app, "39.8.4")
    _addon(app, "DjCoreServicesNative-Electron.node", "x86_64")
    found = graftable.blockers(app)
    assert found == ["HP's DjCoreServicesNative-Electron.node is Intel-only in this version."]


@pytest.mark.skipif(not CLANG, reason="needs clang to make Mach-O add-ons")
def test_both_intel_only_addons_read_as_one_finding(app):
    _electron(app, "8.2.3")
    _addon(app, "DjCoreServicesNative-Electron.node", "x86_64")
    _addon(app, "DjConnServicesNative-Electron.node", "x86_64")
    found = graftable.blockers(app)
    assert len(found) == 2
    assert found[1] == ("HP's DjCoreServicesNative-Electron.node and "
                        "DjConnServicesNative-Electron.node are Intel-only in this version.")


@pytest.mark.skipif(not CLANG, reason="needs clang to make Mach-O add-ons")
def test_universal_addon_is_not(app):
    _electron(app, "39.8.4")
    _addon(app, "DjCoreServicesNative-Electron.node", "x86_64", "arm64")
    _addon(app, "DjConnServicesNative-Electron.node", "x86_64", "arm64")
    assert graftable.blockers(app) == []


def test_unreadable_addon_is_not_evidence(app):
    lib = os.path.join(app, "Contents", "Resources", "app", "appData", "macx", "lib")
    with open(os.path.join(lib, "DjCoreServicesNative-Electron.node"), "wb") as f:
        f.write(b"not a binary")
    assert graftable.blockers(app) == []


def test_reference_list_is_4_8_117():
    ref = os.path.join(os.path.dirname(graftable.__file__), "data",
                       f"printers-{graftable.REFERENCE_VERSION}.json")
    with open(ref) as f:
        data = json.load(f)
    assert data["app_version"] == graftable.REFERENCE_VERSION
    assert len(data["names"]) == 117
    assert "HP DesignJet T750 36-in" in data["names"]


def test_moving_costs_nothing_when_list_is_a_subset(app):
    _printers(app, ["HP DesignJet T730", "HP DesignJet Z9dr 44in"])
    assert graftable.printers_lost_by_moving(app) == []


def test_moving_names_what_would_be_lost(app):
    _printers(app, ["HP DesignJet T730", "HP DesignJet Imaginary 9000"])
    assert graftable.printers_lost_by_moving(app) == ["HP DesignJet Imaginary 9000"]


def test_no_list_means_no_claim(app):
    assert graftable.printers_lost_by_moving(app) is None


def test_zero_byte_placeholder_is_skipped(app):
    unpacked = os.path.join(app, "Contents", "Resources", "app.asar.unpacked", "app",
                            "node_modules", "DjConnServices", "resources")
    os.makedirs(unpacked)
    open(os.path.join(unpacked, "printersValidate.json"), "w").close()
    assert graftable.printers_lost_by_moving(app) is None


def _native_parts(app, exe_archs, fw_archs):
    """HPClickExe, and an Electron Framework whose top-level binary is a symlink
    into Versions/A -- as in every real bundle, and the detail that first made
    4.11.31 read as not native."""
    src = os.path.join(os.path.dirname(app), "x.c")
    with open(src, "w") as f:
        f.write("int x(void) { return 0; }\n")
    def build(out, archs):
        os.makedirs(os.path.dirname(out), exist_ok=True)
        cmd = ["clang", "-dynamiclib", "-o", out, src]
        for a in archs:
            cmd[1:1] = ["-arch", a]
        subprocess.run(cmd, check=True, capture_output=True)
    build(os.path.join(app, "Contents", "MacOS", "HPClickExe"), exe_archs)
    fw = os.path.join(app, "Contents", "Frameworks", "Electron Framework.framework")
    build(os.path.join(fw, "Versions", "A", "Electron Framework"), fw_archs)
    os.symlink("Versions/A/Electron Framework", os.path.join(fw, "Electron Framework"))


@pytest.mark.skipif(not CLANG, reason="needs clang to make Mach-O binaries")
def test_universal_exe_and_framework_is_hp_native(app):
    _native_parts(app, ("x86_64", "arm64"), ("x86_64", "arm64"))
    assert graftable.hp_native(app)


@pytest.mark.skipif(not CLANG, reason="needs clang to make Mach-O binaries")
@pytest.mark.parametrize("exe,fw", [
    (("arm64",), ("arm64",)),                 # a ClickGraft copy
    (("x86_64",), ("x86_64",)),               # stock HP Click up to 4.10.42
    (("x86_64", "arm64"), ("x86_64",)),       # half-done: must not count
])
def test_anything_less_than_both_universal_is_not(app, exe, fw):
    _native_parts(app, exe, fw)
    assert not graftable.hp_native(app)


def test_missing_binaries_are_not_native(app):
    assert not graftable.hp_native(app)


def test_printers_dropped_names_what_needs_4_8_117(app):
    ref = os.path.join(os.path.dirname(graftable.__file__), "data",
                       f"printers-{graftable.REFERENCE_VERSION}.json")
    with open(ref) as f:
        names = json.load(f)["names"]
    keep = [n for n in names if "T750" not in n]
    _printers(app, keep)
    assert graftable.printers_dropped_since_reference(app) == [
        "HP DesignJet T750 24-in", "HP DesignJet T750 36-in"]


def test_printers_dropped_unknown_without_a_list(app):
    assert graftable.printers_dropped_since_reference(app) is None


def test_real_4_11_31_is_hp_native_and_lacks_the_t_series():
    path = "/Applications/4.11.31 HP Click.app"
    if not os.path.isdir(path):
        pytest.skip(f"{path} not installed")
    assert graftable.hp_native(path)
    dropped = graftable.printers_dropped_since_reference(path)
    assert dropped and all(any(m in n for m in ("T310", "T320", "T350", "T720", "T750"))
                           for n in dropped) and len(dropped) == 7


@pytest.mark.parametrize("name", ["4.8.117 HP Click.app", "4.8.118 HP Click.app",
                                  "HP Click.app", "HP Click (Apple Silicon).app"])
def test_real_intel_builds_and_copies_are_not_hp_native(name):
    path = os.path.join("/Applications", name)
    if not os.path.isdir(path):
        pytest.skip(f"{path} not installed")
    assert not graftable.hp_native(path)


@pytest.mark.parametrize("name", ["4.8.117 HP Click.app", "4.8.118 HP Click.app",
                                  "HP Click.app"])
def test_real_supported_bundles_are_never_flagged(name):
    path = os.path.join("/Applications", name)
    if not os.path.isdir(path):
        pytest.skip(f"{path} not installed")
    assert graftable.blockers(path) == []
    assert graftable.printers_lost_by_moving(path) == []
