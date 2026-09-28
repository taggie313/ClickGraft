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

import fcntl
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
REPO_PIN = os.path.join(ROOT, "packaging", "python-pin.json")

# The candidate is an input, not a constant. It arrives through the
# clickgraft_app / clickgraft_exe / shipped_pin fixtures in conftest.py, so the
# release gate can build a fresh app and hand this suite its path. Freezing
# dist/ here is what let the gate test last release's executable while the
# structural tests read this release's source (F3).


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


def _fetch(url, home, app, force=True, extra=None):
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
    reject can be pushed past the hash check and onto the checks behind it.

    Re-sealed afterwards. python-pin.json is a sealed resource, so editing it
    inside a SIGNED app makes the kernel SIGKILL the process on exec -- which is
    macOS working correctly, and shows up here as a bare `assert -9 == 1`. It
    only appears once dist/ has been through sign_and_notarize.sh, so it stayed
    hidden through every run against an unsigned build and surfaced during the
    1.8.0 release. Ad-hoc is right for the copy: teamID is then nil and
    trusted() takes its documented development-build path.
    """
    app = os.path.join(app_dir, "ClickGraft.app")
    pin_path = os.path.join(app, "Contents", "Resources", "python-pin.json")
    pin = _pin(pin_path)
    pin["payload_zip_sha256"] = sha
    with open(pin_path, "w", encoding="utf-8") as f:
        json.dump(pin, f, indent=2)
    subprocess.run(["/usr/bin/codesign", "--force", "--deep", "--sign", "-", app],
                   capture_output=True, check=True)
    return os.path.join(app, "Contents", "MacOS", "ClickGraft")


# --- fixtures ---------------------------------------------------------------

# The `payload` fixture lives in conftest.py now, with the candidate fixtures.
# A module-level copy shadowed it, so --clickgraft-payload was silently ignored
# and a deliberately wrong archive still passed a release-mode run.


@pytest.fixture
def app_copy(tmp_path, clickgraft_app):
    """A throwaway copy of the built app, so a test may edit its pin."""
    dest = tmp_path / "app"
    dest.mkdir()
    shutil.copytree(clickgraft_app, dest / "ClickGraft.app", symlinks=True)
    return str(dest)


# --- what the app ships -----------------------------------------------------

def test_the_app_ships_the_pin_and_carries_no_interpreter(clickgraft_app, shipped_pin):
    assert os.path.exists(shipped_pin), "a Mac with no interpreter would have nothing to fetch"
    bundled = os.path.join(clickgraft_app, "Contents", "Frameworks", "Python.framework")
    assert not os.path.exists(bundled), \
        "built with CLICKGRAFT_BUNDLE_PYTHON=1; that build is for an estate with no internet"


def test_the_pin_in_the_app_is_the_one_in_the_repo(shipped_pin):
    """check_payload proves this for a release. Held here too, because every
    other test in this file trusts the shipped copy."""
    assert _sha256(shipped_pin) == _sha256(REPO_PIN)


def test_the_pin_fetches_over_https():
    """The sha256 is what makes the fetch trustworthy, not the transport -- but
    a pin that fetches over http would hand a bystander the download itself."""
    assert _pin()["payload_url"].startswith("https://")


# --- how the interpreter is chosen -----------------------------------------

def test_a_mac_with_its_own_interpreter_fetches_nothing(tmp_path, exe):
    """CLICKGRAFT_PYTHON_HOME is set even though this should never reach it.

    Without it the test passes on a developer's Mac only because
    /usr/bin/python3 works there; on a Mac with no developer tools -- the ones
    this feature exists for -- it would drive a real 17 MB download from the live
    payload_url into the real ~/Library/Application Support/ClickGraft. A test
    must not write outside tmp_path on any machine that runs it.
    """
    run = subprocess.run(
        [exe, "--fetch-python"], capture_output=True, text=True, timeout=120,
        env=dict(os.environ,
                 CLICKGRAFT_PYTHON_HOME=str(tmp_path / "never-used"),
                 CLICKGRAFT_PYTHON_PAYLOAD_URL="file:///nowhere-at-all.zip"))
    assert run.returncode == 0, run.stderr
    assert "nothing to fetch" in run.stdout
    assert not (tmp_path / "never-used").exists(), "it fetched when it should not have"


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
    # The guard has to BAIL, not merely appear. systemPythonWorks opens with an
    # unrelated `return false` for the test override, which satisfied a plain
    # `"return false" in text[:spawn]` however the guard was written -- so
    # turning it into a no-op `if` left this passing.
    assert re.search(r"guard dirs\.contains\(where:.*?\)\s*\n\s*else \{ return false \}",
                     text, re.S), "the filesystem guard does not bail before the spawn"


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
    # The RETURN is the point, not the order. Deleting it left the assertion
    # satisfied -- and the fall-through reaches Toolchain.state, which runs
    # /usr/bin/python3 and raises Apple's install prompt: the one harm this
    # whole feature exists to avoid.
    assert re.search(r"if Toolchain\.needsPython \{\s*\n\s*showNeedPython\(\)\s*\n\s*return\s*\n\s*\}",
                     text), "the fetch offer does not stop the screen from continuing"
    assert text.index("Toolchain.needsPython") < text.index("Toolchain.state")


# --- fetching it ------------------------------------------------------------

def test_it_fetches_verifies_and_installs(payload, tmp_path, exe):
    home = tmp_path / "python"
    code, out = _fetch(f"file://{payload}", home, exe)
    assert code == 0, out
    installed = home / _pin()["version"] / "Python.framework/Versions/Current/bin/python3"
    assert installed.exists(), out
    run = subprocess.run([str(installed), "-c", "import sys; print(sys.version.split()[0])"],
                         capture_output=True, text=True,
                         env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert run.stdout.strip() == _pin()["version"], run.stderr


def test_the_fetched_interpreter_runs_the_backend(payload, tmp_path, exe):
    """The whole point. An interpreter that installs but cannot start the
    backend has moved the failure, not fixed it."""
    home = tmp_path / "python"
    assert _fetch(f"file://{payload}", home, exe)[0] == 0
    installed = home / _pin()["version"] / "Python.framework/Versions/Current/bin/python3"
    run = subprocess.run([str(installed), "-m", "clickgraft.cli", "agent", "env"],
                         cwd=ROOT, capture_output=True, text=True, timeout=300,
                         env=dict(os.environ, PYTHONPATH=ROOT, PYTHONDONTWRITEBYTECODE="1"))
    assert run.returncode == 0, run.stderr
    events = [json.loads(l) for l in run.stdout.splitlines() if l.startswith("{")]
    assert any(e.get("type") == "env" for e in events), run.stdout[:400]


def test_a_second_launch_does_not_fetch_again(payload, tmp_path, exe):
    home = tmp_path / "python"
    assert _fetch(f"file://{payload}", home, exe)[0] == 0
    # No --force, and a URL that would fail if it were used.
    code, out = _fetch("file:///nowhere/at/all.zip", home, exe, force=False)
    assert code == 0, out
    assert "nothing to fetch: fetched by ClickGraft" in out


def test_an_install_that_did_not_finish_is_not_trusted(payload, tmp_path, exe):
    """The marker is written last, after the unpack, the signature and the move.
    Without it there is an interpreter on disk that no check ever passed, and
    running that is worse than fetching again."""
    home = tmp_path / "python"
    assert _fetch(f"file://{payload}", home, exe)[0] == 0
    marker = home / _pin()["version"] / ".pinned"
    assert marker.exists()
    marker.unlink()
    code, out = _fetch("file:///nowhere/at/all.zip", home, exe, force=False)
    assert code == 1, "it trusted an interpreter with no marker\n" + out
    assert "nothing to fetch" not in out


# --- and every way it refuses ----------------------------------------------

def test_one_changed_byte_is_refused_and_nothing_is_installed(payload, tmp_path, exe):
    home = tmp_path / "python"
    assert _fetch(f"file://{payload}", home, exe)[0] == 0
    good = (home / _pin()["version"] / ".pinned").read_text()

    tampered = tmp_path / "tampered.zip"
    data = bytearray(open(payload, "rb").read())
    data[len(data) // 2] ^= 0xFF
    tampered.write_bytes(bytes(data))

    code, out = _fetch(f"file://{tampered}", home, exe)
    assert code == 1, out
    assert "isn't what this version of ClickGraft expects" in out
    assert (home / _pin()["version"] / ".pinned").read_text() == good, \
        "a refused download damaged the interpreter that was already there"
    # .staging-<uuid>, not *.staging: pathlib's * does not match a leading dot,
    # so the old pattern could never match anything this code creates.
    assert not list(home.glob(".staging-*")), "it left a half-unpacked copy behind"


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
    code, out = _fetch(f"file://{archive}", tmp_path / "python", app)
    assert code == 1, out
    assert "did not contain an interpreter" in out


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
    code, out = _fetch(f"file://{archive}", tmp_path / "python", app)
    assert code == 1, out
    assert "wouldn't vouch for it" in out


def test_a_download_that_is_not_there_is_refused_in_words(exe, tmp_path):
    code, out = _fetch("https://clickgraft.elusive.net/there-is-no-such-payload.zip",
                       tmp_path / "never-used-because-this-fails", exe)
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

def test_two_fetches_at_once_still_leave_a_working_interpreter(payload, tmp_path, exe):
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
            results.append(_fetch(url, home, exe))

        threads = [threading.Thread(target=go, args=(d,)) for d in (0, stagger)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=600)
        assert len(results) == 2, "a fetch never returned"
        why = "\n".join(out for _c, out in results)

        installed = home / version / "Python.framework/Versions/Current/bin/python3"
        assert installed.exists(), (
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


def test_a_fetch_sweeps_staging_left_by_an_earlier_run(payload, tmp_path, exe):
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

    assert _fetch(f"file://{payload}", home, exe)[0] == 0
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
    # Per-run staging. a5b2ec8 used one shared `dir + ".staging"` for every
    # process; an audit restored that name and the whole file stayed green, so
    # nothing covered the uniqueness the concurrency test's docstring is about.
    assert 'UUID().uuidString' in text, "staging is shared between processes again"
    assert 'dir + ".staging"' not in text
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
    predating the pin added for it.

    Pointed at site/deploy/publish_site.py, which is where that cutoff actually
    lives. An earlier version of this test pinned check_release.py's copy, which
    had already stopped being called -- the gate asks the tag instead -- so the
    test was holding dead code upright while the two live copies of the boundary
    had none.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ps_version", os.path.join(ROOT, "site", "deploy", "publish_site.py"))
    publish = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publish)
    assert publish._version_tuple(version) == expected
    assert publish._version_tuple("1.8") >= publish.PYTHON_PIN_FROM


def test_the_gate_decides_by_the_tag_and_not_by_the_version_number():
    """A 1.7.1 cut from a tree that has a pin ships an app that fetches. Keying
    on "is this below 1.8.0" would skip the only check that makes that safe."""
    gate = _gate()
    assert not hasattr(gate, "_predates_python_pin"), \
        "the version cutoff is back; check_artifact should ask the tag"
    source = open(os.path.join(ROOT, "packaging", "check_release.py"), encoding="utf-8").read()
    body = re.search(r"def check_artifact\(.*?\n\ndef ", source, re.S)
    assert body, "check_artifact is gone"
    assert "'packaging/python-pin.json' in expected" in body.group(0)


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

@pytest.fixture
def signed_app(tmp_path, clickgraft_app, developer_id):
    """A copy of the built app signed with the Developer ID.

    The build_app.sh output is unsigned, so it has no Team ID and trusted()
    deliberately falls back to a bare seal check -- which means the unsigned
    build cannot exercise the thing these tests are about.
    """
    identity = developer_id
    dest = tmp_path / "signed"
    dest.mkdir()
    shutil.copytree(clickgraft_app, dest / "ClickGraft.app", symlinks=True)
    for target in (dest / "ClickGraft.app/Contents/MacOS/ClickGraft", dest / "ClickGraft.app"):
        # --timestamp=none: signing here must not need Apple's timestamp server.
        run = subprocess.run(["/usr/bin/codesign", "--force", "--timestamp=none",
                              "--options", "runtime", "--sign", identity, str(target)],
                             capture_output=True, text=True)
        assert run.returncode == 0, run.stderr
    return str(dest / "ClickGraft.app/Contents/MacOS/ClickGraft")


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
    assert _fetch(f"file://{payload}", home, signed_app)[0] == 0
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
    code, out = _fetch("file:///nowhere-at-all.zip", home, signed_app, force=False)
    assert code == 1, "the app ran an interpreter signed by someone else\n" + out
    assert "nothing to fetch" not in out, out


def test_the_real_interpreter_is_accepted_by_a_signed_app(signed_app, payload, tmp_path):
    """The control. A check that refuses everything is not a check -- and an
    earlier draft of this did exactly that: codesign -R reads its argument as a
    PATH unless the text starts with '=', so every framework failed with
    'invalid requirement specification', including ClickGraft's own.
    """
    home = tmp_path / "python"
    assert _fetch(f"file://{payload}", home, signed_app)[0] == 0
    code, out = _fetch("file:///nowhere-at-all.zip", home, signed_app, force=False)
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


# --- what the review of the FIXES found ----------------------------------

def test_the_install_lock_is_flock_and_never_removes_its_file():
    """The first lock was O_EXCL plus a pid, with rules for taking over a stale
    one. A verifier compiled that class verbatim and ran 400 barrier-synchronised
    double-launches against a lock naming a dead pid: 16 had BOTH processes
    believe they held it, because the takeover was removeItem-then-create and the
    second process deleted the lock the first had just made.

    flock has no takeover path at all -- the kernel releases it when the fd
    closes or the process dies -- so the fix is the absence of that machinery,
    and this checks the machinery has not crept back.
    """
    code = _swift_code()
    body = re.search(r"final class Lock \{.*?\n    \}", code, re.S)
    assert body, "Lock is gone"
    text = body.group(0)

    # The RESULT of flock has to decide whether the lock is held. An audit built
    # `_ = flock(fd, LOCK_EX | LOCK_NB); if true { taken = true }` -- a lock that
    # excludes nobody -- and the first version of this test passed, because it
    # only asked whether the words appeared.
    assert re.search(r"if flock\(fd, LOCK_EX \| LOCK_NB\) == 0 \{\s*\n\s*taken = true",
                     text), "taken is not set from flock's return value"
    assert text.count("taken = true") == 1, \
        "taken is set somewhere other than the flock branch"

    # And the machinery that made the O_EXCL version racy stays gone.
    assert "removeItem" not in text, "removing the lock file reintroduces the race"
    assert "kill(" not in text, "pid liveness is a takeover heuristic flock does not need"
    assert "getpid()" not in text


def test_a_second_installer_is_told_to_wait(payload, tmp_path, exe):
    """Deterministic, because the racing version is not.

    An audit built a lock that excludes nobody -- flock's return value discarded,
    so every process believes it holds it -- and the racing test below passed,
    because two clean installs in sequence satisfy every assertion it makes. This
    one takes the lock from Python first, so the app MUST be refused.
    """
    home = tmp_path / "python"
    home.mkdir()
    fd = os.open(str(home / ".installing"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, out = _fetch(f"file://{payload}", home, exe)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert code == 1, "the app installed while another process held the lock\n" + out
    assert "Another copy of ClickGraft is already fetching" in out, out
    assert not (home / _pin()["version"]).exists(), "it installed anyway"
    assert not list(home.glob(".staging-*")), "it unpacked anyway"

    # The control: with the lock released, the same fetch succeeds.
    code, out = _fetch(f"file://{payload}", home, exe)
    assert code == 0, out


def test_two_processes_cannot_both_hold_the_install_lock(payload, tmp_path, exe):
    """A smoke test, not the guarantee -- and the distinction is the point.

    The old takeover raced 16 times in 400 barrier-synchronised trials, which a
    verifier measured by compiling the Lock class into a standalone binary. At
    ~4%, two app launches reproduce it about one run in twelve, so this test
    passes against the BROKEN lock most of the time and would be false comfort
    if it were the only check. test_the_install_lock_is_flock_and_never_removes
    _its_file is the one that binds; this exercises the same path end to end and
    catches a lock that is broken outright.

    The lock file is planted first because both processes racing an EXISTING
    lock is the case the old takeover got wrong.
    """
    import threading
    home = tmp_path / "python"
    home.mkdir()
    # A lock left by something that is gone: what the old code tried to take over.
    (home / ".installing").write_bytes(b"99998\n")

    results = []
    lock = threading.Lock()

    def go():
        r = _fetch(f"file://{payload}", home, exe)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=600)

    assert len(results) == 2
    why = "\n".join(out for _c, out in results)
    version = _pin()["version"]
    assert (home / version / ".pinned").exists(), f"neither fetch installed anything\n{why}"
    seal = subprocess.run(
        ["/usr/bin/codesign", "--verify", "--strict",
         str(home / version / "Python.framework")], capture_output=True, text=True)
    assert seal.returncode == 0, f"installed a framework codesign refuses\n{seal.stderr}\n{why}"
    assert not list(home.glob(".staging-*")), f"staging leaked\n{why}"


def test_an_unwritable_home_does_not_say_another_copy_is_fetching(payload, tmp_path, exe):
    """Telling someone whose home is unwritable to wait for another copy is
    advice that can never come true, and every retry repeats it. The old lock
    could not tell "busy" from "could not open the lock at all"."""
    home = tmp_path / "python"
    home.mkdir()
    home.chmod(0o500)
    try:
        code, out = _fetch(f"file://{payload}", home, exe)
    finally:
        home.chmod(0o700)
    assert code == 1, out
    assert "Another copy of ClickGraft is already fetching" not in out, out
    # The app's own sentence, not any downstream permission error: strerror puts
    # "Permission denied" into this same line, so the `or` arm that used to be
    # here was satisfied by ditto failing for unrelated reasons.
    assert "can't write to" in out, out


def test_cancel_stops_the_install_and_not_only_the_download():
    """cancel() used to touch the download task alone. accept() runs
    synchronously in the delegate callback and consulted nothing, so Cancel
    during "Unpacking" still installed 49 MB and wrote .pinned for a fetch the
    person had backed out of -- while the button's comment said otherwise."""
    code = _swift_code()
    body = re.search(r"static func accept\(.*?\n    \}", code, re.S)
    assert body, "accept() is gone"
    assert body.group(0).count("isCancelling") >= 2, \
        "accept() does not give up when the fetch was cancelled"
    cancel = re.search(r"static func cancel\(\) \{.*?\n    \}", code, re.S)
    assert cancel and "setCancelling(true)" in cancel.group(0)


