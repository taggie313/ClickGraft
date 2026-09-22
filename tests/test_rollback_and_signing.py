"""
tests/test_rollback_and_signing.py — The copy a build replaces is kept until the
new one is proven, and a signing failure is a failure.

Up to 1.5.8 build.py deleted the copy at the output path and then renamed the
new one into place, and the wizard verified the new copy only after that. A copy
that failed its checks had already cost the owner the one that worked, and a
failed rename cost them both (demonstrated 22 Sep 2026). Every codesign ran with
check=False, so a signing step could fail on every file and sign_bundle() still
returned, and the re-seal after the test launch said "PASSED" without checking.

These tests hold the replacement: the old copy is renamed aside and comes back,
byte for byte and at the same inode, when the new copy fails its checks or
cannot be put in place; a build that crashed in between is found and offered
on the next run; codesign's own words stop the build before anything moves;
and a re-seal that does not verify says FAILED.

Most of this runs on fake bundles in a temporary folder, with the tools stood
in for by scripts on PATH. The three tests that need a real copy build one from a
stock HP Click and skip without one.
Target: Python 3.9+
"""

import hashlib
import io
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout

import pytest

from clickgraft import agent, build, signing, verify
from clickgraft.build import (Built, InstallError, LeftoverPendingError, NotTheBuildsCopyError,
                              OutputInUseError, install_copy, leftovers, restore_previous)
from clickgraft.manifest import ManifestManager
from clickgraft.signing import SigningError, sign_bundle
from clickgraft.verify import VerifyError

NAME = "HP Click (Apple Silicon).app"


@pytest.fixture
def tmp():
    d = tempfile.mkdtemp(prefix="cg-rollback-")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _copy(folder, version, marker, name=NAME):
    """A fake ClickGraft copy with a marker file of random bytes inside it."""
    app = os.path.join(folder, name)
    os.makedirs(os.path.join(app, "Contents", "Resources"))
    with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
        plistlib.dump({"CFBundleShortVersionString": version,
                       "CFBundleIdentifier": agent.COPY_BUNDLE_ID}, f)
    with open(os.path.join(app, "Contents", "Resources", "marker.bin"), "wb") as f:
        f.write(marker)
    return app


def _tree(path):
    """Every file under path and its SHA-256, so "as it was" can be checked."""
    out = {}
    for root, _dirs, files in os.walk(path):
        for fn in files:
            fp = os.path.join(root, fn)
            with open(fp, "rb") as f:
                out[os.path.relpath(fp, path)] = hashlib.sha256(f.read()).hexdigest()
    return out


def _set_aside(folder):
    return sorted(n for n in os.listdir(folder) if n.startswith(".clickgraft-"))


def _dead_pid():
    p = subprocess.Popen(["/usr/bin/true"])
    p.wait()
    return p.pid


@pytest.fixture
def quiet(monkeypatch):
    """Nothing is running from any bundle here (the real ps is not consulted)."""
    monkeypatch.setattr(build, "processes_inside", lambda path, ps=None: [])
    monkeypatch.setattr(agent, "processes_inside", lambda path, ps=None: [])


# ---------------------------------------------------------------------------
# install_copy: the old copy is renamed aside, never deleted


def test_install_sets_the_previous_copy_aside_intact(tmp, quiet):
    old = _copy(tmp, "4.8.117", os.urandom(4096))
    before, inode = _tree(old), os.stat(old).st_ino
    staging = _copy(tmp, "4.10.42", b"new", name="staging_clickgraft_1")

    previous = install_copy(staging, old)

    assert _tree(old)["Contents/Resources/marker.bin"] == hashlib.sha256(b"new").hexdigest()
    assert _tree(previous) == before and os.stat(previous).st_ino == inode
    # Beside it, hidden, and never with an .app name that Launch Services
    # would register.
    name = os.path.basename(previous)
    assert os.path.dirname(previous) == tmp
    assert name.startswith(".clickgraft-previous-") and not name.endswith(".app")
    assert name.endswith("-HP Click (Apple Silicon)")
    assert build.is_set_aside(previous)


def test_install_with_nothing_there_returns_none(tmp, quiet):
    staging = _copy(tmp, "4.10.42", b"new", name="staging_clickgraft_1")
    out = os.path.join(tmp, NAME)
    assert install_copy(staging, out) is None
    assert os.path.isdir(out) and _set_aside(tmp) == []


def test_an_open_copy_is_refused_before_anything_moves(tmp, monkeypatch):
    old = _copy(tmp, "4.8.117", b"old")
    staging = _copy(tmp, "4.10.42", b"new", name="staging_clickgraft_1")
    monkeypatch.setattr(build, "processes_inside",
                        lambda path, ps=None: [(4242, f"{old}/Contents/MacOS/HPClickExe")])
    with pytest.raises(OutputInUseError):
        install_copy(staging, old)
    assert _tree(old)["Contents/Resources/marker.bin"] == hashlib.sha256(b"old").hexdigest()
    assert os.path.isdir(staging) and _set_aside(tmp) == []


