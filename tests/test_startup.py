"""G4: two complete startups, one fetch, and a runtime that stays sealed.

`--fetch-python` installs a runtime and stops. It never calls probe() and never
starts the backend, which is why 1.8.0's re-fetch loop survived the whole suite
and was found only by driving the released app against the live site. So the
wizard's actual startup sequence -- resolve, probe, ask the backend -- is now a
callable path, and `--check-startup` runs exactly it.

Reuse is proved, not inferred: the second startup runs with the download
endpoint pointed somewhere unreachable, so a fetch would fail loudly rather
than silently succeed and look like reuse.
"""

import json
import os
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UNREACHABLE = "https://127.0.0.1:1/there-is-no-runtime-here.zip"


def _startup(exe, home, url=UNREACHABLE, no_system=True):
    env = dict(os.environ, CLICKGRAFT_PYTHON_HOME=str(home),
               CLICKGRAFT_PYTHON_PAYLOAD_URL=url)
    if no_system:
        env["CLICKGRAFT_NO_SYSTEM_PYTHON"] = "1"
    else:
        env.pop("CLICKGRAFT_NO_SYSTEM_PYTHON", None)
    run = subprocess.run([exe, "--check-startup"], env=env,
                         capture_output=True, text=True, timeout=600)
    try:
        return run.returncode, json.loads(run.stdout.strip() or "{}")
    except ValueError:
        pytest.fail(f"--check-startup did not print JSON:\n{run.stdout}\n{run.stderr}")


def _fetch(exe, home, payload):
    return subprocess.run(
        [exe, "--fetch-python"],
        env=dict(os.environ, CLICKGRAFT_PYTHON_HOME=str(home),
                 CLICKGRAFT_NO_SYSTEM_PYTHON="1",
                 CLICKGRAFT_PYTHON_PAYLOAD_URL=f"file://{payload}"),
        capture_output=True, text=True, timeout=600)


def _sealed(home, version):
    framework = home / version / "Python.framework"
    return subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(framework)],
                          capture_output=True).returncode == 0


def test_startup_without_a_runtime_asks_for_one_and_fetches_nothing(exe, tmp_path):
    """It must not silently consent to a 17 MB download. `needs-runtime` is a
    state to report, not a problem to solve behind the caller's back."""
    home = tmp_path / "python"
    home.mkdir()
    code, out = _startup(exe, home, url="file:///nowhere-at-all.zip")
    assert code == 2, out
    assert out["outcome"] == "needs-runtime", out
    assert not any(home.iterdir()), f"it fetched something anyway: {list(home.iterdir())}"


def test_g4_two_startups_one_fetch_and_the_seal_survives(exe, payload, tmp_path):
    """G4. Two separate processes against one isolated cache.

    The download endpoint is unreachable for both startups, so reuse is proved
    rather than assumed: a second fetch could not succeed quietly.
    """
    from tests.conftest import _pin
    version = _pin()["version"]
    home = tmp_path / "python"
    home.mkdir()

    assert _fetch(exe, home, payload).returncode == 0
    assert _sealed(home, version), "the freshly installed runtime is already unsealed"
    marker = (home / version / ".pinned").read_text()

    for attempt in (1, 2):
        code, out = _startup(exe, home)
        assert code == 0, f"startup {attempt} failed: {out}"
        assert out["outcome"] == "ok", out
        assert out["source"] == "fetched by ClickGraft", out
        assert _sealed(home, version), (
            f"startup {attempt} broke the runtime's signature -- the next launch "
            f"would refuse it and fetch again")
        assert (home / version / ".pinned").read_text() == marker, \
            f"startup {attempt} rewrote the marker"


def test_startup_uses_the_same_invocation_as_the_wizard():
    """A diagnostic that built its own python invocation would stop reflecting
    the one that matters the moment they diverged -- and bytecode suppression
    is exactly where they diverged once already."""
    with open(os.path.join(ROOT, "packaging", "ClickGraft.swift"), encoding="utf-8") as h:
        code = "\n".join(l for l in h.read().splitlines() if not l.strip().startswith("//"))
    import re
    body = re.search(r"enum Startup \{.*?\n\}", code, re.S)
    assert body, "Startup is gone"
    assert "Agent(resources: resources).once" in body.group(0), \
        "check() spawns its own process instead of going through Agent"
    # And Agent is what carries the environment.
    process = re.search(r"private func process\(_ args: \[String\]\) -> Process\??\s*\{.*?\n    \}",
                        code, re.S)
    assert process and "PYTHONDONTWRITEBYTECODE" in process.group(0)


