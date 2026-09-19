"""
tests/test_patch_guards.py — A patch that cannot apply, or must not, stops the build.

Three guards added in 1.5.8, each for a way a manifest used to fail silently:
- PatchEngine: a path listed twice kept only the last entry's ops. Its older
  contracts -- an anchor that must occur exactly once, a json_set path that
  must already exist -- are covered here too, because until 1.5.8 they were
  only ever reached through tests that now stop at the guard instead.
- patch_and_repack_asar: a path absent from the archive, or stored unpacked,
  was skipped without a word and the copy simply lacked the fix.
- manifest_guard: nothing stopped an op that is not on the allowlist from
  shipping, because nothing on the release path runs these tests. So it runs in
  build.py and in packaging/build_app.sh, and these tests hold both call sites
  in place.

The never-distribute signatures live outside the repository (see
manifest_guard's docstring), so the tests for that lock write their own file
with invented signatures. Nothing here names HP code that is not already in a
shipped manifest.

No HP Click is needed, launched or touched: the archives here are built in a
temporary directory.
Target: Python 3.9+
"""

import copy
import glob
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess

import pytest

from clickgraft import manifest_guard
from clickgraft.asar import AsarArchive, patch_and_repack_asar
from clickgraft.manifest_guard import ManifestGuardError, check_manifest, forbidden, problems
from clickgraft.patches import PatchEngine

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFESTS = os.path.join(ROOT, "manifests")
BUILD_APP = os.path.join(ROOT, "packaging", "build_app.sh")


def _manifests():
    out = {}
    for path in sorted(glob.glob(os.path.join(MANIFESTS, "*.json"))):
        with open(path, encoding="utf-8") as f:
            out[os.path.basename(path)] = json.load(f)
    return out


# --- a tiny archive -------------------------------------------------------

def _write_asar(asar_path, files, unpacked=()):
    """An asar holding `files` ({rel_path: bytes}), laid out the way
    AsarArchive reads HP's. Paths in `unpacked` get a header entry marked
    unpacked, no bytes in the blob, and a file under <asar>.unpacked/."""
    header = {"files": {}}
    blob = bytearray()
    for rel_path, data in files.items():
        parts = rel_path.split("/")
        d = header
        for part in parts[:-1]:
            d = d["files"].setdefault(part, {"files": {}})
        sha = hashlib.sha256(data).hexdigest()
        integrity = {"algorithm": "SHA256", "hash": sha, "blockSize": 4 * 1024 * 1024, "blocks": [sha]}
        if rel_path in unpacked:
            d["files"][parts[-1]] = {"size": len(data), "unpacked": True, "integrity": integrity}
            on_disk = os.path.join(asar_path + ".unpacked", rel_path)
            os.makedirs(os.path.dirname(on_disk), exist_ok=True)
            with open(on_disk, "wb") as f:
                f.write(data)
        else:
            d["files"][parts[-1]] = {"size": len(data), "offset": str(len(blob)), "integrity": integrity}
            blob.extend(data)
    header_json = json.dumps(header).encode("utf-8")
    pad = (4 - len(header_json) % 4) % 4
    with open(asar_path, "wb") as f:
        f.write(struct.pack("<IIII", 4, len(header_json) + pad + 8, len(header_json) + pad + 4, len(header_json)))
        f.write(header_json + b"\x00" * pad + bytes(blob))


TINY_FILES = {
    "package.json": b'{"name": "root"}',
    "app/package.json": b'{"hp_configs": {"crashAutoSubmit": true}}',
    "app/main.js": b'const packagejson = require("./package.json");\n',
    "app/native/addon.node": b"\xcf\xfa\xed\xfe not really a Mach-O",
}
CRASH_OP = {"type": "json_set", "path": "hp_configs.crashAutoSubmit", "value": False}


@pytest.fixture
def tiny_asar(tmp_path):
    src = str(tmp_path / "src" / "app.asar")
    os.makedirs(os.path.dirname(src))
    _write_asar(src, TINY_FILES, unpacked={"app/native/addon.node"})
    # build.py ditto's the whole source bundle, app.asar.unpacked/ included,
    # before the repack checks it; the test does that part itself.
    dst = str(tmp_path / "dst" / "app.asar")
    shutil.copytree(src + ".unpacked", dst + ".unpacked")
    manifest = {"asar_entries": {"packed": 3, "unpacked": 1}}
    return src, dst, manifest