def _failing_rename(monkeypatch, fail_from):
    """os.rename, except that renaming anything in `fail_from` fails."""
    real = os.rename

    def rename(src, dst):
        if any(os.path.basename(src).startswith(p) for p in fail_from):
            raise OSError(28, "No space left on device (injected)")
        return real(src, dst)
    monkeypatch.setattr(build.os, "rename", rename)


def test_a_failed_rename_puts_the_previous_copy_back(tmp, quiet, monkeypatch):
    old = _copy(tmp, "4.8.117", os.urandom(4096))
    before, inode = _tree(old), os.stat(old).st_ino
    staging = _copy(tmp, "4.10.42", b"new", name="staging_clickgraft_1")
    _failing_rename(monkeypatch, ["staging_clickgraft_"])

    with pytest.raises(InstallError) as exc:
        install_copy(staging, old)

    assert exc.value.previous_copy == "restored"
    assert "put back as it was" in str(exc.value) and "injected" in str(exc.value)
    assert _tree(old) == before and os.stat(old).st_ino == inode
    assert _set_aside(tmp) == []


def test_a_failed_rename_and_a_failed_undo_say_where_the_copy_is(tmp, quiet, monkeypatch):
    old = _copy(tmp, "4.8.117", os.urandom(4096))
    before = _tree(old)
    staging = _copy(tmp, "4.10.42", b"new", name="staging_clickgraft_1")
    _failing_rename(monkeypatch, ["staging_clickgraft_", ".clickgraft-previous-"])

    with pytest.raises(InstallError) as exc:
        install_copy(staging, old)

    assert exc.value.previous_copy == "aside"
    assert exc.value.previous_path in str(exc.value)
    assert _tree(exc.value.previous_path) == before


# ---------------------------------------------------------------------------
# The wizard's build route: verify decides whether the old copy comes back


