"""
tests/test_manifest_patches.py — What the 1.5.8 manifests patch, and that it applies to stock.

The shape checks always run. The stock checks read each installed stock HP
Click's app.asar, apply that version's manifest through PatchEngine in memory,
and skip when the version is not installed. Nothing is written into any app,
and nothing is launched.

Why each check exists (all found 19 Sep 2026):
- Every release before 1.5.8 set crashAutoSubmit on the ROOT package.json,
  whose copy of that key nothing reads (Electron reads the file for "main" and
  the version). app/main.js require()s app/package.json and passes
  hp_configs.crashAutoSubmit to crashReporter.start, so every copy went on
  trying to upload crash dumps.
- HP Click 4.8 and 4.10 log the SNMPv3 user name and both passwords. HP
  replaced the line in 4.11.31; the manifests use HP's line verbatim.
- 4.10.42's index.html loads only jquery.js and bundle.js, so the shared/
  classic-script fix does nothing there; the 4.8.x pages still load it.
Target: Python 3.9+
"""

import hashlib
import json
import os
import plistlib
import shutil
import subprocess

import pytest

from clickgraft.asar import AsarArchive
from clickgraft.manifest import ManifestManager
from clickgraft.patches import PatchEngine
from tests.test_clickgraft import find_stock_bundle

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MM = ManifestManager(os.path.join(ROOT, "manifests"))
VERSIONS = sorted(MM.manifests)

# Spelled out here rather than imported from manifest_guard: this file is the
# specification the guard's allowlist is checked against, not a copy of it.
SNMP_ANCHOR = ('this.log("credentials key enter with - authenticationPassword: "'
               '+this.authenticationPassword+" policyPassword: "+this.policyPassword'
               '+" userName: "+this.userName)')
SNMP_HP_LINE = 'this.log("credentials key enter event received")'


def _vtuple(version):
    return tuple(int(x) for x in version.split("."))


def _ops(manifest, path):
    return [op for p in manifest["patches"] if p["path"] == path for op in p["ops"]]


# --- shape: always runs --------------------------------------------------------

def test_manifests_load_cleanly():
    assert MM.load_errors == {}
    assert {"4.8.117", "4.8.118", "4.10.42"} <= set(VERSIONS)


@pytest.mark.parametrize("version", VERSIONS)
def test_crash_reports_are_switched_off_where_they_are_read(version):
    m = MM.manifests[version]
    ops = _ops(m, "app/package.json")
    assert ops == [{"type": "json_set", "path": "hp_configs.crashAutoSubmit", "value": False}]
    assert ops[0]["value"] is False
    assert _ops(m, "package.json") == [], "the root package.json is read by nothing"


@pytest.mark.parametrize("version", [v for v in VERSIONS if _vtuple(v) < (4, 11)])
def test_snmp_credentials_leave_the_log_below_4_11(version):
    ops = _ops(MM.manifests[version], "app/bundle.js")
    assert {"type": "replace", "anchor": SNMP_ANCHOR, "replacement": SNMP_HP_LINE} in ops


@pytest.mark.parametrize("version", VERSIONS)
def test_no_manifest_repeats_a_path(version):
    paths = [p["path"] for p in MM.manifests[version]["patches"]]
    assert len(paths) == len(set(paths)), paths


def test_shared_fix_only_where_index_html_loads_it():
    assert [p["path"] for p in MM.manifests["4.10.42"]["patches"]
            if p["path"].startswith("app/shared/")] == []
    for version in ("4.8.117", "4.8.118"):
        paths = {p["path"] for p in MM.manifests[version]["patches"]}
        assert {"app/shared/constants.js", "app/shared/industries.js"} <= paths, version


# --- stock: runs when the stock version is installed -----------------------------

def _stock_archive(version, manifest):
    app = find_stock_bundle(version)
    if app is None:
        pytest.skip(f"no stock HP Click {version} in /Applications")
    archive = AsarArchive(os.path.join(app, "Contents", "Resources", "app.asar"))
    if hashlib.sha256(archive.raw_bytes).hexdigest() != manifest["asar_sha256"]:
        pytest.skip(f"{app} is not the stock {version} build its manifest names")
    return archive


def _node_check(tmp_path, rel_path, data):
    """node --check on the patched file, as CommonJS so Node does not guess."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    d = tmp_path / rel_path.replace("/", "__")
    d.mkdir()
    (d / "package.json").write_text('{"type": "commonjs"}')
    f = d / os.path.basename(rel_path)
    f.write_bytes(data)
    out = subprocess.run([node, "--check", str(f)], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, f"{rel_path}: {out.stderr[-2000:]}"


@pytest.mark.parametrize("version", VERSIONS)
def test_manifest_applies_to_stock(version, tmp_path):
    manifest = MM.manifests[version]
    archive = _stock_archive(version, manifest)
    nodes = archive.get_all_file_nodes()
    engine = PatchEngine(manifest["patches"])

    for patch in manifest["patches"]:
        path = patch["path"]
        # What patch_and_repack_asar now requires: a packed file in the archive.
        assert path in nodes, f"{path} is not in stock {version}"
        assert not nodes[path].get("unpacked"), f"{path} is unpacked in stock {version}"
        original = archive.read_file_content(nodes[path])
        text = original.decode("utf-8")
        for op in patch["ops"]:
            if op["type"] == "replace":
                assert text.count(op["anchor"]) == 1, (path, op["anchor"][:80])

        patched = engine.apply_patches_for_path(path, original)
        assert patched != original, f"{path}: the ops changed nothing"

        if path == "app/package.json":
            assert json.loads(text)["hp_configs"]["crashAutoSubmit"] is True
            assert json.loads(patched)["hp_configs"]["crashAutoSubmit"] is False
        elif path.endswith(".js"):
            _node_check(tmp_path, path, patched)

        if path == "app/bundle.js":
            patched_text = patched.decode("utf-8")
            assert SNMP_ANCHOR not in patched_text
            assert patched_text.count(SNMP_HP_LINE) == 1


def _hp_installed(version):
    """HP's own install of `version` (bundle id com.hp.hpclick; ClickGraft
    copies are com.hp.hpclick.arm64), whatever its architecture."""
    import glob
    for app in sorted(glob.glob("/Applications/*Click*.app")):
        try:
            with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
                info = plistlib.load(f)
        except (OSError, plistlib.InvalidFileException):
            continue
        if (info.get("CFBundleShortVersionString") == version
                and info.get("CFBundleIdentifier") == "com.hp.hpclick"):
            return app
    return None


def test_snmp_replacement_is_hps_own_4_11_31_line():
    """The back-port is only as good as "verbatim": HP's 4.11.31 must carry the
    replacement once and the credential line not at all. find_stock_bundle
    rejects any arm64 slice, and 4.11.31 is HP's own universal build, so it is
    found by bundle id instead."""
    app = _hp_installed("4.11.31")
    if app is None:
        pytest.skip("no stock HP Click 4.11.31 in /Applications")
    archive = AsarArchive(os.path.join(app, "Contents", "Resources", "app.asar"))
    bundle = archive.read_file_content(archive.get_all_file_nodes()["app/bundle.js"]).decode("utf-8")
    assert bundle.count(SNMP_ANCHOR) == 0
    assert bundle.count(SNMP_HP_LINE) == 1
