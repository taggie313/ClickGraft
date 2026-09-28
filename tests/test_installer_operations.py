"""C3/C4: cancellation belongs to one install attempt, driven deterministically.

These compile packaging/PythonPayload.swift -- unmodified -- into a small
harness and drive the real `Operation` type. That is deliberate. The defect
(F1) was a process-global cancellation flag that a retry reset, and neither
pytest nor a source-text assertion can reach it: pytest cannot pause a worker
mid-`ditto`, and a regex cannot tell a guard from an `if`. The review that
found F1 built exactly this harness to reproduce it; keeping it means the
acceptance cases run against the production types.

The harness itself is tests/swift/OperationHarness.swift.
"""

import os
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAYLOAD_SWIFT = os.path.join(ROOT, "packaging", "PythonPayload.swift")
HARNESS = os.path.join(ROOT, "tests", "swift", "OperationHarness.swift")


def _build_and_run(payload_source=None):
    """Compile the harness against the installer and run it.

    `payload_source` replaces PythonPayload.swift's text, for mutation controls.
    The repository is never modified: everything happens in a temp directory.
    """
    if sys.platform != "darwin":
        pytest.skip("the harness compiles Swift, which needs macOS")
    with tempfile.TemporaryDirectory(prefix="cg-op-harness-") as folder:
        payload = os.path.join(folder, "PythonPayload.swift")
        if payload_source is None:
            shutil.copyfile(PAYLOAD_SWIFT, payload)
        else:
            with open(payload, "w", encoding="utf-8") as handle:
                handle.write(payload_source)
        # Top-level code is only allowed in a file called main.swift, which is
        # the same reason build_app.sh compiles a copy under that name.
        main = os.path.join(folder, "main.swift")
        shutil.copyfile(HARNESS, main)
        binary = os.path.join(folder, "harness")
        build = subprocess.run(
            ["xcrun", "swiftc", "-O", "-target", "arm64-apple-macos12.0",
             "-module-cache-path", os.path.join(folder, "mc"),
             "-o", binary, payload, main],
            capture_output=True, text=True, timeout=600)
        if build.returncode:
            pytest.fail("the harness did not compile:\n" + build.stderr[-1500:])
        return subprocess.run([binary], capture_output=True, text=True, timeout=300)


def test_cancel_and_commit_never_both_win():
    """C3. Two threads race the same barrier 2000 times; the two answers must
    always disagree, and both orderings must actually occur -- a lock that
    always lets the same side win proves nothing about the other."""
    run = _build_and_run()
    assert run.returncode == 0, run.stdout + run.stderr
    assert "ALL PASSED" in run.stdout, run.stdout
    assert "both=0" in run.stdout and "neither=0" in run.stdout, run.stdout


def test_a_new_attempt_cannot_clear_an_older_ones_cancellation():
    """C4, and the defect itself. With the old process-global flag, creating
    the second attempt reset the state the first was about to read."""
    run = _build_and_run()
    assert "old attempt is STILL cancelled: got true" in run.stdout, run.stdout
    assert "old attempt cannot commit: got false" in run.stdout, run.stdout


def test_the_harness_fails_when_the_barrier_is_removed():
    """The control. A harness that passes against broken code is decoration.

    admitCommit stops consulting the cancellation flag -- exactly the shape of
    the original defect, where commit proceeded regardless of a cancel that had
    already been accepted.
    """
    with open(PAYLOAD_SWIFT, encoding="utf-8") as handle:
        source = handle.read()
    broken = source.replace(
        """    func admitCommit() -> Bool {
            gate.lock(); defer { gate.unlock() }
            if cancelledFlag { return false }
            committedFlag = true
            return true
        }""",
        """    func admitCommit() -> Bool {
            gate.lock(); defer { gate.unlock() }
            committedFlag = true
            return true
        }""")
    assert broken != source, "the mutation did not apply; has admitCommit moved?"
    run = _build_and_run(broken)
    assert run.returncode != 0, (
        "the harness passed with the cancel/commit barrier removed\n" + run.stdout)
    assert "both=" in run.stdout and "both=0" not in run.stdout, run.stdout