def _run(argv):
    out = io.StringIO()
    with redirect_stdout(out):
        code = agent.main(argv)
    return code, [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


@pytest.fixture
def route(tmp, quiet, monkeypatch):
    """agent build with a build that makes a fake copy and installs it for real,
    and a verify the test decides. Nothing reads /Applications or the real ps."""
    manifest = ManifestManager().manifests["4.10.42"]
    state = {"verify": lambda out: (True, {"smoke_launch": "PASSED (fake)"}), "built": 0}
    source = os.path.join(tmp, "4.10.42 HP Click.app")
    out = os.path.join(tmp, NAME)

    def fake_build(source_app_path, output_app_path, **_kw):
        state["built"] += 1
        staging = _copy(tmp, "4.10.42", b"the new copy", name=f"staging_clickgraft_{os.getpid()}")
        return Built(output_app_path, install_copy(staging, output_app_path))

    def fake_verify(output, manifest=None):
        return state["verify"](output)

    monkeypatch.setattr(agent, "_manifest_for", lambda mm, source: manifest)
    monkeypatch.setattr(agent, "_log_path", lambda: None)
    monkeypatch.setattr(agent, "resolve_output", lambda: (out, False))
    monkeypatch.setattr(agent, "build_apple_silicon_bundle", fake_build)
    monkeypatch.setattr(agent, "verify_app_bundle", fake_verify)
    state.update(source=source, out=out)
    return state


def _fail_verify(check="smoke_launch"):
    def verify_that_fails(_out):
        raise VerifyError("Smoke launch FAILED: the app quit on its own (injected).", check,
                          {"architectures": "PASSED (fake)"})
    return verify_that_fails


def test_a_failed_verify_puts_the_previous_copy_back_byte_for_byte(tmp, route):
    old = _copy(tmp, "4.8.117", os.urandom(65536))
    before, inode = _tree(old), os.stat(old).st_ino
    route["verify"] = _fail_verify()

    code, events = _run(["build", "--source", route["source"], "--out", old])

    assert code == 1 and route["built"] == 1
    ev = events[-1]
    assert ev["type"] == "error" and ev["stage"] == "verify"
    assert ev["check"] == "smoke_launch"
    assert ev["previous_copy"] == "restored" and ev["new_copy"] == "removed"
    assert ev["results"] == {"architectures": "PASSED (fake)"}
    assert _tree(old) == before and os.stat(old).st_ino == inode
    assert _set_aside(tmp) == [], "the failed copy or the backup was left behind"


def test_with_no_previous_copy_a_failed_verify_keeps_the_new_one(tmp, route):
    route["verify"] = _fail_verify("code_signature")
    code, events = _run(["build", "--source", route["source"], "--out", route["out"]])
    ev = events[-1]
    assert code == 1 and ev["stage"] == "verify" and ev["check"] == "code_signature"
    assert ev["previous_copy"] == "none" and ev["new_copy"] == "kept"
    assert os.path.isdir(route["out"]) and _set_aside(tmp) == []


def test_a_passed_verify_removes_the_previous_copy(tmp, route):
    old = _copy(tmp, "4.8.117", b"old")
    code, events = _run(["build", "--source", route["source"], "--out", old])
    ev = events[-1]
    assert code == 0 and ev["type"] == "done" and ev["previous_copy"] == "replaced"
    assert "previous_left" not in ev
    assert _tree(old)["Contents/Resources/marker.bin"] == hashlib.sha256(b"the new copy").hexdigest()
    assert _set_aside(tmp) == []


def test_a_failed_verify_with_the_new_copy_open_keeps_both(tmp, route, monkeypatch):
    """Opened in the seconds after the test launch stopped: deleting it would
    pull its files out from under it, so the old copy stays set aside and the
    wizard is told where."""
    old = _copy(tmp, "4.8.117", b"old")
    opened = []

    def verify_then_open(_out):
        opened.append(True)
        _fail_verify()(_out)
    route["verify"] = verify_then_open
    monkeypatch.setattr(build, "processes_inside", lambda path, ps=None:
                        [(777, f"{path}/Contents/MacOS/HPClickExe")] if opened else [])

    code, events = _run(["build", "--source", route["source"], "--out", old])
    ev = events[-1]
    assert code == 1 and ev["previous_copy"] == "aside" and ev["new_copy"] == "kept"
    assert ev["restore_reason"] == "open" and os.path.isdir(ev["previous_path"])
    assert _tree(ev["previous_path"])["Contents/Resources/marker.bin"] == \
        hashlib.sha256(b"old").hexdigest()


def test_a_build_failure_before_install_says_the_copy_is_untouched(tmp, route, monkeypatch):
    old = _copy(tmp, "4.8.117", b"old")
    before = _tree(old)

    def build_that_fails(**_kw):
        raise SigningError("Signing failed: codesign could not sign HPClickExe.")
    monkeypatch.setattr(agent, "build_apple_silicon_bundle", build_that_fails)
    code, events = _run(["build", "--source", route["source"], "--out", old])
    ev = events[-1]
    assert code == 1 and ev["stage"] == "build" and ev["previous_copy"] == "untouched"
    assert _tree(old) == before


# ---------------------------------------------------------------------------
# A build killed between install and verify: found next time, never deleted


def _leave_a_crashed_build(tmp, monkeypatch, marker):
    """What a build killed mid-verify leaves: the new copy in place and the old
    one set aside under the pid of a process that is no longer running."""
    old = _copy(tmp, "4.8.117", marker)
    staging = _copy(tmp, "4.10.42", b"unchecked new copy", name="staging_clickgraft_1")
    dead = _dead_pid()
    with monkeypatch.context() as m:
        m.setattr(build.os, "getpid", lambda: dead)
        previous = install_copy(staging, old)
    return old, previous


def test_env_reports_a_previous_copy_left_set_aside(tmp, route, monkeypatch):
    old, previous = _leave_a_crashed_build(tmp, monkeypatch, b"old")
    monkeypatch.setattr(agent, "SYSTEM_APPS", tmp)
    monkeypatch.setattr(agent, "USER_APPS", tmp)
    monkeypatch.setattr(agent, "candidates", lambda mm: [])
    code, events = _run(["env"])
    assert code == 0
    found = events[-1]["leftovers"]
    assert len(found) == 1
    item = found[0]
    assert item["path"] == os.path.join(os.path.realpath(tmp), os.path.basename(previous))
    assert item["restores_to"] == os.path.join(os.path.realpath(tmp), NAME)
    assert item["version"] == "4.8.117" and item["made_by_clickgraft"] is True
    assert item["restores_to_exists"] is True and item["current_version"] == "4.10.42"
    # The copy at the path is the one that build put there, and it never
    # finished its checks.
    assert item["state"] == "installed" and item["check"] == ""
    # Reported, not acted on.
    assert os.path.isdir(previous)


def test_a_running_build_owns_its_set_aside_copy(tmp, quiet):
    old = _copy(tmp, "4.8.117", b"old")
    staging = _copy(tmp, "4.10.42", b"new", name="staging_clickgraft_1")
    install_copy(staging, old)             # under this process's own pid
    assert leftovers(tmp) == []


def test_restore_previous_puts_the_leftover_back(tmp, route, monkeypatch):
    marker = os.urandom(65536)
    old, previous = _leave_a_crashed_build(tmp, monkeypatch, marker)
    before, inode = _tree(previous), os.stat(previous).st_ino
    code, events = _run(["restore-previous", "--backup", previous])
    assert code == 0 and events[-1] == {"type": "restored", "output": old, "removed_copy": True}
    assert _tree(old) == before and os.stat(old).st_ino == inode
    assert _set_aside(tmp) == []


def test_discard_previous_deletes_only_the_leftover(tmp, route, monkeypatch):
    old, previous = _leave_a_crashed_build(tmp, monkeypatch, b"old")
    code, events = _run(["discard-previous", "--backup", previous])
    assert code == 0 and events[-1]["type"] == "discarded"
    assert _set_aside(tmp) == []
    assert _tree(old)["Contents/Resources/marker.bin"] == \
        hashlib.sha256(b"unchecked new copy").hexdigest()


def test_the_leftover_commands_touch_nothing_else(tmp, route, monkeypatch):
    elsewhere = _copy(tmp, "4.8.117", b"someone's app", name="Other.app")
    for cmd in ("restore-previous", "discard-previous"):
        code, events = _run([cmd, "--backup", elsewhere])
        assert code == 1 and events[-1]["stage"] == "leftover"
    assert os.path.isdir(elsewhere)


def test_the_next_build_sweeps_what_an_earlier_one_meant_to_delete(tmp, quiet, monkeypatch):
    out = os.path.join(tmp, NAME)
    dead = _dead_pid()
    with monkeypatch.context() as m:
        m.setattr(build.os, "getpid", lambda: dead)
        junk = build._set_aside_path(out, build.DISCARD)
    os.makedirs(os.path.join(junk, "Contents"))
    kept = _copy(tmp, "4.8.117", b"old")
    with monkeypatch.context() as m:
        m.setattr(build.os, "getpid", lambda: dead)
        previous = build._set_aside_path(out, build.PREVIOUS)
    os.rename(kept, previous)
    build.sweep_discards(tmp)
    assert not os.path.exists(junk)
    assert os.path.isdir(previous), "the owner's previous copy is never swept"


# ---------------------------------------------------------------------------
# Found in review, 22 Sep 2026: what a leftover is, and who may remove what


def _label(path):
    with open(os.path.join(path, "Contents", "Resources", "marker.bin"), "rb") as f:
        return f.read()


def _crashed_install(tmp, monkeypatch, out, new_marker):
    """install_copy of a new copy over whatever is at `out`, by a build that
    then died: the set-aside copy carries a pid that is no longer running."""
    staging = _copy(tmp, "4.10.42", new_marker, name=f"staging_clickgraft_{len(os.listdir(tmp))}")
    dead = _dead_pid()
    with monkeypatch.context() as m:
        m.setattr(build.os, "getpid", lambda: dead)
        previous = install_copy(staging, out)
    return previous


def test_a_leftover_that_has_gone_is_refused_without_claiming_it_is_there(tmp, route, monkeypatch):
    """The wizard adds "still safe, set aside where it was" to this refusal,
    so the refusal has to say whether it is. Deleted in the Finder, or put back
    from a second window, after the leftover screen was drawn: until review
    (22 Sep 2026) the wizard claimed the copy for a path holding nothing."""
    out = _copy(tmp, "4.8.117", b"owner's")
    previous = _crashed_install(tmp, monkeypatch, out, b"unchecked")
    shutil.rmtree(previous)
    code, events = _run(["restore-previous", "--backup", previous])
    assert code == 1 and events[-1]["stage"] == "leftover"
    assert events[-1]["backup_exists"] is False


def test_two_interrupted_builds_unwind_to_the_owners_copy(tmp, route, monkeypatch):
    """Two interrupted builds, "Decide later" in between, then "Put it back"
    on each leftover the wizard shows, in the order it shows them. Before the
    review the second press deleted the owner's copy to make room for the
    first build's unchecked one."""
    owners = os.urandom(65536)
    out = _copy(tmp, "4.8.117", owners)
    _crashed_install(tmp, monkeypatch, out, b"A: from build 1")
    # Distinct timestamps, as two builds a minute apart would have.
    first = leftovers(tmp)[0]["path"]
    renamed = os.path.join(tmp, re.sub(r"-\d{8}-\d{6}", "-20260922-000000",
                                       os.path.basename(first), count=1))
    os.rename(first, renamed)
    os.rename(build._record_path(first), build._record_path(renamed))
    _crashed_install(tmp, monkeypatch, out, b"B: from build 2")

    shown = []
    for _ in range(3):
        items = leftovers(tmp)
        if not items:
            break
        shown.append((_label(items[0]["path"]), items[0]["state"]))
        code, events = _run(["restore-previous", "--backup", items[0]["path"]])
        assert code == 0 and events[-1]["type"] == "restored", events[-1]
    assert shown == [(b"A: from build 1", "installed"), (owners, "installed")]
    assert _label(out) == owners
    assert _set_aside(tmp) == []


def test_a_copy_the_build_did_not_put_there_is_never_removed(tmp, route, monkeypatch):
    out = _copy(tmp, "4.8.117", b"owner's")
    previous = _crashed_install(tmp, monkeypatch, out, b"unchecked")
    # Something else takes the path: the owner's own doing, or a build by a
    # ClickGraft from before 1.5.9.
    shutil.rmtree(out)
    _copy(tmp, "4.10.42", b"someone else's copy")
    assert leftovers(tmp)[0]["state"] == "other"

    with pytest.raises(NotTheBuildsCopyError) as exc:
        restore_previous(out, previous)
    assert exc.value.previous_copy == "aside" and "Nothing has been moved" in str(exc.value)
    code, events = _run(["restore-previous", "--backup", previous])
    assert code == 1 and events[-1]["stage"] == "leftover"
    assert "won't remove it" in events[-1]["error"] and events[-1]["backup_exists"] is True
    assert _label(out) == b"someone else's copy" and _label(previous) == b"owner's"

    # Moved out of the way by hand, it goes back.
    shutil.rmtree(out)
    assert leftovers(tmp)[0]["state"] == "missing"
    code, events = _run(["restore-previous", "--backup", previous])
    assert code == 0 and events[-1]["removed_copy"] is False and _label(out) == b"owner's"


def test_a_build_will_not_start_over_a_copy_waiting_for_the_owner(tmp, route, monkeypatch):
    out = _copy(tmp, "4.8.117", b"owner's")
    previous = _crashed_install(tmp, monkeypatch, out, b"unchecked")
    source = os.path.join(tmp, "Source.app")
    os.makedirs(source)
    with pytest.raises(LeftoverPendingError) as exc:
        build.build_apple_silicon_bundle(source, out, manifest={}, allow_foreign_host=True)
    assert exc.value.leftovers[0]["path"] == previous
    assert "Nothing has been downloaded or changed" in str(exc.value)

    # And through the wizard's route, which then shows the leftover screen.
    os.rename(source, route["source"])
    monkeypatch.setattr(agent, "build_apple_silicon_bundle", build.build_apple_silicon_bundle)
    code, events = _run(["build", "--source", route["source"], "--out", out])
    ev = events[-1]
    assert code == 1 and ev["stage"] == "leftover_pending" and ev["previous_copy"] == "untouched"
    assert [i["path"] for i in ev["leftovers"]] == [previous]
    assert ev["leftovers"][0]["version"] == "4.8.117"
    assert ev["leftovers"][0]["current_version"] == "4.10.42"
    assert agent._pending_leftover(out)["path"] == previous
    assert _label(out) == b"unchecked" and _label(previous) == b"owner's"


class _Gone(io.StringIO):
    """A stdout whose reader goes away once `after` has been written, as the
    wizard's pipe does when it is quit mid-build."""

    def __init__(self, after):
        super().__init__()
        self.after, self.gone = after, False

    def write(self, text):
        if self.gone:
            raise BrokenPipeError(32, "Broken pipe (injected)")
        n = super().write(text)
        self.gone = self.after in text
        return n


def _run_gone(argv, after):
    out = _Gone(after)
    with redirect_stdout(out):
        code = agent.main(argv)
    return code, [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def test_a_passed_verify_settles_after_the_wizard_has_gone(tmp, route):
    """The review's probe: the wizard quit during "Verifying", the agent
    carried on, verify passed, and the write that followed raised before the
    old copy was deleted, leaving it hidden beside a copy that had passed."""
    old = _copy(tmp, "4.8.117", b"old")
    code, events = _run_gone(["build", "--source", route["source"], "--out", old],
                             after="Verifying the result")
    assert code == 0 and events[-1]["msg"] == "Verifying the result…"
    assert _label(old) == b"the new copy" and _set_aside(tmp) == []


def test_a_failed_verify_settles_after_the_wizard_has_gone(tmp, route):
    old = _copy(tmp, "4.8.117", os.urandom(4096))
    before = _tree(old)
    route["verify"] = _fail_verify()
    code, _events = _run_gone(["build", "--source", route["source"], "--out", old],
                              after="Verifying the result")
    assert code == 1 and _tree(old) == before and _set_aside(tmp) == []


def _failing_rename_to(monkeypatch, prefix):
    """os.rename, except that renaming anything TO a name starting `prefix` fails."""
    real = os.rename

    def rename(src, dst):
        if os.path.basename(dst).startswith(prefix):
            raise PermissionError(1, "Operation not permitted (injected)")
        return real(src, dst)
    monkeypatch.setattr(build.os, "rename", rename)


def test_a_restore_that_cannot_make_room_still_answers(tmp, route, monkeypatch):
    """restore_previous's first rename, the new copy out of the way, was
    outside its try: an OSError there escaped, and the wizard waited forever
    on "Checking the result"."""
    old = _copy(tmp, "4.8.117", b"old")
    route["verify"] = _fail_verify("smoke_launch")
    real_build = agent.build_apple_silicon_bundle

    def build_then_break_renames(**kw):
        built = real_build(**kw)
        _failing_rename_to(monkeypatch, ".clickgraft-discard-")
        return built
    monkeypatch.setattr(agent, "build_apple_silicon_bundle", build_then_break_renames)
    code, events = _run(["build", "--source", route["source"], "--out", old])
    ev = events[-1]
    assert code == 1 and ev["type"] == "error" and ev["stage"] == "verify"
    assert ev["previous_copy"] == "aside" and ev["new_copy"] == "kept"
    assert ev["restore_reason"] == "error" and "injected" in ev["restore_error"]
    assert _label(ev["previous_path"]) == b"old"
    # What the next `agent env` will say of it: the copy there failed, and which check.
    assert build._leftover_state(ev["previous_path"], old) == ("failed", "smoke_launch")


def test_a_passed_verify_whose_delete_fails_leaves_it_marked_passed(tmp, route, monkeypatch):
    old = _copy(tmp, "4.8.117", b"old")
    real_build = agent.build_apple_silicon_bundle

    def build_then_break_renames(**kw):
        built = real_build(**kw)
        _failing_rename_to(monkeypatch, ".clickgraft-discard-")
        return built
    monkeypatch.setattr(agent, "build_apple_silicon_bundle", build_then_break_renames)
    code, events = _run(["build", "--source", route["source"], "--out", old])
    ev = events[-1]
    assert code == 0 and ev["type"] == "done"
    assert ev["previous_copy"] == "aside" and _label(ev["previous_path"]) == b"old"
    assert build._leftover_state(ev["previous_path"], old) == ("passed", "")


def test_a_failed_progress_message_after_the_install_puts_the_copy_back(tmp, quiet):
    """Every message after install_copy is inside the try that undoes it. The
    "set aside" message once was not (review, 22 Sep 2026)."""
    for fail_on in ("is set aside at", "BUILD COMPLETED"):
        old = _copy(tmp, "4.8.117", os.urandom(4096))
        before, inode = _tree(old), os.stat(old).st_ino
        staging = _copy(tmp, "4.10.42", b"new", name="staging_clickgraft_1")
        seen = []

        def log(msg, pct=0.0):
            seen.append(msg)
            if fail_on in msg:
                raise BrokenPipeError(32, "Broken pipe (injected)")

        with pytest.raises(InstallError) as exc:
            build._install_and_report(staging, old, log)
        assert exc.value.previous_copy == "restored", fail_on
        assert _tree(old) == before and os.stat(old).st_ino == inode
        assert _set_aside(tmp) == [] and not os.path.exists(staging)
        shutil.rmtree(old)


def test_one_build_at_a_time_in_a_folder(tmp, route):
    """Two builds into one folder could interleave so that the second one's
    rollback put a failed copy back over the owner's, and deleted that."""
    old = _copy(tmp, "4.8.117", b"old")
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl, os, sys\n"
         "fd = os.open(sys.argv[1], os.O_RDONLY)\n"
         "fcntl.flock(fd, fcntl.LOCK_EX)\n"
         "print('held', flush=True)\n"
         "sys.stdin.read()\n", tmp],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        code, events = _run(["build", "--source", route["source"], "--out", old])
        ev = events[-1]
        assert code == 1 and ev["stage"] == "busy" and ev["previous_copy"] == "untouched"
        assert "Another ClickGraft" in ev["error"] and route["built"] == 0
        with pytest.raises(build.BuildInProgressError):
            with build.folder_lock(tmp):
                pass
    finally:
        holder.stdin.close()
        holder.wait(timeout=10)
    assert _label(old) == b"old"
    # Free again, and re-entrant within one process, as agent.py and the build nest it.
    with build.folder_lock(tmp):
        with build.folder_lock(os.path.join(tmp, ".")):
            pass
    assert build._HELD == {}


def test_leftover_records_come_and_go_with_their_copy(tmp, quiet, monkeypatch):
    out = _copy(tmp, "4.8.117", b"old")
    previous = _crashed_install(tmp, monkeypatch, out, b"new")
    assert os.path.isfile(build._record_path(previous))
    assert build.discard_previous(previous) is None
    assert _set_aside(tmp) == [], "the record outlived its copy"

    previous = _crashed_install(tmp, monkeypatch, out, b"newer")
    os.rename(previous, previous + "-moved-by-hand")
    build.sweep_discards(tmp)
    assert _set_aside(tmp) == [os.path.basename(previous) + "-moved-by-hand"]


# ---------------------------------------------------------------------------
# The macOS the wizard states before the backend can run


def test_the_wizards_early_macos_floor_holds_for_every_stock_version():
    """ClickGraft.swift's Requirements screen tells a Mac below this that no
    copy can be made, before anything runs /usr/bin/python3 (whose Command
    Line Tools stub would offer a large install for nothing). It must never
    exceed what a copy of any supported version actually needs; HP's own files
    alone set 15.0 in all three (22 Sep 2026)."""
    with open(os.path.join(os.path.dirname(__file__), "..", "packaging",
                           "ClickGraft.swift"), encoding="utf-8") as f:
        swift = f.read()
    m = re.search(r"static let copiesNeedMacOS = OperatingSystemVersion\("
                  r"majorVersion: (\d+), minorVersion: (\d+), patchVersion: (\d+)\)", swift)
    assert m, "copiesNeedMacOS not found in ClickGraft.swift"
    stated = tuple(int(g) for g in m.groups())
    from clickgraft.macos_floor import plan_floor
    from tests.test_clickgraft import find_stock_bundle
    checked = 0
    for version, manifest in sorted(ManifestManager().manifests.items()):
        source = find_stock_bundle(version)
        if not source:
            continue
        hp_only = plan_floor(source, manifest, bottles={})["floor"]
        assert hp_only >= stated, (version, hp_only, stated)
        checked += 1
    if not checked:
        pytest.skip("no stock HP Click in /Applications to check it against")
    assert re.search(r'"4\.11\.31"', swift) and '"hp_lists_to": "26.0"' in swift


# ---------------------------------------------------------------------------
# Signing: codesign's own words stop the build


def _tool(bindir, name, script):
    """A stand-in for a command-line tool, first on PATH."""
    os.makedirs(bindir, exist_ok=True)
    path = os.path.join(bindir, name)
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + script + "\n")
    os.chmod(path, 0o755)
    return bindir