def test_startup_reports_the_system_interpreter_when_there_is_one(exe, tmp_path):
    """M2's shape: a Mac with working developer tools starts without fetching."""
    home = tmp_path / "python"
    home.mkdir()
    code, out = _startup(exe, home, no_system=False)
    assert code == 0, out
    assert out["outcome"] == "ok", out
    assert not any(home.iterdir()), "it fetched a runtime it did not need"


# --- G6: evidence belongs to one archive -----------------------------------

def _gate():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cr_startup", os.path.join(ROOT, "packaging", "check_release.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_g6_evidence_is_rejected_for_a_different_zip(tmp_path):
    """G6. A version string is not enough: two builds of 1.8.2 are both
    "1.8.2" and only one of them was ever started."""
    gate = _gate()
    import hashlib
    archive = tmp_path / "ClickGraft.zip"
    archive.write_bytes(b"the archive that was verified")
    good = {"zip_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "cases": [{"id": "B1", "result": "pass"}]}
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps(good))
    assert gate.check_evidence(archive, evidence, say=lambda _l: None)

    archive.write_bytes(b"a different archive entirely")
    with pytest.raises(gate.Refused, match="different ZIP"):
        gate.check_evidence(archive, evidence, say=lambda _l: None)


def test_evidence_recording_a_failure_is_not_approval(tmp_path):
    gate = _gate()
    import hashlib
    archive = tmp_path / "ClickGraft.zip"
    archive.write_bytes(b"x")
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps(
        {"zip_sha256": hashlib.sha256(b"x").hexdigest(),
         "cases": [{"id": "B1", "result": "pass"}, {"id": "B3", "result": "fail"}]}))
    with pytest.raises(gate.Refused, match="did not pass"):
        gate.check_evidence(archive, evidence, say=lambda _l: None)


def test_evidence_with_no_cases_is_not_approval(tmp_path):
    """An empty run must not read as a clean one."""
    gate = _gate()
    import hashlib
    archive = tmp_path / "ClickGraft.zip"
    archive.write_bytes(b"x")
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps(
        {"zip_sha256": hashlib.sha256(b"x").hexdigest(), "cases": []}))
    with pytest.raises(gate.Refused, match="did not pass"):
        gate.check_evidence(archive, evidence, say=lambda _l: None)


def test_the_bootstrap_gate_runs_after_the_artifact_checks_not_instead():
    """Starting is not the same as being the app its tag builds, and a gate
    that swapped one for the other would approve an unrelated binary."""
    with open(os.path.join(ROOT, "packaging", "check_release.py"), encoding="utf-8") as h:
        src = h.read()
    import re
    body = re.search(r"elif args\.artifact:.*?ready to distribute", src, re.S)
    assert body, "the artifact branch changed shape"
    text = body.group(0)
    assert text.index("check_artifact(") < text.index("bootstrap_gate("), \
        "the bootstrap runs before the artifact is shown to match its tag"


def _startup_clean(exe, home, nodev, url=UNREACHABLE):
    """--check-startup on the REAL clean-Mac condition, with no test hook.

    `CLICKGRAFT_NO_SYSTEM_PYTHON` returns false at the top of
    systemPythonWorks(), so it never reaches the filesystem guard that decides
    this on an actual tool-less Mac. Pointing CLICKGRAFT_CLT_DIR and
    DEVELOPER_DIR at an empty DIRECTORY runs that guard for real: xcode-select
    -p echoes DEVELOPER_DIR and exits 0, so both candidates exist and neither
    holds a python3.

    `nodev` must be a directory and never the empty string: cltDir treats "" as
    a real value, so "" + "/usr/bin/python3" is the literal /usr/bin/python3,
    which exists on every Mac including a clean one, and the guard would pass.
    """
    env = dict(os.environ, CLICKGRAFT_PYTHON_HOME=str(home),
               CLICKGRAFT_PYTHON_PAYLOAD_URL=url,
               CLICKGRAFT_CLT_DIR=str(nodev), DEVELOPER_DIR=str(nodev))
    for hook in ("CLICKGRAFT_NO_SYSTEM_PYTHON", "CLICKGRAFT_PYTHON"):
        env.pop(hook, None)
    run = subprocess.run([exe, "--check-startup"], env=env,
                         capture_output=True, text=True, timeout=600)
    try:
        return run.returncode, json.loads(run.stdout.strip() or "{}")
    except ValueError:
        pytest.fail(f"--check-startup did not print JSON:\n{run.stdout}\n{run.stderr}")