# --- PatchEngine: one entry per file --------------------------------------

def test_a_path_listed_twice_raises():
    patches = [
        {"path": "app/package.json", "ops": [CRASH_OP]},
        {"path": "app/main.js", "ops": [{"type": "append", "text": "\n"}]},
        {"path": "app/package.json", "ops": [{"type": "append", "text": "\n"}]},
    ]
    with pytest.raises(ValueError) as e:
        PatchEngine(patches)
    assert "app/package.json" in str(e.value)
    assert "more than once" in str(e.value)


def test_distinct_paths_still_load():
    engine = PatchEngine([{"path": "app/package.json", "ops": [CRASH_OP]},
                          {"path": "app/main.js", "ops": []}])
    assert sorted(engine.get_patched_paths()) == ["app/main.js", "app/package.json"]


# --- PatchEngine: the anchor and json_set contracts ------------------------
#
# Straight at PatchEngine, not through a manifest. The manifest_guard tests
# below reject a bad op before the engine ever sees it, so these contracts --
# the exactly-once anchor above all, which is what makes a manifest for a new
# HP version safe -- would otherwise have nothing checking them.

def _engine(path, op):
    return PatchEngine([{"path": path, "ops": [op]}])


@pytest.mark.parametrize("body, count", [
    (b"nothing like the anchor here", 0),
    (b"anchorHere(); and again anchorHere();", 2),
])
def test_an_anchor_not_found_exactly_once_raises(body, count):
    engine = _engine("app/bundle.js", {"type": "replace", "anchor": "anchorHere();",
                                       "replacement": "x();"})
    with pytest.raises(ValueError) as e:
        engine.apply_patches_for_path("app/bundle.js", body)
    assert f"occurred {count} times (expected exactly 1)" in str(e.value)
    assert "app/bundle.js" in str(e.value)


def test_an_anchor_found_once_is_replaced():
    engine = _engine("app/bundle.js", {"type": "replace", "anchor": "anchorHere();",
                                       "replacement": "x();"})
    assert engine.apply_patches_for_path("app/bundle.js", b"a(); anchorHere(); b();") \
        == b"a(); x(); b();"


def test_json_set_with_a_missing_parent_raises():
    engine = _engine("app/package.json", {"type": "json_set",
                                          "path": "hp_configsTYPO.crashAutoSubmit",
                                          "value": False})
    with pytest.raises(ValueError) as e:
        engine.apply_patches_for_path("app/package.json", TINY_FILES["app/package.json"])
    assert "parent segment 'hp_configsTYPO'" in str(e.value) and "does not exist" in str(e.value)


def test_json_set_with_a_missing_leaf_raises_without_create():
    engine = _engine("app/package.json", {"type": "json_set",
                                          "path": "hp_configs.crashAutoSubmitTYPO",
                                          "value": False})
    with pytest.raises(ValueError) as e:
        engine.apply_patches_for_path("app/package.json", TINY_FILES["app/package.json"])
    assert "leaf key 'crashAutoSubmitTYPO'" in str(e.value)


def test_json_set_on_a_file_that_is_not_json_raises():
    engine = _engine("app/main.js", dict(CRASH_OP))
    with pytest.raises(ValueError, match="not valid JSON"):
        engine.apply_patches_for_path("app/main.js", TINY_FILES["app/main.js"])


def test_an_unknown_op_type_raises():
    engine = _engine("app/bundle.js", {"type": "prepend", "text": "x"})
    with pytest.raises(ValueError, match="Unknown patch op type"):
        engine.apply_patches_for_path("app/bundle.js", b"a();")


# --- patch_and_repack_asar: every path is reached --------------------------

def test_control_a_reachable_path_is_patched(tiny_asar):
    """The control: this archive and this call really do patch, so the two
    failures below are about the paths and not about the harness."""
    src, dst, manifest = tiny_asar
    patch_and_repack_asar(src, dst, PatchEngine([{"path": "app/package.json", "ops": [CRASH_OP]}]), manifest)
    rebuilt = AsarArchive(dst)
    pkg = json.loads(rebuilt.read_file_content(rebuilt.get_all_file_nodes()["app/package.json"]))
    assert pkg["hp_configs"]["crashAutoSubmit"] is False


