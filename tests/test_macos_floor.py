"""
tests/test_macos_floor.py — Which macOS a copy says it needs, and whether that is true.

Until 1.5.9 every copy said HP's LSMinimumSystemVersion 12.0 whatever went into
it. On 22 Sep 2026 the bottle a build downloaded was the first one Homebrew
listed, arm64_golden_gate (built on macOS 27); the ones already cached were
tahoe (minos 26.0); and that libidn2 imports _strchrnul, new in macOS 15.4. So
a copy aborted at launch on macOS 12.0-15.3 while claiming 12.0. These tests
hold the replacement: the bottle for the oldest macOS Homebrew publishes, no
library newer than the build's floor from any source, a cache that must still
match what was fetched, and a copy whose Info.plist states the highest minimum
anything in it declares.

No network. Homebrew is stood in for through deps._http_get, and the Mach-O
files are tiny dylibs compiled here with clang at chosen deployment targets.
The one test that needs a stock HP Click skips without one.
Target: Python 3.9+
"""

import hashlib
import io
import json
import os
import plistlib
import shutil
import subprocess
import tarfile
import urllib.error

import pytest

from clickgraft import deps, hostarch, macos_floor
from clickgraft.agent import macos_floor_plan
from clickgraft.macos_floor import (MacOSTooOldError, declared_minimum, exact_floor,
                                    macho_minimum, plan_floor, refuse_if_too_old,
                                    stamp_minimum)
from clickgraft.verify import check_minimum_macos

needs_clang = pytest.mark.skipif(shutil.which("clang") is None or shutil.which("vtool") is None,
                                 reason="needs the Command Line Tools (clang, vtool)")