FAKE_CODESIGN_ERROR = "resource fork, Finder information, or similar detritus not allowed"


@pytest.fixture
def failing_codesign(tmp, monkeypatch):
    bindir = _tool(os.path.join(tmp, "bin"), "codesign",
                   f'echo "$@" >> "{tmp}/codesign.calls"\n'
                   f'for last; do :; done\necho "$last: {FAKE_CODESIGN_ERROR}" >&2\nexit 1')
    monkeypatch.setenv("PATH", bindir + os.pathsep + os.environ["PATH"])
    return bindir


def _signable(tmp):
    app = os.path.join(tmp, "Signable.app")
    lib = os.path.join(app, "Contents", "Resources", "app", "appData", "macx", "lib")
    os.makedirs(lib)
    with open(os.path.join(lib, "libexample.dylib"), "wb") as f:
        f.write(b"not really a dylib")
    return app


def test_a_failed_codesign_raises_with_what_codesign_said(tmp, failing_codesign):
    app = _signable(tmp)
    with pytest.raises(SigningError) as exc:
        sign_bundle(app)
    msg = str(exc.value)
    assert FAKE_CODESIGN_ERROR in msg
    assert "Contents/Resources/app/appData/macx/lib/libexample.dylib" in msg
    assert "codesign --force -s -" in msg
    # It stopped at the first failure rather than carrying on regardless.
    with open(os.path.join(tmp, "codesign.calls")) as f:
        assert len(f.read().splitlines()) == 1