def test_the_clean_mac_condition_is_reached_without_the_hook(exe, tmp_path):
    """The gate's B1b, in the suite: no-developer-tools concluded by the real
    filesystem guard, and the hook shown to be a faithful stand-in for it.

    Every other startup case sets CLICKGRAFT_NO_SYSTEM_PYTHON, which short-
    circuits systemPythonWorks() before the [cltDir, selectedDeveloperDir()]
    search — so until this existed nothing exercised the mechanism a clean Mac
    depends on, and a regression in it could not have failed anything.
    """
    nodev = tmp_path / "no-developer-dir"
    nodev.mkdir()
    honest_home, hook_home = tmp_path / "honest", tmp_path / "hook"
    honest_home.mkdir(), hook_home.mkdir()

    code, honest = _startup_clean(exe, honest_home, nodev)
    assert code == 2, f"real detection should report needs-runtime, got {honest}"
    assert honest["outcome"] == "needs-runtime" and honest["source"] == "none"
    assert not list(honest_home.iterdir()), "it fetched a runtime without being asked"

    # The hook must agree, because the rest of the suite and the gate rely on it
    # standing in for this condition. Divergence means they test something else.
    hook_code, hook = _startup(exe, hook_home, no_system=True)
    assert (code, honest) == (hook_code, hook), (
        "the hook and the real detection disagree, so every case using the hook "
        f"is testing something else: hook={hook} real={honest}")


def test_the_clean_mac_condition_is_what_produced_that_answer(exe, tmp_path):
    """The control for the test above, without which it proves nothing.

    It would pass just as happily if --check-startup had started answering
    needs-runtime unconditionally, so the same binary must reach a DIFFERENT
    answer when the developer tools are left visible. Skipped on a host that has
    none: there the clean condition is indistinguishable from the host's own
    state and cannot be shown to be load-bearing.
    """
    gate = _gate()
    if not gate._host_has_system_python():
        pytest.skip("this host has no usable /usr/bin/python3, so the clean-Mac "
                    "condition cannot be shown to be what produced the answer")
    seen_home = tmp_path / "seen"
    seen_home.mkdir()
    code, seen = _startup(exe, seen_home, no_system=False)
    assert code == 0 and seen["outcome"] == "ok", (
        f"with the tools visible it should use them, got {seen}")
    assert seen["source"] != "none", (
        "it answers needs-runtime even with the tools visible, so the clean-Mac "
        "test above proves nothing")


def _recording_developer_dir(tmp_path):
    """A developer directory holding no python3 but a recording `xcrun`.

    The xcrun stub at /usr/bin/python3 forwards to $DEVELOPER_DIR/usr/bin/xcrun
    (it says so itself: "missing xcrun at: <DEVELOPER_DIR>/usr/bin/xcrun"), so a
    recorder placed there is a direct, behavioural answer to "did anything run
    the stub?" -- which is the question, not what the JSON happens to say.

    No python3 in it, so Toolchain.systemPythonWorks()'s filesystem guard still
    concludes there is none and the source is .none.
    """
    dev = tmp_path / "devdir"
    (dev / "usr/bin").mkdir(parents=True)
    marker = tmp_path / "stub-was-run"
    xcrun = dev / "usr/bin/xcrun"
    xcrun.write_text(f'#!/bin/sh\necho ran >> "{marker}"\nexit 1\n')
    xcrun.chmod(0o755)
    return dev, marker


