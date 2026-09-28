"""G1-G3: the release gate tests the app it is about to ship.

Until 1.8.2 it did not. `source_gate` ran pytest and compiled its candidate
afterwards, while the behavioural tests read a hardcoded `dist/ClickGraft.app`
-- and the documented release order builds `dist/` *after* this gate, so the app
under test was normally the PREVIOUS release. A Swift change could be checked
against an older executable while the structural tests read newer source, and
with no `dist/` at all the behavioural tests skipped and a green pytest run
approved the release.

These hold the three properties that stops mattering by.
"""

import json
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(ROOT, "packaging", "check_release.py")


def _gate_source():
    with open(GATE, encoding="utf-8") as handle:
        return handle.read()


def _source_gate_body():
    src = _gate_source()
    body = re.search(r"def source_gate\(.*?\n\ndef ", src, re.S)
    assert body, "source_gate is gone"
    return body.group(0)


# --- G1: the candidate is built before it is tested ------------------------

def test_the_gate_builds_its_candidate_before_it_runs_the_tests():
    """Ordering, not vocabulary: build_app.sh must appear before pytest."""
    body = _source_gate_body()
    build = body.index("build_app.sh")
    tests = body.index("'-m', 'pytest'")
    assert build < tests, (
        "the gate still runs the tests before it builds the candidate, so they "
        "test whatever is in dist/")


def test_the_gate_hands_the_candidate_path_to_the_tests():
    """And passes it explicitly, rather than hoping dist/ happens to match."""
    body = _source_gate_body()
    assert "'--clickgraft-app', str(candidate)" in body, \
        "the gate does not tell the tests which app to exercise"
    assert "'--release-check'" in body, \
        "a missing prerequisite would still skip rather than fail"


def test_the_tests_no_longer_freeze_a_candidate_at_import():
    """The module-level constants and skip decorator are what made dist/ sticky."""
    with open(os.path.join(ROOT, "tests", "test_python_payload.py"), encoding="utf-8") as h:
        src = h.read()
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert not re.search(r'^APP = .*dist', code, re.M), "dist/ is frozen at import again"
    assert not re.search(r'^EXE = ', code, re.M)
    assert "needs_app = pytest.mark.skipif" not in code, \
        "an import-time skip decorator decides before the candidate is known"


# --- G2/G3: what --release-check changes -----------------------------------

def _run_pytest(args, cwd=ROOT):
    return subprocess.run([sys.executable, "-m", "pytest", "-q", *args],
                          cwd=cwd, capture_output=True, text=True, timeout=600)


def test_a_missing_candidate_skips_in_development_and_fails_in_release():
    """G3. The same absent app must read differently in the two modes: a
    contributor can still run most of the suite, a release cannot approve
    itself by skipping the cases that matter."""
    missing = os.path.join(ROOT, "dist", "no-such-candidate.app")

    dev = _run_pytest(["tests/test_python_payload.py", "-k", "fetches_verifies",
                       "--clickgraft-app", missing])
    assert "skipped" in dev.stdout or "no tests ran" in dev.stdout, dev.stdout[-600:]
    assert "failed" not in dev.stdout, dev.stdout[-600:]

    rel = _run_pytest(["tests/test_python_payload.py", "-k", "fetches_verifies",
                       "--clickgraft-app", missing, "--release-check"])
    assert rel.returncode != 0, "release mode approved a run with no candidate"
    assert "release check" in (rel.stdout + rel.stderr), (rel.stdout + rel.stderr)[-600:]


def test_a_payload_that_is_not_the_pinned_one_fails_in_release(tmp_path):
    """G3, the payload half. A mismatched archive is not a reason to skip a
    release; it is a reason to stop."""
    wrong = tmp_path / "not-the-payload.zip"
    wrong.write_bytes(b"not the pinned archive")
    rel = _run_pytest(["tests/test_python_payload.py", "-k", "fetches_verifies",
                       "--clickgraft-payload", str(wrong), "--release-check"])
    assert rel.returncode != 0
    assert "not the archive the pin names" in (rel.stdout + rel.stderr)


def test_release_mode_is_off_by_default():
    """The control: without the flag, the suite must still be runnable by
    someone with no Developer ID and no 17 MB archive."""
    with open(os.path.join(ROOT, "tests", "conftest.py"), encoding="utf-8") as h:
        conf = h.read()
    assert 'default=False' in conf
    assert '"--release-check"' in conf


def test_a_broken_candidate_fails_even_when_dist_is_healthy(tmp_path, clickgraft_app):
    """G2. The point of the whole change.

    Seed the situation the gate used to be in: a perfectly good app in dist/,
    and a candidate with a defect. The tests must follow the candidate and fail.
    Before 1.8.2 they read dist/ and passed.

    The defect is injected into a COPY; dist/ is never touched. It is a stub
    launcher rather than a subtle bug, because what is under test is which
    binary the suite executes, not how cleverly it detects breakage.
    """
    assert os.path.exists(os.path.join(clickgraft_app, "Contents/MacOS/ClickGraft")), \
        "this test needs a healthy app to contrast against"

    import shutil
    broken = tmp_path / "broken"
    broken.mkdir()
    shutil.copytree(clickgraft_app, broken / "ClickGraft.app", symlinks=True)
    launcher = broken / "ClickGraft.app/Contents/MacOS/ClickGraft"
    launcher.unlink()
    launcher.write_text("#!/bin/sh\necho 'candidate is broken' >&2\nexit 3\n")
    launcher.chmod(0o755)

    run = _run_pytest(["tests/test_python_payload.py",
                       "-k", "a_mac_with_its_own_interpreter_fetches_nothing",
                       "--clickgraft-app", str(broken / "ClickGraft.app"),
                       "--release-check"])
    assert run.returncode != 0, (
        "the suite passed against a candidate whose launcher exits 3 -- it is "
        "still testing dist/\n" + run.stdout[-800:])

    # The control: the same test against the healthy app passes, so the failure
    # above is the candidate and not the test being broken outright.
    ok = _run_pytest(["tests/test_python_payload.py",
                      "-k", "a_mac_with_its_own_interpreter_fetches_nothing",
                      "--clickgraft-app", clickgraft_app, "--release-check"])
    assert ok.returncode == 0, ok.stdout[-800:]