def _dylib(path, minos, arch="arm64", body="int clickgraft_test(void) { return 1; }"):
    """A real dylib declaring `minos`, compiled here."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    src = path + ".c"
    with open(src, "w") as f:
        f.write(body + "\n")
    subprocess.run(["clang", "-arch", arch, "-dynamiclib", f"-mmacosx-version-min={minos}",
                    "-install_name", f"@rpath/{os.path.basename(path)}", "-o", path, src],
                   check=True, capture_output=True)
    os.remove(src)
    return path


def _app(root, declared="12.0", name="Test.app"):
    app = os.path.join(root, name)
    os.makedirs(os.path.join(app, "Contents"), exist_ok=True)
    plist = {"CFBundleIdentifier": "com.example.test"}
    if declared is not None:
        plist["LSMinimumSystemVersion"] = declared
    with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
        plistlib.dump(plist, f)
    return app


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# ---------------------------------------------------------------------------
# Bottle choice


# The shape of formulae.brew.sh/api/formula/libidn2.json on 22 Sep 2026, in
# the order Homebrew lists it: the first key used to win.
LIBIDN2_FILES = {t: {"url": f"https://ghcr.invalid/{t}", "sha256": t}
                 for t in ("arm64_golden_gate", "arm64_tahoe", "arm64_sequoia",
                           "arm64_sonoma", "arm64_ventura", "sequoia", "arm64_linux",
                           "x86_64_linux")}


def test_the_oldest_macos_bottle_wins_not_the_first_listed():
    assert deps.choose_bottle(LIBIDN2_FILES, "libidn2") == ("arm64_ventura", (13, 0, 0))
    three = {t: LIBIDN2_FILES[t] for t in ("arm64_golden_gate", "arm64_tahoe",
                                             "arm64_sequoia", "arm64_linux", "x86_64_linux")}
    assert deps.choose_bottle(three, "libunistring") == ("arm64_sequoia", (15, 0, 0))


def test_linux_and_intel_bottles_are_never_chosen():
    for tag in ("arm64_linux", "x86_64_linux", "sequoia", "sonoma", "ventura", "monterey"):
        assert deps.bottle_macos(tag) is None, tag
    with pytest.raises(ValueError) as exc:
        deps.choose_bottle({"arm64_linux": {}, "x86_64_linux": {}, "sequoia": {}}, "libfoo")
    assert "libfoo" in str(exc.value) and "arm64_linux" in str(exc.value)


def test_an_unknown_tag_is_a_newer_macos_than_any_known_one():
    files = {"arm64_somewhere_new": {}, "arm64_tahoe": {}, "arm64_golden_gate": {}}
    assert deps.choose_bottle(files, "x") == ("arm64_tahoe", (26, 0, 0))
    assert deps.bottle_macos("arm64_somewhere_new") > deps.bottle_macos("arm64_golden_gate")


def test_all_is_accepted():
    assert deps.choose_bottle({"all": {}}, "x") == ("all", (0, 0, 0))
    assert deps.choose_bottle({"all": {}, "x86_64_linux": {}, "arm64_linux": {}}, "x")[0] == "all"


class FakeHomebrew:
    """Stands in for deps._http_get: the formula API and the bottle CDN."""

    def __init__(self):
        self.formulae = {}        # formula -> files dict
        self.blobs = {}           # url -> bytes
        self.calls = []
        self.offline = False

    def add_bottle(self, formula, tag, dylib_path, member_name):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(dylib_path, arcname=f"{formula}/1.0/lib/{member_name}")
            link = tarfile.TarInfo(f"{formula}/1.0/lib/{member_name.split('.')[0]}.dylib")
            link.type, link.linkname = tarfile.SYMTYPE, member_name
            tar.addfile(link)
        blob = buf.getvalue()
        url = f"https://ghcr.invalid/{formula}/{tag}"
        self.blobs[url] = blob
        self.formulae.setdefault(formula, {})[tag] = {
            "url": url, "sha256": hashlib.sha256(blob).hexdigest()}

    def __call__(self, url, headers=None, timeout=60):
        self.calls.append(url)
        if self.offline:
            raise urllib.error.URLError("offline")
        api = "https://formulae.brew.sh/api/formula/"
        if url.startswith(api):
            formula = url[len(api):-len(".json")]
            return json.dumps({"bottle": {"stable": {"files": self.formulae[formula]}}}).encode()
        return self.blobs[url]


@pytest.fixture
def brew(tmp_path, monkeypatch):
    fake = FakeHomebrew()
    monkeypatch.setattr(deps, "_http_get", fake)
    monkeypatch.setattr(deps, "find_local_brew_dylib", lambda name: None)
    return fake


INFO = {"name": "libfake.1.dylib", "brew_formula": "libfake"}
FLOOR = (15, 0, 0)


@pytest.fixture
def published(tmp_path, brew):
    """libfake published for ventura (a 13.0 dylib), tahoe (26.0) and Linux."""
    src = tmp_path / "src"
    brew.add_bottle("libfake", "arm64_tahoe", _dylib(str(src / "t" / "libfake.1.dylib"), "26.0"),
                    "libfake.1.dylib")
    brew.add_bottle("libfake", "arm64_ventura", _dylib(str(src / "v" / "libfake.1.dylib"), "13.0"),
                    "libfake.1.dylib")
    brew.add_bottle("libfake", "arm64_linux", _dylib(str(src / "l" / "libfake.1.dylib"), "11.0"),
                    "libfake.1.dylib")
    return brew


@needs_clang
def test_choose_bottles_reads_the_api_and_picks_the_oldest(tmp_path, published):
    got = deps.choose_bottles({"required_dylibs": [INFO]}, cache_dir=str(tmp_path))
    assert got["libfake"]["tag"] == "arm64_ventura"
    assert got["libfake"]["macos"] == (13, 0, 0)


@needs_clang
def test_a_download_records_its_hash_and_bottle(tmp_path, published):
    cache = str(tmp_path / "cache")
    os.makedirs(cache)
    got = deps.resolve_dylib(INFO, cache_dir=cache, floor=FLOOR)
    assert got["source"] == "download" and got["bottle_tag"] == "arm64_ventura"
    assert got["minos"] == (13, 0, 0)
    with open(got["path"] + ".clickgraft.json") as f:
        meta = json.load(f)
    assert meta["sha256"] == _sha(got["path"])
    assert meta["bottle_tag"] == "arm64_ventura" and meta["formula"] == "libfake"

    published.calls.clear()
    again = deps.resolve_dylib(INFO, cache_dir=cache, floor=FLOOR)
    assert again["source"] == "cache" and again["path"] == got["path"]
    assert published.calls == [], "an intact cache must not be fetched again"


def _prime_cache(cache, dylib_src, record=True, tag="arm64_tahoe"):
    os.makedirs(cache, exist_ok=True)
    cached = os.path.join(cache, INFO["name"])
    shutil.copy2(dylib_src, cached)
    if record:
        with open(cached + ".clickgraft.json", "w") as f:
            json.dump({"formula": "libfake", "bottle_tag": tag, "sha256": _sha(cached)}, f)
    return cached


@needs_clang
def test_a_cached_dylib_newer_than_the_floor_is_evicted_and_fetched_again(tmp_path, published):
    """What every cache held on 22 Sep 2026: tahoe bottles, minos 26.0."""
    cache = str(tmp_path / "cache")
    cached = _prime_cache(cache, _dylib(str(tmp_path / "old" / "libfake.1.dylib"), "26.0"))
    got = deps.resolve_dylib(INFO, cache_dir=cache, floor=FLOOR)
    assert got["source"] == "download" and got["path"] == cached
    assert macho_minimum(cached) == (13, 0, 0)


@needs_clang
@pytest.mark.parametrize("damage", ["truncate", "alter", "no_record"])
def test_a_damaged_or_unrecorded_cache_is_fetched_again(tmp_path, published, damage):
    """A libidn2 cut off at 4096 bytes used to be accepted: its first bytes
    were an arm64 Mach-O header, and that was all the cache checked."""
    cache = str(tmp_path / "cache")
    good = deps.resolve_dylib(INFO, cache_dir=cache, floor=FLOOR)["path"]
    full = open(good, "rb").read()
    if damage == "truncate":
        with open(good, "wb") as f:
            f.write(full[:4096])
    elif damage == "alter":
        with open(good, "wb") as f:
            f.write(full[:-16] + bytes(b ^ 0xFF for b in full[-16:]))
    else:
        os.remove(good + ".clickgraft.json")
    assert "arm64" in deps._archs_of(good), "the damaged file still looks like arm64"

    published.calls.clear()
    got = deps.resolve_dylib(INFO, cache_dir=cache, floor=FLOOR)
    assert got["source"] == "download"
    assert open(got["path"], "rb").read() == full
    assert any("ghcr.invalid" in c for c in published.calls)


@needs_clang
def test_local_homebrew_newer_than_the_floor_is_skipped(tmp_path, published, monkeypatch):
    """The development Mac's Homebrew libraries declare 26.0."""
    too_new = _dylib(str(tmp_path / "brew" / "new" / "libfake.1.dylib"), "26.0")
    monkeypatch.setattr(deps, "find_local_brew_dylib", lambda name: too_new)
    got = deps.resolve_dylib(INFO, cache_dir=str(tmp_path / "cache"), floor=FLOOR)
    assert got["source"] == "download" and got["path"] != too_new

    fine = _dylib(str(tmp_path / "brew" / "ok" / "libfake.1.dylib"), "14.0")
    monkeypatch.setattr(deps, "find_local_brew_dylib", lambda name: fine)
    got = deps.resolve_dylib(INFO, cache_dir=str(tmp_path / "cache2"), floor=FLOOR)
    assert got["source"] == "homebrew" and got["path"] == fine