def test_the_outer_signature_failing_is_a_failure_too(tmp, monkeypatch):
    app = os.path.join(tmp, "Outer.app")
    os.makedirs(os.path.join(app, "Contents", "MacOS"))
    bindir = _tool(os.path.join(tmp, "bin"), "codesign",
                   'for last; do :; done\necho "$last: code object is not signed at all" >&2\nexit 1')
    monkeypatch.setenv("PATH", bindir + os.pathsep + os.environ["PATH"])
    with pytest.raises(SigningError, match="code object is not signed at all") as exc:
        sign_bundle(app)
    assert "could not sign Outer.app" in str(exc.value)


def test_symlinks_are_not_signed_through(tmp, monkeypatch):
    app = _signable(tmp)
    lib = os.path.join(app, "Contents", "Resources", "app", "appData", "macx", "lib")
    os.symlink("missing.dylib", os.path.join(lib, "dangling.dylib"))
    os.symlink("libexample.dylib", os.path.join(lib, "libexample.1.dylib"))
    calls = os.path.join(tmp, "calls")
    bindir = _tool(os.path.join(tmp, "bin"), "codesign", f'echo "$@" >> "{calls}"')
    _tool(bindir, "xattr", "exit 0")
    monkeypatch.setenv("PATH", bindir + os.pathsep + os.environ["PATH"])
    assert sign_bundle(app) == []
    with open(calls) as f:
        signed = f.read()
    assert "libexample.dylib" in signed
    assert "dangling.dylib" not in signed and "libexample.1.dylib" not in signed


