"""The interpreter ClickGraft fetches, and every way it refuses one.

1.8.0 bundled python.org's Python.framework inside the app. That worked, and it
charged every user 17 MB of every download -- ClickGraft.zip went 770 KB to
18 MB -- for a problem only a Mac without Apple's Command Line Tools has. So the
app ships the *pin* and fetches an interpreter once, on the Macs that need one.

Which moves the whole question to: what stops it installing the wrong thing?
packaging/python-pin.json is a recorded source, so the release tag fixes the
sha256, and nothing is unpacked before the bytes match. These hold that, and they
hold it the only way worth holding it -- by feeding the fetch archives it should
refuse and checking that it does, and that an interpreter already installed is
still there afterwards.

The GUI cannot be driven from a test, so the app has one windowless command,
`--fetch-python`, which runs exactly the code the wizard's Fetch it button runs.
Target: Python 3.9+
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWIFT = os.path.join(ROOT, "packaging", "ClickGraft.swift")
APP = os.path.join(ROOT, "dist", "ClickGraft.app")
EXE = os.path.join(APP, "Contents", "MacOS", "ClickGraft")
SHIPPED_PIN = os.path.join(APP, "Contents", "Resources", "python-pin.json")
REPO_PIN = os.path.join(ROOT, "packaging", "python-pin.json")

needs_app = pytest.mark.skipif(
    not os.path.exists(EXE),
    reason="needs a built app: ./packaging/build_app.sh")


def _swift():
    with open(SWIFT, encoding="utf-8") as f:
        return f.read()


def _swift_code():
    """The Swift with its comment lines removed.

    Asserting a phrase is ABSENT from the source is a trap in this file: the
    comment explaining why a phrase was removed necessarily quotes it, so the
    test matches its own explanation and fails. That has now happened three
    times in this project (the showRequirements ordering test, and twice here),
    so it gets a helper rather than a fourth fix.
    """
    return "\n".join(line for line in _swift().splitlines()
                      if not line.strip().startswith("//"))


def _pin(path=REPO_PIN):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch(url, home, app=EXE, force=True, extra=None):
    """Run the app's windowless fetch. Returns (returncode, output)."""
    env = dict(os.environ,
               CLICKGRAFT_PYTHON_HOME=str(home),
               CLICKGRAFT_NO_SYSTEM_PYTHON="1",
               CLICKGRAFT_PYTHON_PAYLOAD_URL=url)
    args = [app, "--fetch-python"] + (["--force"] if force else [])
    run = subprocess.run(args, env=env, capture_output=True, text=True, timeout=600)
    return run.returncode, (run.stdout + run.stderr)


def _repin(app_dir, sha):
    """A copy of the app whose pin names `sha`, so an archive the real pin would
    reject can be pushed past the hash check and onto the checks behind it."""
    pin_path = os.path.join(app_dir, "ClickGraft.app", "Contents",
                            "Resources", "python-pin.json")
    pin = _pin(pin_path)
    pin["payload_zip_sha256"] = sha
    with open(pin_path, "w", encoding="utf-8") as f:
        json.dump(pin, f, indent=2)
    return os.path.join(app_dir, "ClickGraft.app", "Contents", "MacOS", "ClickGraft")


# --- fixtures ---------------------------------------------------------------

@pytest.fixture(scope="session")
def payload():
    """The archive the pin names -- the built one, not a rebuild.

    It cannot be rebuilt to match: signing writes a fresh signature each time, so
    two runs of `fetch_python.py --payload` produce two different sha256s and the
    pin names exactly one. So this looks for the built archive where --payload
    leaves it, and skips rather than quietly testing a different file. A
    stand-in would be worse than a skip: a synthetic archive could not exercise
    the signature check at all, which is the check with the least cover.
    """
    if sys.platform != "darwin":
        pytest.skip("the payload is signed with a Developer ID, which needs macOS")
    sys.path.insert(0, os.path.join(ROOT, "packaging"))
    import fetch_python
    built = fetch_python.payload_path()
    if not os.path.exists(built):
        pytest.skip(f"no built payload at {built}; "
                    f"run python3 packaging/fetch_python.py --payload")
    if _sha256(built) != _pin()["payload_zip_sha256"]:
        pytest.skip(f"{built} is not the archive the pin names; rebuild and re-pin "
                    f"with python3 packaging/fetch_python.py --payload")
    return built