@needs_clang
def test_a_download_newer_than_the_floor_is_refused(tmp_path, brew):
    """A bottle whose file declares more than its tag promises."""
    brew.add_bottle("libfake", "arm64_ventura",
                    _dylib(str(tmp_path / "src" / "libfake.1.dylib"), "26.0"), "libfake.1.dylib")
    cache = str(tmp_path / "cache")
    os.makedirs(cache)
    with pytest.raises(ValueError) as exc:
        deps.resolve_dylib(INFO, cache_dir=cache, floor=FLOOR)
    assert "26.0" in str(exc.value) and "15.0" in str(exc.value)
    assert not os.path.exists(os.path.join(cache, INFO["name"]))


@needs_clang
def test_offline_a_recorded_cache_keeps_its_bottle(tmp_path, published):
    cache = str(tmp_path / "cache")
    deps.resolve_dylib(INFO, cache_dir=cache, floor=FLOOR)
    published.offline = True
    got = deps.choose_bottles({"required_dylibs": [INFO]}, cache_dir=cache)
    assert got["libfake"]["tag"] == "arm64_ventura"

    with pytest.raises(OSError) as exc:
        deps.choose_bottles({"required_dylibs": [INFO]}, cache_dir=str(tmp_path / "empty"))
    assert "libfake" in str(exc.value)