def test_the_optional_steps_are_notes_not_failures(tmp, monkeypatch):
    app = _signable(tmp)
    bindir = _tool(os.path.join(tmp, "bin"), "codesign", "exit 0")
    _tool(bindir, "xattr", 'echo "xattr: [Errno 1] Operation not permitted" >&2\nexit 1')
    monkeypatch.setenv("PATH", bindir + os.pathsep + os.environ["PATH"])
    notes = sign_bundle(app)
    assert len(notes) == 1 and "quarantine" in notes[0] and "not permitted" in notes[0]


def test_an_rpath_that_is_already_there_is_a_note(tmp, monkeypatch):
    fp = os.path.join(tmp, "AdobeACE")
    open(fp, "wb").close()
    bindir = _tool(os.path.join(tmp, "bin"), "install_name_tool",
                   'echo "error: install_name_tool: for: x (for architecture arm64) option '
                   '\\"-add_rpath @loader_path/..\\" would duplicate path, file already has '
                   'LC_RPATH for: @loader_path/.." >&2\nexit 1')
    monkeypatch.setenv("PATH", bindir + os.pathsep + os.environ["PATH"])
    notes = []
    signing._add_rpath(fp, tmp, notes)
    assert notes == ["AdobeACE already had the @loader_path/.. rpath"]

    _tool(bindir, "install_name_tool", 'echo "fatal error: truncated or malformed object" >&2\nexit 1')
    with pytest.raises(SigningError, match="truncated or malformed object"):
        signing._add_rpath(fp, tmp, notes)