@pytest.fixture
def app_copy(tmp_path):
    """A throwaway copy of the built app, so a test may edit its pin."""
    if not os.path.exists(APP):
        pytest.skip("needs a built app: ./packaging/build_app.sh")
    dest = tmp_path / "app"
    dest.mkdir()
    shutil.copytree(APP, dest / "ClickGraft.app", symlinks=True)
    return str(dest)


# --- what the app ships -----------------------------------------------------

@needs_app
def test_the_app_ships_the_pin_and_carries_no_interpreter():
    assert os.path.exists(SHIPPED_PIN), "a Mac with no interpreter would have nothing to fetch"
    bundled = os.path.join(APP, "Contents", "Frameworks", "Python.framework")
    assert not os.path.exists(bundled), \
        "built with CLICKGRAFT_BUNDLE_PYTHON=1; that build is for an estate with no internet"


@needs_app
def test_the_pin_in_the_app_is_the_one_in_the_repo():
    """check_payload proves this for a release. Held here too, because every
    other test in this file trusts the shipped copy."""
    assert _sha256(SHIPPED_PIN) == _sha256(REPO_PIN)


def test_the_pin_fetches_over_https():
    """The sha256 is what makes the fetch trustworthy, not the transport -- but
    a pin that fetches over http would hand a bystander the download itself."""
    assert _pin()["payload_url"].startswith("https://")


# --- how the interpreter is chosen -----------------------------------------

