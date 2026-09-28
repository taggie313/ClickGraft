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
    process = re.search(r"private func process\(_ args: \[String\]\) -> Process \{.*?\n    \}",
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