# ---------------------------------------------------------------------------
# The re-seal after the test launch


def test_a_reseal_that_cannot_sign_reports_failed(tmp, monkeypatch):
    def sign_that_fails(path):
        raise SigningError(f"Signing failed: codesign could not sign X.app.\n"
                           f"codesign said: {FAKE_CODESIGN_ERROR}")
    monkeypatch.setattr(signing, "sign_bundle", sign_that_fails)
    ok, entry = verify.reseal(tmp)
    assert ok is False and entry.startswith("FAILED") and FAKE_CODESIGN_ERROR in entry


def test_a_reseal_that_does_not_verify_reports_failed(tmp, monkeypatch):
    monkeypatch.setattr(signing, "sign_bundle", lambda path: [])
    monkeypatch.setattr(verify, "codesign_verifies",
                        lambda path: (False, "a sealed resource is missing or invalid"))
    ok, entry = verify.reseal(tmp)
    assert ok is False and entry.startswith("FAILED")
    assert "a sealed resource is missing or invalid" in entry


def test_a_reseal_that_verifies_says_so(tmp, monkeypatch):
    monkeypatch.setattr(signing, "sign_bundle", lambda path: [])
    monkeypatch.setattr(verify, "codesign_verifies", lambda path: (True, ""))
    ok, entry = verify.reseal(tmp)
    assert ok is True and entry.startswith("PASSED") and "codesign --verify accepts it" in entry