@pytest.mark.parametrize("path, reason", [
    ("app/no-such-file.js", "not a file in this app.asar"),
    ("app/native", "not a file in this app.asar"),              # a directory
    ("app/native/addon.node", "stored unpacked"),
])
def test_a_path_that_cannot_be_patched_raises_and_writes_nothing(tiny_asar, path, reason):
    src, dst, manifest = tiny_asar
    engine = PatchEngine([{"path": "app/package.json", "ops": [CRASH_OP]},
                          {"path": path, "ops": [{"type": "append", "text": "\n"}]}])
    with pytest.raises(ValueError) as e:
        patch_and_repack_asar(src, dst, engine, manifest)
    assert path in str(e.value) and reason in str(e.value)
    assert not os.path.exists(dst)


# --- manifest_guard: the shipped manifests ---------------------------------

@pytest.mark.parametrize("name", sorted(_manifests()))
def test_every_shipped_manifest_passes_the_guard(name):
    manifest = _manifests()[name]
    assert problems(manifest) == []
    check_manifest(manifest)


def test_the_manifests_directory_passes():
    assert manifest_guard.check_manifests_dir(MANIFESTS) == []


def test_every_allowlisted_op_is_in_use():
    """An allowlist entry no manifest uses is a hole waiting for a manifest."""
    used = {manifest_guard._canonical(p["path"], op)
            for m in _manifests().values() for p in m["patches"] for op in p["ops"]}
    for path, op in manifest_guard.ALLOWED_OPS:
        assert manifest_guard._canonical(path, op) in used, (path, op)


# --- manifest_guard: what it refuses -----------------------------------------

def _with_op(path, op, base="4.8.117.json"):
    """A shipped manifest plus one op, added to the entry for `path` if there
    is one, so the only problem is the op."""
    manifest = copy.deepcopy(_manifests()[base])
    for patch in manifest["patches"]:
        if patch["path"] == path:
            patch["ops"].append(op)
            return manifest
    manifest["patches"].append({"path": path, "why": "test", "ops": [op]})
    return manifest


# --- manifest_guard: the optional second lock --------------------------------
#
# The real signatures are not in the repository (manifest_guard's docstring says
# why), so these use invented ones and test the mechanism: where in an op a
# signature is looked for, how a name's spelling is normalised, that a match
# beats the allowlist, and that a named file which cannot be read is an error
# rather than a lock that quietly does nothing.

SIGNATURES = "\n".join([
    "# label<TAB>regex, as the owner's own file is written",
    "frobnicator\tfrobnicat",
    "widgetPolicy\twidgetpolicy",
    "widget key\twidget(?!ry)",
])


@pytest.fixture
def signatures(tmp_path, monkeypatch):
    path = tmp_path / "never-distribute.txt"
    path.write_text(SIGNATURES, encoding="utf-8")
    monkeypatch.setenv(manifest_guard.NEVER_DISTRIBUTE_ENV, str(path))
    return str(path)


# Inert on purpose: each only has to carry the signature, not do anything.
@pytest.mark.parametrize("path, op, label", [
    ("app/bundle.js", {"type": "replace", "anchor": "frobnicate(x)", "replacement": "x"}, "frobnicator"),
    ("app/bundle.js", {"type": "replace", "anchor": "x", "replacement": "frobnicate(x)"}, "frobnicator"),
    ("app/node/services/frobnicator-service.js", {"type": "append", "text": "\n"}, "frobnicator"),
    ("app/bundle.js", {"type": "append", "text": "widget-policy"}, "widgetPolicy"),
    ("app/bundle.js", {"type": "append", "text": "widget_Policy"}, "widgetPolicy"),
    ("app/package.json", {"type": "json_set", "path": "hp_configs.frobnication", "value": False},
     "frobnicator"),
    ("app/package.json", {"type": "json_set", "path": "hp_configs.crashAutoSubmit",
                          "value": "frobnicate"}, "frobnicator"),
])
def test_never_distribute_signatures_fail(signatures, path, op, label):
    found = problems(_with_op(path, op))
    assert len(found) == 1 and f"never-distribute signature '{label}'" in found[0], found
    with pytest.raises(ManifestGuardError):
        check_manifest(_with_op(path, op))