@needs_app
def test_a_mac_with_its_own_interpreter_fetches_nothing():
    run = subprocess.run([EXE, "--fetch-python"], capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr
    assert "nothing to fetch" in run.stdout


def test_the_shim_is_never_run_blind_to_find_out():
    """The reason any of this exists: running /usr/bin/python3 with nothing
    behind it IS macOS's offer to install the Command Line Tools -- gigabytes,
    behind an administrator password. So the filesystem is asked first, and the
    guard has to come before the spawn rather than after it."""
    body = re.search(r"static func systemPythonWorks\(\) -> Bool \{(.*?)\n    \}",
                     _swift(), re.S)
    assert body, "systemPythonWorks is gone"
    text = body.group(1)
    guard = text.index("isExecutableFile")
    spawn = text.index("runPython")
    assert guard < spawn, "it spawns the shim before checking anything is behind it"
    assert "return false" in text[:spawn]


def test_the_wizard_asks_before_it_starts_the_backend():
    """showRequirements reports what the backend found, so on a Mac with no
    interpreter it has to offer the fetch first -- and before Toolchain.state,
    which probes by running one."""
    body = re.search(r"@objc func showRequirements\(\) \{(.*?)\n        guard let d = agent\.once",
                     _swift(), re.S)
    assert body, "showRequirements changed shape"
    # Comments stripped first: the comment explaining why needsPython comes
    # before Toolchain.state names Toolchain.state, and the first draft of this
    # test failed on its own explanation.
    text = "\n".join(line for line in body.group(1).splitlines()
                     if not line.strip().startswith("//"))
    assert "Toolchain.needsPython" in text
    assert text.index("Toolchain.needsPython") < text.index("Toolchain.state")


# --- fetching it ------------------------------------------------------------

@needs_app
def test_it_fetches_verifies_and_installs(payload, tmp_path):
    home = tmp_path / "python"
    code, out = _fetch(f"file://{payload}", home)
    assert code == 0, out
    exe = home / _pin()["version"] / "Python.framework/Versions/Current/bin/python3"
    assert exe.exists(), out
    run = subprocess.run([str(exe), "-c", "import sys; print(sys.version.split()[0])"],
                         capture_output=True, text=True,
                         env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert run.stdout.strip() == _pin()["version"], run.stderr


@needs_app
def test_the_fetched_interpreter_runs_the_backend(payload, tmp_path):
    """The whole point. An interpreter that installs but cannot start the
    backend has moved the failure, not fixed it."""
    home = tmp_path / "python"
    assert _fetch(f"file://{payload}", home)[0] == 0
    exe = home / _pin()["version"] / "Python.framework/Versions/Current/bin/python3"
    run = subprocess.run([str(exe), "-m", "clickgraft.cli", "agent", "env"],
                         cwd=ROOT, capture_output=True, text=True, timeout=300,
                         env=dict(os.environ, PYTHONPATH=ROOT, PYTHONDONTWRITEBYTECODE="1"))
    assert run.returncode == 0, run.stderr
    events = [json.loads(l) for l in run.stdout.splitlines() if l.startswith("{")]
    assert any(e.get("type") == "env" for e in events), run.stdout[:400]


@needs_app
def test_a_second_launch_does_not_fetch_again(payload, tmp_path):
    home = tmp_path / "python"
    assert _fetch(f"file://{payload}", home)[0] == 0
    # No --force, and a URL that would fail if it were used.
    code, out = _fetch("file:///nowhere/at/all.zip", home, force=False)
    assert code == 0, out
    assert "nothing to fetch: fetched by ClickGraft" in out


@needs_app
def test_an_install_that_did_not_finish_is_not_trusted(payload, tmp_path):
    """The marker is written last, after the unpack, the signature and the move.
    Without it there is an interpreter on disk that no check ever passed, and
    running that is worse than fetching again."""
    home = tmp_path / "python"
    assert _fetch(f"file://{payload}", home)[0] == 0
    marker = home / _pin()["version"] / ".pinned"
    assert marker.exists()
    marker.unlink()
    code, out = _fetch("file:///nowhere/at/all.zip", home, force=False)
    assert code == 1, "it trusted an interpreter with no marker\n" + out
    assert "nothing to fetch" not in out


# --- and every way it refuses ----------------------------------------------

@needs_app
def test_one_changed_byte_is_refused_and_nothing_is_installed(payload, tmp_path):
    home = tmp_path / "python"
    assert _fetch(f"file://{payload}", home)[0] == 0
    good = (home / _pin()["version"] / ".pinned").read_text()

    tampered = tmp_path / "tampered.zip"
    data = bytearray(open(payload, "rb").read())
    data[len(data) // 2] ^= 0xFF
    tampered.write_bytes(bytes(data))

    code, out = _fetch(f"file://{tampered}", home)
    assert code == 1, out
    assert "isn't what this version of ClickGraft expects" in out
    assert (home / _pin()["version"] / ".pinned").read_text() == good, \
        "a refused download damaged the interpreter that was already there"
    assert not list(home.glob("*.staging")), "it left a half-unpacked copy behind"


@needs_app
def test_an_archive_with_no_interpreter_in_it_is_refused(app_copy, tmp_path):
    """Past the hash, by pinning the wrong archive. Only a release mistake could
    do that -- which is the point: it is refused rather than installed."""
    junk = tmp_path / "junk"
    (junk / "NotPython").mkdir(parents=True)
    (junk / "NotPython" / "x.txt").write_text("hello\n")
    archive = tmp_path / "junk.zip"
    subprocess.run(["/usr/bin/ditto", "-c", "-k", "--keepParent",
                    str(junk / "NotPython"), str(archive)], check=True, capture_output=True)

    app = _repin(app_copy, _sha256(str(archive)))
    code, out = _fetch(f"file://{archive}", tmp_path / "python", app=app)
    assert code == 1, out
    assert "did not contain an interpreter" in out


@needs_app
def test_a_framework_whose_seal_is_broken_is_refused(payload, app_copy, tmp_path):
    """The control for the signature check, and the reason it is not decorative.

    The archive here hashes to exactly what its pin names, so the sha256 passes.
    One file inside the framework has been edited. codesign is what catches it.
    """
    work = tmp_path / "broken"
    work.mkdir()
    subprocess.run(["/usr/bin/ditto", "-x", "-k", payload, str(work)],
                   check=True, capture_output=True)
    victim = work / "Python.framework" / "Versions" / _pin()["version"].rsplit(".", 1)[0] \
        / "lib" / f"python{_pin()['version'].rsplit('.', 1)[0]}" / "os.py"
    assert victim.exists(), victim
    with open(victim, "a", encoding="utf-8") as f:
        f.write("# an addition that changes what the interpreter does\n")
    archive = tmp_path / "broken.zip"
    subprocess.run(["/usr/bin/ditto", "-c", "-k", "--keepParent",
                    str(work / "Python.framework"), str(archive)],
                   check=True, capture_output=True)

    app = _repin(app_copy, _sha256(str(archive)))
    code, out = _fetch(f"file://{archive}", tmp_path / "python", app=app)
    assert code == 1, out
    assert "wouldn't vouch for it" in out


@needs_app
def test_a_download_that_is_not_there_is_refused_in_words():
    code, out = _fetch("https://clickgraft.elusive.net/there-is-no-such-payload.zip",
                       "/tmp/never-used-because-this-fails")
    assert code == 1, out
    assert "didn't finish" in out and "404" in out


# --- and what the release gate does about all of it -------------------------

def _gate():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cr_payload", os.path.join(ROOT, "packaging", "check_release.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_pin_is_a_recorded_source():
    """Or the tag would not fix which interpreter a release fetches."""
    gate = _gate()
    record = gate.source_record(ROOT)
    assert "packaging/python-pin.json" in record
    assert "packaging/fetch_python.py" in record


def test_the_gate_refuses_an_app_that_carries_an_interpreter():
    """CLICKGRAFT_BUNDLE_PYTHON=1 is for an estate with no internet. What the
    site serves has to be the small one."""
    gate = _gate()
    files = {gate.PYTHON_PREFIX + "Versions/3.13/lib/python3.13/os.py": b"x",
             "Resources/python-pin.json": open(REPO_PIN, "rb").read()}
    with pytest.raises(gate.Refused) as e:
        gate.check_python_pin(files, ROOT)
    assert "carries a Python.framework" in str(e.value)


def test_the_gate_refuses_an_app_with_no_pin():
    gate = _gate()
    with pytest.raises(gate.Refused) as e:
        gate.check_python_pin({"Resources/clickgraft/cli.py": b"x"}, ROOT)
    assert "no python-pin.json" in str(e.value)


@pytest.mark.parametrize("drop", ["payload_url", "payload_zip_sha256", "version"])
def test_the_gate_refuses_a_pin_the_app_could_not_act_on(drop):
    """The control. A pin that reaches the fetch screen and fails there leaves a
    Mac with no other way in, so an unusable pin is refused before it ships."""
    gate = _gate()
    pin = _pin()
    del pin[drop]
    with pytest.raises(gate.Refused) as e:
        gate.check_python_pin({"Resources/python-pin.json": json.dumps(pin).encode()}, ROOT)
    assert drop in str(e.value)


def test_the_gate_accepts_the_app_this_repo_builds():
    """And the control for every refusal above: a gate that only says no is not
    a gate."""
    gate = _gate()
    files = {"Resources/python-pin.json": open(REPO_PIN, "rb").read()}
    assert gate.check_python_pin(files, ROOT) == _pin()["version"]


# --- what the adversarial review of a5b2ec8 found -------------------------
# Each of these reproduces a defect that shipped in a5b2ec8 and was fixed in the
# commit that added the test. They are controls first and regression tests
# second: every one of them failed against a5b2ec8.

@needs_app
def test_two_fetches_at_once_still_leave_a_working_interpreter(payload, tmp_path):
    """Two processes fetching at once shared one <version>.staging path.

    Measured against the a5b2ec8 build, 20 staggered double-launches: 10 installed
    cleanly and 10 installed NOTHING, both processes failing over each other's
    half-extracted tree, with ditto's raw paths shown to the user. The same 20
    against the fixed build: 20 clean. A review also reproduced a worse outcome --
    an install marked .pinned holding one of ditto's .BC.* temp files, so codesign
    refused the framework ever after while the marker said it had passed. I could
    not reproduce that specific outcome in 36 further trials, so this test asserts
    the invariant that does reproduce, and checks the seal as well in case the
    rarer one ever lands.

    --fetch-python is documented for deploying to a managed estate, so a script
    run twice, or a script racing the person at the keyboard, is the ordinary
    case rather than the exotic one.
    """
    import threading
    url = f"file://{payload}"
    version = _pin()["version"]
    for attempt, stagger in enumerate((0.0, 0.05, 0.10, 0.15)):
        home = tmp_path / f"python{attempt}"
        home.mkdir()
        results = []

        def go(delay):
            if delay:
                time.sleep(delay)
            results.append(_fetch(url, home))

        threads = [threading.Thread(target=go, args=(d,)) for d in (0, stagger)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=600)
        assert len(results) == 2, "a fetch never returned"
        why = "\n".join(out for _c, out in results)

        exe = home / version / "Python.framework/Versions/Current/bin/python3"
        assert exe.exists(), (
            f"stagger {stagger}s: two fetches at once left no interpreter at all\n{why}")
        seal = subprocess.run(
            ["/usr/bin/codesign", "--verify", "--strict", str(home / version / "Python.framework")],
            capture_output=True, text=True)
        assert seal.returncode == 0, (
            f"stagger {stagger}s: installed a framework codesign refuses:\n{seal.stderr}\n{why}")
        assert (home / version / ".pinned").read_text().strip() == _pin()["payload_zip_sha256"]
        assert not list(home.glob(".staging-*")), f"stagger {stagger}s: staging leaked\n{why}"
        assert not list(home.glob("*.staging")), f"stagger {stagger}s: staging leaked\n{why}"
        # The loser is told what happened, rather than shown ditto's paths.
        codes = sorted(c for c, _o in results)
        if codes != [0, 0]:
            assert "Another copy of ClickGraft is already fetching" in why, why


@needs_app
def test_a_fetch_sweeps_staging_left_by_an_earlier_run(payload, tmp_path):
    """Each leftover is ~49 MB.

    Note what this does and does not claim. A SIGKILL does not run Swift's
    defer, so a killed fetch still leaves its staging directory behind either
    way -- what changed is that it gets swept by the NEXT run. a5b2ec8 removed
    one hard-coded path (<version>.staging) which happened to be the only name
    it ever used; staging names are now unique per run, so without an explicit
    sweep every interrupted fetch would accumulate forever. This is the sweep.
    """
    home = tmp_path / "python"
    home.mkdir()
    stale = home / ".staging-0000dead-beef-0000-0000-000000000000"
    (stale / "Python.framework").mkdir(parents=True)
    (stale / "Python.framework" / "big").write_bytes(b"x" * 1024)
    also = home / ".replaced-0000dead-beef-0000-0000-000000000001"
    also.mkdir()

    assert _fetch(f"file://{payload}", home)[0] == 0
    assert not stale.exists(), "an interrupted run's staging was left to accumulate"
    assert not also.exists(), "a displaced copy was left behind"
    assert not list(home.glob(".staging-*"))
    assert not list(home.glob(".replaced-*"))


def test_the_replace_never_leaves_the_destination_empty():
    """accept() removed the destination and THEN moved staging in, so a move that
    failed after the remove destroyed a working interpreter and left nothing --
    after the replacement had already passed both checks.

    Asserted on the shape of the code rather than by forcing a mid-replace
    failure: the window is between two renames on one filesystem, and every way
    I could find to make the second fail also made the first fail, so a
    behavioural test would have passed against the unfixed code and proved
    nothing (it did -- that is why this is written this way).
    """
    code = _swift_code()
    body = re.search(r"static func accept\(.*?\n    \}", code, re.S)
    assert body, "accept() is gone"
    text = body.group(0)
    assert "removeItem(atPath: dir)" not in text, \
        "the destination is removed before the replacement is in place"
    aside = text.index('moveItem(atPath: dir, toPath: previous)')
    install = text.index('moveItem(atPath: staging, toPath: dir)')
    assert aside < install, "the old copy must be moved aside before the new one moves in"
    # And put back if the install move fails.
    assert "moveItem(atPath: previous, toPath: dir)" in text[install:]


def test_a_full_disk_is_not_reported_as_a_corrupt_download():
    """shell() kept the LAST stderr line. ditto prints the cause and then its own
    summary, so a full disk was reported as "Couldn't read pkzip signature" --
    sending someone whose disk is full to re-download 17 MB, for ever."""
    code = _swift_code()
    body = re.search(r"private static func shell\(.*?\n    \}", code, re.S)
    assert body, "shell() is gone"
    assert "No space left on device" in body.group(0), \
        "shell() does not prefer a line naming a cause"
    assert "lines.first(where:" in body.group(0)
    # And the sentence the user sees is about disk space, not about the file.
    assert "isn't enough room on this Mac" in code


def test_the_fetch_screen_has_a_way_out():
    """The progress screen shipped with `buttons: [UI.spacer()]`, so the only exit
    during a 17 MB download was the close button -- which quits the app mid-ditto,
    the exact kill that stranded 49 MB."""
    code = _swift_code()
    body = re.search(r"@objc func fetchPython\(\) \{(.*?)\n    \}", code, re.S)
    assert body, "fetchPython is gone"
    assert "cancelFetchPython" in body.group(1), "no Cancel on the fetch screen"
    assert "static func cancel()" in code, "PythonPayload cannot be cancelled"


def test_the_app_no_longer_claims_there_is_nothing_else_to_remove():
    """Welcome promised "no uninstaller because there's nothing else to remove",
    and said it BEFORE the fetch screen. After a fetch there is ~49 MB in
    Application Support. The project had already made this exact correction once,
    for the download cache."""
    assert "nothing else to remove" not in _swift_code()
    for path in ("site/index.html", "site/es/index.html"):
        text = open(os.path.join(ROOT, path), encoding="utf-8").read()
        assert "nothing else to remove" not in text, path
        assert "nada más que quitar" not in text, path
    assert "Application Support" in _swift_code()


def test_screen_two_does_not_claim_everything_is_here():
    """The green panel was unscoped, so on an Intel Mac it said "Everything
    ClickGraft needs is here" two lines above an orange panel saying the copy
    will not run on this Mac."""
    code = _swift_code()
    assert "Everything ClickGraft needs is here" not in code
    assert "Nothing to install." in code


def test_the_managed_mac_disclosure_names_no_vanished_requirement():
    """It opened "Installing these tools needs an administrator password" on a
    screen whose body is "Nothing you have to install" -- the same
    no-antecedent defect the same commit fixed 50 lines below."""
    body = re.search(r'Disclosure\(label: "If this is a Mac you don\'t administer"\).*?\n            \}\)',
                     _swift_code(), re.S)
    assert body, "the managed-Mac disclosure is gone"
    text = body.group(0)
    assert "Installing these tools" not in text
    assert "installing developer tools across the estate" not in text
    assert "a Mac of your own that has the tools" not in text


# --- the gate holes the review found --------------------------------------

def test_the_no_interpreter_refusal_runs_for_every_version():
    """It sat behind the same version gate as the pin, so a sub-1.8.0 release
    could carry anything under Frameworks/Python.framework/ -- which check_payload
    skips on purpose -- and no check would look."""
    gate = _gate()
    files = {gate.PYTHON_PREFIX + "Versions/3.13/lib/python3.13/os.py": b"x"}
    with pytest.raises(gate.Refused) as e:
        gate.check_python_pin(files, ROOT, require_pin=False)
    assert "carries a Python.framework" in str(e.value)


@pytest.mark.parametrize("version,expected", [
    ("1.7.0", (1, 7, 0)), ("1.8", (1, 8, 0)), ("1.8.0", (1, 8, 0)),
    ("2.0", (2, 0, 0)), ("1.8.0-rc1", (1, 8, 0)),
])
def test_a_short_version_is_padded_not_ranked_low(version, expected):
    """(1, 8) < (1, 8, 0) is True, so a release numbered "1.8" was treated as
    predating the pin added for it -- while healthcheck.sh's own cutoff read the
    same 1.8 as needing it. Two mechanisms for one boundary, disagreeing."""
    gate = _gate()
    assert gate._version_tuple(version) == expected
    assert gate._predates_python_pin("1.8") is False


def test_the_pin_must_name_the_file_the_deploy_publishes():
    """The app fetches payload_url verbatim; every publisher composes the name
    from `version`. Nothing compared them, so a re-pin that moved the version and
    left the old basename would publish one file and send every app to another."""
    gate = _gate()
    pin = _pin()
    pin["payload_url"] = "https://clickgraft.elusive.net/ClickGraft-python-3.13.8.zip"
    with pytest.raises(gate.Refused) as e:
        gate.check_python_pin({"Resources/python-pin.json": json.dumps(pin).encode()}, ROOT)
    assert "the deploy publishes" in str(e.value)


def test_the_real_pin_names_the_file_the_deploy_publishes():
    """The control for the test above."""
    gate = _gate()
    assert gate.check_python_pin(
        {"Resources/python-pin.json": open(REPO_PIN, "rb").read()}, ROOT) == _pin()["version"]


# --- the fetched interpreter has to be OURS, not merely intact ------------

def _developer_id():
    run = subprocess.run(["security", "find-identity", "-v", "-p", "codesigning"],
                         capture_output=True, text=True)
    for line in run.stdout.splitlines():
        if "Developer ID Application" in line:
            return line.split('"')[1]
    return None


@pytest.fixture
def signed_app(tmp_path):
    """A copy of the built app signed with the Developer ID.

    The build_app.sh output is unsigned, so it has no Team ID and trusted()
    deliberately falls back to a bare seal check -- which means the unsigned
    build cannot exercise the thing these tests are about.
    """
    if not os.path.exists(APP):
        pytest.skip("needs a built app: ./packaging/build_app.sh")
    identity = _developer_id()
    if not identity:
        pytest.skip("no Developer ID Application identity in the keychain")
    dest = tmp_path / "signed"
    dest.mkdir()
    shutil.copytree(APP, dest / "ClickGraft.app", symlinks=True)
    for target in (dest / "ClickGraft.app/Contents/MacOS/ClickGraft", dest / "ClickGraft.app"):
        # --timestamp=none: signing here must not need Apple's timestamp server.
        run = subprocess.run(["/usr/bin/codesign", "--force", "--timestamp=none",
                              "--options", "runtime", "--sign", identity, str(target)],
                             capture_output=True, text=True)
        assert run.returncode == 0, run.stderr
    return str(dest / "ClickGraft.app/Contents/MacOS/ClickGraft")


@needs_app
def test_an_interpreter_signed_by_someone_else_is_refused(signed_app, payload, tmp_path):
    """The marker decides nothing an attacker could not decide too.

    It is a text file in a directory the user can write, and the value it holds
    is public -- it ships in the app and is published on the site. A review
    planted a framework with a copied marker and the app accepted it, and
    pointed out the reach: `installed` is consulted BEFORE /usr/bin/python3, so
    a Mac with working developer tools that never needed a fetch would prefer
    the plant.

    The plant here is ad-hoc signed, which a bare `codesign --verify --strict`
    ACCEPTS -- that is the whole reason the check names ClickGraft's own Team ID
    rather than only checking the seal.
    """
    home = tmp_path / "python"
    assert _fetch(f"file://{payload}", home, app=signed_app)[0] == 0
    version = _pin()["version"]
    framework = home / version / "Python.framework"

    # Re-sign the real framework ad-hoc: same files, different signer.
    subprocess.run(["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(framework)],
                   capture_output=True, check=True)
    bare = subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(framework)],
                          capture_output=True, text=True)
    assert bare.returncode == 0, (
        "the plant must be one a bare seal check accepts, or this proves nothing")
    assert (home / version / ".pinned").exists(), "the marker is still in place"

    # Offered the plant and a download that cannot succeed: it must go looking
    # for a real one rather than run what is there.
    code, out = _fetch("file:///nowhere-at-all.zip", home, app=signed_app, force=False)
    assert code == 1, "the app ran an interpreter signed by someone else\n" + out
    assert "nothing to fetch" not in out, out


@needs_app
def test_the_real_interpreter_is_accepted_by_a_signed_app(signed_app, payload, tmp_path):
    """The control. A check that refuses everything is not a check -- and an
    earlier draft of this did exactly that: codesign -R reads its argument as a
    PATH unless the text starts with '=', so every framework failed with
    'invalid requirement specification', including ClickGraft's own.
    """
    home = tmp_path / "python"
    assert _fetch(f"file://{payload}", home, app=signed_app)[0] == 0
    code, out = _fetch("file:///nowhere-at-all.zip", home, app=signed_app, force=False)
    assert code == 0, out
    assert "nothing to fetch: fetched by ClickGraft" in out, out


def test_the_requirement_is_a_requirement_and_not_a_path():
    """Guards the '=' whose absence made the check refuse everything."""
    code = _swift_code()
    body = re.search(r"static func trusted\(.*?\n    \}", code, re.S)
    assert body, "trusted() is gone"
    assert '"=anchor apple generic' in body.group(0), \
        "codesign -R needs a leading = or it reads the text as a filename"
    assert "subject.OU" in body.group(0)