def test_the_fetchers_settled_flag_is_not_shared_across_queues_unguarded():
    """It is written by cancel() on the main queue and by the delegate callbacks
    on URLSession's queue. A cancelled fetch whose done() still fired would jump
    the wizard to Requirements after the person backed out."""
    code = _swift_code()
    body = re.search(r"private final class Fetcher.*?\n    \}\n\}", code, re.S)
    assert body, "Fetcher is gone"
    text = body.group(0)
    # The LOCK inside claimSettle is what makes it a single claim. Deleting it
    # left the old assertion passing, because `gate` is still declared NSLock
    # and the accessors still use it.
    assert re.search(r"private func claimSettle\(\) -> Bool \{\s*\n\s*gate\.lock\(\);"
                     r" defer \{ gate\.unlock\(\) \}", text), \
        "claimSettle does not claim under the lock"
    assert "guard claimSettle() else { return }" in text, \
        "settle() does not go through the atomic claim"


def test_robots_keeps_crawlers_off_the_download_people_actually_get():
    """The page links the VERSIONED name, and summary.sh counts 200s on it, so a
    rule covering only /ClickGraft.zip left the real download crawlable -- and
    crawler traffic in the download count is the harm robots.txt was written for.
    """
    rules = [line.split(":", 1)[1].strip()
             for line in open(os.path.join(ROOT, "site/robots.txt"), encoding="utf-8")
             if line.startswith("Disallow:")]

    def blocked(path):
        return any(path.startswith(r) for r in rules)

    assert blocked("/ClickGraft-1.7.0.zip")
    assert blocked("/ClickGraft-1.7.0.zip.sha256")
    assert blocked("/ClickGraft.zip")
    assert blocked(f"/ClickGraft-python-{_pin()['version']}.zip")
    assert blocked("/python-pin.json")
    # And the page itself must stay indexable: that is how people find the tool.
    assert not blocked("/")
    assert not blocked("/es/")