def test_never_distribute_wins_even_when_allowlisted(signatures, monkeypatch):
    """Adding the op to ALLOWED_OPS as well must not be enough."""
    path = "app/bundle.js"
    op = {"type": "replace", "anchor": "frobnicate(x)", "replacement": "frobnicate(y)"}
    monkeypatch.setattr(manifest_guard, "_ALLOWED",
                        manifest_guard._ALLOWED | {manifest_guard._canonical(path, op)})
    found = problems(_with_op(path, op))
    assert found and "never-distribute" in found[0]


def test_a_signature_can_be_written_not_to_catch_a_near_neighbour(signatures):
    # "widget(?!ry)": the point is that a lookahead works, so a signature for
    # one name need not catch a different one that starts the same way.
    assert forbidden("widget") == "widget key"
    assert forbidden("widgetry") is None


def test_the_shipped_manifests_pass_these_signatures_too(signatures):
    for name, manifest in _manifests().items():
        assert problems(manifest) == [], name


def test_with_no_signature_file_the_allowlist_is_the_only_lock(monkeypatch):
    monkeypatch.delenv(manifest_guard.NEVER_DISTRIBUTE_ENV, raising=False)
    assert manifest_guard.load_never_distribute() == []
    assert forbidden("frobnicate(x)") is None
    # And an op that is not on the allowlist still fails, which is the lock
    # that ships.
    found = problems(_with_op("app/bundle.js", {"type": "replace", "anchor": "frobnicate(x)",
                                                "replacement": "x"}))
    assert len(found) == 1 and "not on the allowlist" in found[0], found


@pytest.mark.parametrize("content, expect", [
    (None, "No such file"),                      # named but missing
    ("bad\t[unclosed", "not a valid regex"),
])
def test_a_named_signature_file_that_cannot_be_used_raises(tmp_path, monkeypatch, content, expect):
    path = tmp_path / "never-distribute.txt"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    monkeypatch.setenv(manifest_guard.NEVER_DISTRIBUTE_ENV, str(path))
    manifest_guard._PATTERN_CACHE.pop(str(path), None)
    with pytest.raises((OSError, ValueError)) as e:
        problems(_manifests()["4.8.117.json"])
    assert expect in str(e.value)


def _crash_op_of(manifest):
    return next(p for p in manifest["patches"] if p["path"] == "app/package.json")["ops"][0]


@pytest.mark.parametrize("change", [
    "new_file", "new_anchor", "changed_replacement", "changed_value", "value_0_for_false",
    "extra_field", "root_package_json",
])
def test_an_unlisted_op_fails(change):
    manifest = copy.deepcopy(_manifests()["4.8.117.json"])
    updater = next(p for p in manifest["patches"] if p["path"] == "app/node/main/app-updater.js")
    if change == "new_file":
        manifest["patches"].append({"path": "app/node/main/app-starter.js",
                                    "ops": [{"type": "append", "text": "\n"}]})
    elif change == "new_anchor":
        updater["ops"].append({"type": "replace", "anchor": "function respondTo", "replacement": "x"})
    elif change == "changed_replacement":
        updater["ops"][0]["replacement"] = "function startup(e){return;;"
    elif change == "changed_value":
        _crash_op_of(manifest)["value"] = True
    elif change == "value_0_for_false":
        _crash_op_of(manifest)["value"] = 0
    elif change == "extra_field":
        _crash_op_of(manifest)["create"] = True
    elif change == "root_package_json":
        manifest["patches"].append({"path": "package.json", "ops": [dict(CRASH_OP)]})
    found = problems(manifest)
    assert len(found) == 1 and "not on the allowlist" in found[0], found
    with pytest.raises(ManifestGuardError) as e:
        check_manifest(manifest)
    assert "4.8.117" in str(e.value)


def test_a_repeated_path_fails_the_guard_too():
    manifest = copy.deepcopy(_manifests()["4.8.117.json"])
    manifest["patches"].append(copy.deepcopy(manifest["patches"][0]))
    assert any("listed more than once" in line for line in problems(manifest))


# --- where it is enforced ----------------------------------------------------