def test_a_bottle_only_for_an_unknown_macos_is_refused(tmp_path, brew):
    brew.formulae["libfake"] = {"arm64_somewhere_new": {"url": "u", "sha256": "s"},
                                "arm64_linux": {"url": "u", "sha256": "s"}}
    with pytest.raises(ValueError) as exc:
        deps.choose_bottles({"required_dylibs": [INFO]}, cache_dir=str(tmp_path))
    assert "arm64_somewhere_new" in str(exc.value)


# ---------------------------------------------------------------------------
# Reading minimums, and the floor


@needs_clang
def test_every_slice_counts(tmp_path):
    """A universal file's x86_64 slice runs whenever something starts it
    under Rosetta, so the highest of its slices is the file's minimum."""
    arm = _dylib(str(tmp_path / "a" / "lib.dylib"), "12.0", "arm64")
    intel = _dylib(str(tmp_path / "x" / "lib.dylib"), "14.0", "x86_64")
    fat = str(tmp_path / "fat.dylib")
    subprocess.run(["lipo", "-create", arm, intel, "-output", fat], check=True)
    assert macos_floor.slice_minimums(fat) == {"arm64": (12, 0, 0), "x86_64": (14, 0, 0)}
    assert macho_minimum(fat) == (14, 0, 0)
    assert macho_minimum(arm) == (12, 0, 0)
    assert macos_floor.slice_minimums(arm) == {"": (12, 0, 0)}
    # The older LC_VERSION_MIN_MACOSX command, which HP's Qt still carries.
    legacy = _dylib(str(tmp_path / "l" / "lib.dylib"), "10.13", "x86_64")
    assert macho_minimum(legacy) == (10, 13, 0)
    not_macho = tmp_path / "x.txt"
    not_macho.write_text("hello")
    assert macho_minimum(str(not_macho)) is None


@needs_clang
def test_the_floor_is_the_highest_minimum_and_never_below_hps(tmp_path):
    app = _app(str(tmp_path), "12.0")
    lib = os.path.join(app, "Contents", "Resources", "lib")
    _dylib(os.path.join(lib, "old.dylib"), "11.0")
    _dylib(os.path.join(lib, "new.dylib"), "13.0")
    floor, reached_by = exact_floor(app)
    assert floor == (13, 0, 0)
    assert reached_by == [("Contents/Resources/lib/new.dylib", (13, 0, 0))]

    hp_higher = _app(str(tmp_path), "14.0", name="Higher.app")
    _dylib(os.path.join(hp_higher, "Contents", "Resources", "a.dylib"), "13.0")
    assert exact_floor(hp_higher)[0] == (14, 0, 0)


@needs_clang
def test_the_early_floor_counts_what_the_build_keeps_and_the_bottles(tmp_path):
    source = _app(str(tmp_path), "12.0", name="Source.app")
    # Replaced by the build, so not counted before it.
    _dylib(os.path.join(source, "Contents", "Frameworks", "Old.framework", "Old"), "16.0")
    _dylib(os.path.join(source, "Contents", "MacOS", "HPClickExe"), "16.0")
    _dylib(os.path.join(source, "Contents", "Resources", "lib", "hp.dylib"), "13.0")
    bottles = {"libfake": {"tag": "arm64_sonoma", "macos": (14, 0, 0), "url": "", "sha256": ""}}
    plan = plan_floor(source, {"required_dylibs": []}, bottles=bottles)
    assert plan["floor"] == (14, 0, 0)
    assert plan["hp"] == ("Contents/Resources/lib/hp.dylib", (13, 0, 0))
    assert macos_floor.floor_reasons(plan) == ["the support files ClickGraft adds from Homebrew"]