def test_the_deploy_looks_for_the_payload_of_the_version_it_is_publishing():
    """redeploy.sh reads the pin out of the ZIP, then asked fetch_python for the
    WORKING TREE's payload path -- the wrong question in exactly the case the
    ZIP-pin fix exists for, sending it to the network for an archive it had."""
    deploy = open(os.path.join(ROOT, "site/deploy/redeploy.sh"), encoding="utf-8").read()
    assert '--payload-path "$PY_VERSION"' in deploy

    sys.path.insert(0, os.path.join(ROOT, "packaging"))
    import fetch_python
    assert "3.13.8" in fetch_python.payload_path("3.13.8")
    assert fetch_python.payload_path() == fetch_python.payload_path(_pin()["version"])


def test_running_the_interpreter_to_check_it_would_break_its_own_seal(payload, tmp_path, exe):
    """The mechanism behind the re-fetch loop 1.8.0 shipped.

    Toolchain.probe() runs the interpreter with `-c ""` to see whether it works.
    That imports `encodings`, and an interpreter allowed to write .pyc puts them
    INSIDE the framework, which breaks its code signature -- so trusted() then
    refuses the interpreter ClickGraft installed seconds earlier and every launch
    fetches 17 MB again, silently.

    Caught by driving the released build against the live site: the app fetched
    an interpreter, showed Requirements (which calls probe()), and asking the
    same app again answered "fetching".

    Note what this does NOT do: --fetch-python never calls probe(), so it cannot
    reproduce the loop end to end -- an earlier version of this test claimed to
    and passed against the unfixed build. What binds the call sites is
    test_every_way_the_app_runs_python_refuses_to_write_bytecode; this holds the
    fact underneath it, in both directions.
    """
    home = tmp_path / "python"
    assert _fetch(f"file://{payload}", home, exe)[0] == 0
    version = _pin()["version"]
    framework = home / version / "Python.framework"
    installed = framework / "Versions/Current/bin/python3"

    def sealed():
        return subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(framework)],
                              capture_output=True).returncode == 0

    assert sealed(), "the freshly installed framework is already unsealed"

    # With the variable: what ClickGraft must do.
    subprocess.run([str(installed), "-c", ""], capture_output=True,
                   env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert sealed(), "even with PYTHONDONTWRITEBYTECODE the framework was dirtied"
    assert (home / version / ".pinned").exists()

    # The control, so the assertion above is known to be load-bearing: without
    # it, the same command breaks the seal.
    env = dict(os.environ)
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    subprocess.run([str(installed), "-c", ""], capture_output=True, env=env)
    assert not sealed(), (
        "running the interpreter without PYTHONDONTWRITEBYTECODE no longer dirties "
        "it -- if the payload now ships a complete __pycache__, this test and the "
        "reason for the fix should both be revisited")


def test_every_way_the_app_runs_python_refuses_to_write_bytecode():
    """Agent.process had it; runPython did not, and runPython runs first."""
    code = _swift_code()
    for func in (r"private static func runPython\(.*?\n    \}",
                 r"private func process\(_ args: \[String\]\) -> Process \{.*?\n    \}"):
        body = re.search(func, code, re.S)
        assert body, func
        assert 'PYTHONDONTWRITEBYTECODE' in body.group(0), \
            f"this path can dirty a signed framework: {func}"