def test_build_refuses_before_fetching_anything(tmp_path):
    """build_apple_silicon_bundle checks the guard straight after validating
    the manifest, before the stock-source check and the Electron download.
    The source here is an empty folder, so reaching any later step would fail
    differently."""
    from clickgraft.build import build_apple_silicon_bundle
    source = tmp_path / "Source.app"
    source.mkdir()
    manifest = _with_op("app/bundle.js", {"type": "replace", "anchor": "frobnicate(x)",
                                          "replacement": "x"})
    with pytest.raises(ManifestGuardError):
        build_apple_silicon_bundle(str(source), str(tmp_path / "Out.app"), manifest=manifest,
                                   allow_foreign_host=True)
    assert sorted(os.listdir(tmp_path)) == ["Source.app"]


def _guard_block():
    with open(BUILD_APP, encoding="utf-8") as f:
        src = f.read()
    m = re.search(r"^# --- patch guard -+\n.*?(?=^rm -rf \"\$APP\")", src, re.S | re.M)
    assert m, "build_app.sh no longer has its patch guard step before rm -rf \"$APP\""
    return src, m


def test_build_app_runs_the_guard_before_copying_manifests():
    src, m = _guard_block()
    assert "clickgraft.manifest_guard manifests" in m.group(0)
    assert m.end() < src.index('rsync -a "$ROOT/manifests"')
    assert m.end() < src.index("swiftc")


def _run_guard_step(tmp_path, manifests, env=None):
    """Only the guard step of build_app.sh, against a copy of the repo that
    has `manifests` ({filename: dict or raw str}) as its manifests/."""
    _src, m = _guard_block()
    root = tmp_path / "root"
    (root / "manifests").mkdir(parents=True)
    os.symlink(os.path.join(ROOT, "clickgraft"), root / "clickgraft")
    for name, content in manifests.items():
        text = content if isinstance(content, str) else json.dumps(content, indent=2)
        (root / "manifests" / name).write_text(text, encoding="utf-8")
    script = 'set -euo pipefail\nROOT="$1"\n' + m.group(0) + "\necho GUARD-PASSED\n"
    run_env = dict(os.environ)
    run_env.pop(manifest_guard.NEVER_DISTRIBUTE_ENV, None)
    run_env.update(env or {})
    return subprocess.run(["/bin/bash", "-c", script, "build_app.sh", str(root)],
                          capture_output=True, text=True, timeout=60, env=run_env)


def test_build_app_guard_step_passes_the_shipped_manifests(tmp_path):
    """The control for the ones below."""
    out = _run_guard_step(tmp_path, _manifests())
    assert out.returncode == 0, out.stderr
    assert "GUARD-PASSED" in out.stdout


@pytest.mark.parametrize("bad, expect", [
    ("unlisted", "not on the allowlist"),
    ("unparseable", "could not be read"),
])
def test_build_app_guard_step_fails_a_bad_manifests_dir(tmp_path, bad, expect):
    manifests = _manifests()
    if bad == "unlisted":
        manifests["4.8.117.json"] = _with_op("app/bundle.js", {
            "type": "replace", "anchor": "credentialsKeyEnter", "replacement": "credentialsKeyEnter"})
    else:
        manifests["4.8.117.json"] = '{"app_version": "4.8.117", TRUNCATED'
    out = _run_guard_step(tmp_path, manifests)
    assert out.returncode != 0
    assert "GUARD-PASSED" not in out.stdout
    assert "4.8.117.json" in out.stderr and expect in out.stderr, out.stderr


def test_build_app_guard_step_uses_a_signature_file_when_the_environment_names_one(tmp_path):
    """The owner's own signatures reach the release build through the
    environment, and the step says so when it passes."""
    sig = tmp_path / "never-distribute.txt"
    sig.write_text(SIGNATURES, encoding="utf-8")
    env = {manifest_guard.NEVER_DISTRIBUTE_ENV: str(sig)}

    passed = _run_guard_step(tmp_path / "ok", _manifests(), env=env)
    assert passed.returncode == 0, passed.stderr
    assert "never-distribute signature(s)" in passed.stdout

    manifests = _manifests()
    manifests["4.8.117.json"] = _with_op("app/bundle.js", {
        "type": "replace", "anchor": "frobnicate(x)", "replacement": "x"})
    out = _run_guard_step(tmp_path / "bad", manifests, env=env)
    assert out.returncode != 0 and "GUARD-PASSED" not in out.stdout
    assert "never-distribute signature 'frobnicator'" in out.stderr, out.stderr