@needs_clang
def test_the_early_floor_says_when_homebrew_could_not_be_asked(tmp_path, brew):
    """Review still gets HP's part of the answer, and is told it is partial."""
    source = _app(str(tmp_path), "12.0", name="Source.app")
    _dylib(os.path.join(source, "Contents", "Resources", "lib", "hp.dylib"), "13.0")
    brew.offline = True
    plan = plan_floor(source, {"required_dylibs": [INFO]}, cache_dir=str(tmp_path / "empty"))
    assert plan["complete"] is False and "libfake" in plan["problem"]
    assert plan["floor"] == (13, 0, 0) and plan["bottles"] == {}
    assert macos_floor.floor_reasons(plan) == ["files HP ships inside HP Click itself"]


@needs_clang
def test_the_png_shim_never_raises_the_floor(tmp_path):
    """clang's default target is the macOS it runs on: 27.0 here, unasked."""
    from clickgraft.build import _compile_pngshim
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "clickgraft", "shims", "pngshim.c")
    out = str(tmp_path / "libclickgraft-pngshim.dylib")
    _compile_pngshim(src, out, "libclickgraft-pngshim.dylib")
    assert macho_minimum(out) == (11, 0, 0)


# ---------------------------------------------------------------------------
# The copy's Info.plist, and verify's check of it


@needs_clang
def test_verify_fails_a_file_newer_than_the_copy_says(tmp_path):
    app = _app(str(tmp_path), "12.0", name="HP Click (Apple Silicon).app")
    _dylib(os.path.join(app, "Contents", "Resources", "lib", "fine.dylib"), "11.0")
    _dylib(os.path.join(app, "Contents", "Resources", "lib", "libidn2.0.dylib"), "14.0")
    with pytest.raises(ValueError) as exc:
        check_minimum_macos(app)
    msg = str(exc.value)
    assert "Contents/Resources/lib/libidn2.0.dylib needs 14.0" in msg
    assert "LSMinimumSystemVersion 12.0" in msg
    assert "fine.dylib" not in msg

    floor, _ = exact_floor(app)
    stamp_minimum(app, floor)
    assert declared_minimum(app) == (14, 0, 0)
    with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
        plist = plistlib.load(f)
    assert plist["LSMinimumSystemVersion"] == "14.0"
    assert plist["CFBundleIdentifier"] == "com.example.test", "stamping lost other keys"
    assert check_minimum_macos(app).startswith("PASSED")


def test_verify_fails_a_copy_that_states_no_minimum(tmp_path):
    app = _app(str(tmp_path), None)
    with pytest.raises(ValueError) as exc:
        check_minimum_macos(app)
    assert "LSMinimumSystemVersion" in str(exc.value)


def test_the_check_runs_with_the_static_checks_before_any_launch():
    """On an Intel Mac verify returns before the smoke launch, so the check
    must sit above that return to run there at all."""
    import inspect
    from clickgraft import verify
    src = inspect.getsource(verify.verify_app_bundle)
    assert src.index('results["minimum_macos"]') < src.index("if not is_apple_silicon()")


# ---------------------------------------------------------------------------
# Refusing before anything is fetched


def test_refuses_a_mac_older_than_the_floor():
    with pytest.raises(MacOSTooOldError) as exc:
        refuse_if_too_old((15, 0, 0), ["files HP ships inside HP Click itself"], "4.10.42",
                          this_mac=(14, 6, 1))
    e = exc.value
    assert (e.needs, e.this_mac) == ("15.0", "14.6.1")
    assert "Nothing has been downloaded or written" in str(e)
    assert "macOS 15.0 or later" in str(e) and "HP Click 4.10.42" in str(e)
    assert isinstance(e, ValueError), "callers that catch ValueError must still see it"

    refuse_if_too_old((15, 0, 0), this_mac=(15, 0, 0))            # equal is fine
    refuse_if_too_old((15, 0, 0), this_mac=(27, 0, 0))
    refuse_if_too_old(None, this_mac=(12, 0, 0))                   # nothing known
    after = pytest.raises(MacOSTooOldError, refuse_if_too_old, (15, 0, 0),
                          this_mac=(14, 0, 0), after_build=True)
    assert "nothing here was replaced" in str(after.value)
    assert "downloaded" not in str(after.value)


