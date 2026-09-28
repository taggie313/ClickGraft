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