def test_verify_names_the_check_that_failed(tmp):
    with pytest.raises(VerifyError) as exc:
        verify.verify_app_bundle(os.path.join(tmp, "Nowhere.app"))
    assert exc.value.check == "bundle" and isinstance(exc.value, ValueError)


# ---------------------------------------------------------------------------
# Real copies: skipped without a stock HP Click


def _stock():
    from tests.test_clickgraft import find_stock_bundle
    mm = ManifestManager()
    for version in ("4.10.42", "4.8.117", "4.8.118"):
        path = find_stock_bundle(version)
        if path and version in mm.manifests:
            return path, mm.manifests[version]
    return None, None


def test_a_real_build_whose_codesign_fails_leaves_the_previous_copy(tmp, failing_codesign):
    source, manifest = _stock()
    if source is None:
        pytest.skip("no stock HP Click with a manifest in /Applications")
    out_dir = os.path.join(tmp, "out")
    os.makedirs(out_dir)
    old = _copy(out_dir, "4.8.117", os.urandom(65536))
    before, inode = _tree(old), os.stat(old).st_ino

    with pytest.raises(SigningError) as exc:
        build.build_apple_silicon_bundle(source, old, manifest=manifest)

    assert FAKE_CODESIGN_ERROR in str(exc.value)
    assert _tree(old) == before and os.stat(old).st_ino == inode
    assert sorted(os.listdir(out_dir)) == [NAME], "staging or a set-aside copy was left"


def test_a_real_copy_whose_reseal_fails_fails_verify_at_resealed(tmp, monkeypatch):
    source, manifest = _stock()
    if source is None:
        pytest.skip("no stock HP Click with a manifest in /Applications")
    from clickgraft import hostarch
    if not hostarch.is_apple_silicon():
        pytest.skip("the re-seal follows the test launch, which needs Apple Silicon")
    out = os.path.join(tmp, NAME)
    built = build.build_apple_silicon_bundle(source, out, manifest=manifest)
    assert built == Built(out, None)

    # The launch itself is stood in for: what is under test is what happens
    # to its result.
    monkeypatch.setattr(verify, "smoke_launch", lambda app, manifest: {
        "ok": True, "message": "PASSED (stood in for)", "cleanup": None})

    def sign_that_fails(path):
        raise SigningError(f"Signing failed: codesign could not sign {NAME}.\n"
                           f"codesign said: {FAKE_CODESIGN_ERROR}")
    monkeypatch.setattr(signing, "sign_bundle", sign_that_fails)

    with pytest.raises(VerifyError) as exc:
        verify.verify_app_bundle(out, manifest=manifest)
    assert exc.value.check == "resealed"
    assert exc.value.results["resealed"].startswith("FAILED")
    assert FAKE_CODESIGN_ERROR in exc.value.results["resealed"]
    # Every check before it passed on the real copy, including the signature
    # the build made -- the same check the re-seal is now held to.
    assert exc.value.results["code_signature"].startswith("PASSED")
    assert exc.value.results["smoke_launch"] == "PASSED (stood in for)"


def test_a_real_build_that_fails_after_its_install_puts_the_copy_back(tmp):
    """The last thing a build does once the new copy is in place is report
    progress. If that fails -- the wizard gone and its pipe closed -- the build
    has not finished, so the old copy goes back, byte for byte."""
    source, manifest = _stock()
    if source is None:
        pytest.skip("no stock HP Click with a manifest in /Applications")
    out_dir = os.path.join(tmp, "out")
    os.makedirs(out_dir)
    old = _copy(out_dir, "4.8.117", os.urandom(65536))
    before, inode = _tree(old), os.stat(old).st_ino
    seen = []

    def progress(msg, pct):
        seen.append(msg)
        if pct >= 1.0:
            raise BrokenPipeError(32, "Broken pipe (injected)")

    with pytest.raises(InstallError) as exc:
        build.build_apple_silicon_bundle(source, old, manifest=manifest,
                                         progress_callback=progress)
    assert exc.value.previous_copy == "restored"
    assert any("set aside" in m for m in seen), "the build never got as far as the install"
    assert _tree(old) == before and os.stat(old).st_ino == inode
    assert sorted(os.listdir(out_dir)) == [NAME]