def test_an_unreadable_macos_is_let_through(monkeypatch):
    monkeypatch.setattr(macos_floor, "host_macos", lambda: None)
    refuse_if_too_old((15, 0, 0))


def test_the_plan_says_what_the_wizard_needs(monkeypatch):
    plan = {"floor": (15, 0, 0), "declared": (12, 0, 0),
            "hp": ("Contents/Resources/app/appData/macx/lib/libmagic.1.dylib", (15, 0, 0)),
            "bottles": {"libidn2": {"tag": "arm64_ventura", "macos": (13, 0, 0)}},
            "complete": True, "problem": ""}
    monkeypatch.setattr("clickgraft.agent.host_macos", lambda: (14, 7, 0))
    monkeypatch.setattr("clickgraft.agent.host_info", lambda: {"apple_silicon": True})
    got = macos_floor_plan({"app_version": "4.8.117"}, "/nowhere", floor=plan)
    assert got["needs"] == "15.0" and got["this_mac"] == "14.7"
    assert got["this_mac_ok"] is False and got["for_this_mac"] is True
    assert "HP Click 4.8.117" in got["message"]
    assert got["bottles"] == {"libidn2": {"tag": "arm64_ventura", "needs": "13.0"}}
    assert got["hp_declares"] == "12.0" and got["hp_needs"] == "15.0"
    json.dumps(got)

    # On an Intel Mac the copy is for another Mac: nothing to refuse here.
    monkeypatch.setattr("clickgraft.agent.host_info", lambda: {"apple_silicon": False})
    got = macos_floor_plan({"app_version": "4.8.117"}, "/nowhere", floor=plan)
    assert got["this_mac_ok"] is None and got["message"] == ""


def _stock_source():
    """Any stock HP Click with a manifest, or (None, None), found the way the
    rest of the suite finds one (tests/test_clickgraft.py)."""
    from clickgraft.manifest import ManifestManager
    from tests.test_clickgraft import find_stock_bundle
    mm = ManifestManager()
    for version, manifest in sorted(mm.manifests.items()):
        path = find_stock_bundle(version)
        if path:
            return path, manifest
    return None, None


@needs_clang
def test_the_build_refuses_before_downloading_anything(tmp_path, monkeypatch, brew):
    source, manifest = _stock_source()
    if source is None:
        pytest.skip("no stock HP Click with a manifest in /Applications")
    from clickgraft import build
    for d in manifest["required_dylibs"]:
        brew.formulae[d["brew_formula"]] = {"arm64_sequoia": {"url": "u", "sha256": "s"}}

    class Fetched(Exception):
        pass

    def no_fetch(*_a, **_k):
        raise Fetched()

    monkeypatch.setattr(build, "fetch_electron", no_fetch)
    monkeypatch.setattr(hostarch, "is_apple_silicon", lambda: True)
    monkeypatch.setattr(macos_floor, "host_macos", lambda: (14, 0, 0))
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(MacOSTooOldError) as exc:
        build.build_apple_silicon_bundle(source, str(out / "HP Click (Apple Silicon).app"),
                                         manifest=manifest)
    assert exc.value.needs == "15.0" and exc.value.this_mac == "14.0"
    assert os.listdir(out) == []
    assert all("formulae.brew.sh" in c for c in brew.calls), brew.calls

    # Built on an Intel Mac for another one, this Mac's macOS says nothing
    # about the target's: no refusal, and the build goes on to fetch.
    monkeypatch.setattr(hostarch, "is_apple_silicon", lambda: False)
    with pytest.raises(Fetched):
        build.build_apple_silicon_bundle(source, str(out / "HP Click (Apple Silicon).app"),
                                         manifest=manifest, allow_foreign_host=True)