def _app_without_a_pin(clickgraft_app, tmp_path):
    """A copy of the shipped app with python-pin.json removed, re-sealed.

    python-pin.json is a sealed resource: deleting it inside a SIGNED app makes
    the kernel SIGKILL the binary on exec, which shows up as a bare assert -9.
    Ad-hoc re-signing the copy is what test_python_payload's _repin does for the
    same reason.
    """
    copy = tmp_path / "nopin"
    subprocess.run(["/bin/cp", "-R", clickgraft_app, str(copy)], check=True,
                   capture_output=True)
    os.remove(os.path.join(copy, "Contents", "Resources", "python-pin.json"))
    subprocess.run(["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(copy)],
                   check=True, capture_output=True)
    return os.path.join(copy, "Contents", "MacOS", "ClickGraft")


def test_a_build_with_no_pin_says_so_instead_of_running_the_stub(clickgraft_app, tmp_path):
    """The one thing ClickGraft exists to spare people is Apple's "install the
    command line developer tools" dialog. Until 29 Sep 2026 a bundle whose
    python-pin.json could not be read raised exactly that.

    needsPython was `source == .none && pin != nil`, so with no pin it was
    FALSE -- and both gates that depend on it (Startup.check and the wizard's
    showRequirements) fell through to the backend, which ran Toolchain.python,
    which for .none was the literal "/usr/bin/python3". On a Mac with no
    developer tools that is the xcrun stub, and running it IS the dialog.
    Observed on a clean macOS 12.4 guest, which reported `backend-failed` with
    the contradictory `source: "none", path: null`.
    """
    dev, marker = _recording_developer_dir(tmp_path)
    exe = _app_without_a_pin(clickgraft_app, tmp_path)
    env = dict(os.environ, CLICKGRAFT_CLT_DIR=str(dev), DEVELOPER_DIR=str(dev),
               CLICKGRAFT_PYTHON_HOME=str(tmp_path / "home"))
    for hook in ("CLICKGRAFT_NO_SYSTEM_PYTHON", "CLICKGRAFT_PYTHON"):
        env.pop(hook, None)
    run = subprocess.run([exe, "--check-startup"], env=env, capture_output=True,
                         text=True, timeout=600)
    got = json.loads(run.stdout.strip() or "{}")

    assert not marker.exists(), (
        "it ran /usr/bin/python3 -- on a Mac with no developer tools that is "
        "macOS's offer to install them, which is the whole thing this avoids")
    assert got.get("outcome") == "no-runtime", f"expected no-runtime, got {got}"
    assert run.returncode == 1, f"expected exit 1, got {run.returncode}"
    assert "python-pin.json" in got.get("detail", ""), (
        f"the detail should name what is missing: {got}")


def test_the_stub_recorder_would_have_caught_it(tmp_path):
    """The control for the test above, without which it proves nothing.

    `marker.exists() is False` is only evidence if the recorder fires when the
    stub IS run. It is the assertion that would have failed before the fix, so
    it has to be shown to work at all.
    """
    dev, marker = _recording_developer_dir(tmp_path)
    assert not marker.exists()
    subprocess.run(["/usr/bin/python3", "-c", ""],
                   env=dict(os.environ, DEVELOPER_DIR=str(dev)),
                   capture_output=True, timeout=300)
    assert marker.exists(), (
        "the recorder never fired, so 'the stub was not run' in the test above "
        "would pass even if it had been")


def test_a_build_with_a_pin_still_offers_the_fetch(clickgraft_app, tmp_path):
    """The other control: the fix must not turn a normal clean Mac into an
    error. Same condition, pin left in place -- it must still reach
    needs-runtime, and still not run the stub."""
    dev, marker = _recording_developer_dir(tmp_path)
    env = dict(os.environ, CLICKGRAFT_CLT_DIR=str(dev), DEVELOPER_DIR=str(dev),
               CLICKGRAFT_PYTHON_HOME=str(tmp_path / "home2"))
    for hook in ("CLICKGRAFT_NO_SYSTEM_PYTHON", "CLICKGRAFT_PYTHON"):
        env.pop(hook, None)
    exe = os.path.join(clickgraft_app, "Contents", "MacOS", "ClickGraft")
    run = subprocess.run([exe, "--check-startup"], env=env, capture_output=True,
                         text=True, timeout=600)
    got = json.loads(run.stdout.strip() or "{}")
    assert not marker.exists(), "it ran the stub even with a pin present"
    assert got.get("outcome") == "needs-runtime", f"got {got}"
    assert run.returncode == 2
