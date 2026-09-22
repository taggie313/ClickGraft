"""
clickgraft.build — Main build pipeline for clickgraft.
Copies source HP Click app to HP Click (Apple Silicon).app, leaving original untouched.
Swaps Electron runtime, bundles dylibs, updates bundle IDs, patches ASAR, writes launcher script, and signs.
Target: Python 3.9+ (Standard Library only)
"""

import collections
import contextlib
import errno
import fcntl
import json
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import time
from clickgraft.asar import AsarArchive, patch_and_repack_asar
from clickgraft.deps import choose_bottles, fetch_electron, resolve_dylib
from clickgraft.macos_floor import (exact_floor, floor_reasons, format_version,
                                    plan_floor, refuse_if_too_old, stamp_minimum)
from clickgraft.macho import run_cmd
from clickgraft.patches import PatchEngine
from clickgraft.signing import sign_bundle
from clickgraft.verify import processes_inside


class OutputInUseError(RuntimeError):
    """The bundle at the output path is running, so it must not be deleted.

    Carries .pids and .output so a caller can say which process to quit.
    """

    def __init__(self, message, output, pids):
        super().__init__(message)
        self.output = output
        self.pids = list(pids)


def _open_from(app_path):
    """Pids running from inside app_path; [] for a path that cannot have any.

    build.py's --out is any path the caller likes, and processes_inside refuses
    anything that is not an .app rather than guess at a prefix that could match
    half the Mac.
    """
    try:
        return [pid for pid, _command in processes_inside(app_path)]
    except ValueError:
        return []


def refuse_if_open(output_app_path):
    """Raise OutputInUseError when anything is running from output_app_path.

    Called immediately before the old bundle at that path is moved aside, which
    is the one moment a running copy matters: once the new copy passes its
    checks the old one is deleted, and deleting a running bundle pulls its
    files out from under it.
    """
    open_pids = _open_from(output_app_path)
    if open_pids:
        raise OutputInUseError(
            f"HP Click is open from {output_app_path} (process "
            f"{', '.join(str(p) for p in open_pids)}). Quit it and build again; "
            f"nothing has been replaced.", output_app_path, open_pids)


# What a build hands back: where the copy is, and where the copy it replaced
# was set aside (None when there was nothing there). The caller decides what
# happens to that one -- agent.py deletes it once the new copy passes verify,
# and puts it back when it does not; `clickgraft build`, which does not verify,
# deletes it straight away.
Built = collections.namedtuple("Built", "output previous")


class InstallError(RuntimeError):
    """Putting the new copy in place, or the old one back, failed part way.

    .previous_copy says where that left the copy that was there before:
    "restored" (back at the output path, as it was) or "aside" (still set
    aside, at .previous_path, because putting it back failed too, or was
    refused).
    """

    def __init__(self, message, previous_copy, previous_path=None):
        super().__init__(message)
        self.previous_copy = previous_copy
        self.previous_path = previous_path


class NotTheBuildsCopyError(InstallError):
    """restore_previous was asked to make room at a path that holds something
    other than the copy the build that set this one aside put there. Nothing
    has been moved."""


class BuildInProgressError(RuntimeError):
    """Another ClickGraft process is building, checking or settling a copy in
    the same folder. Nothing has been moved or written when it is raised."""


class LeftoverPendingError(RuntimeError):
    """A copy an earlier build set aside at this output path is still waiting
    for the owner to say what happens to it (leftovers()). .leftovers lists
    them; nothing has been downloaded or written."""

    def __init__(self, message, output, pending):
        super().__init__(message)
        self.output = output
        self.leftovers = pending


# Folders this process holds ClickGraft's lock on, with the descriptor that
# holds each, and how many callers are inside it.
_HELD = {}


@contextlib.contextmanager
def folder_lock(folder):
    """One build, check or settle at a time in `folder`: BuildInProgressError
    if another ClickGraft process is in it.

    Two at once could interleave so that the second one's rollback put a copy
    that had failed its checks back over the owner's working one, and deleted
    that (found in review, 22 Sep 2026). It takes two wizards open at once, or
    a build run by hand beside one, but the cost is the owner's copy, so it is
    ruled out rather than argued about. agent.py holds it from before the build
    until the old copy is settled, since verify and the settle belong to the
    build; build_apple_silicon_bundle takes it too, for `clickgraft build`.
    Re-entrant within a process, so those two nest.

    An advisory flock on the folder itself: nothing is left behind in
    Applications, and the kernel drops it when the process ends, however it
    ends, so a crash never leaves a stale lock. A folder that cannot be opened
    or locked at all is let through: the build that follows says what is
    wrong with the folder, and refusing every build on a filesystem without
    locks would cost more than the race it guards against.
    """
    key = os.path.realpath(folder) if folder else ""
    held = _HELD.get(key)
    if held is not None:
        held[1] += 1
    else:
        fd = None
        try:
            fd = os.open(key, os.O_RDONLY)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if fd is not None:
                os.close(fd)
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise BuildInProgressError(
                    f"Another ClickGraft is making or checking a copy in {folder} "
                    f"right now. Wait for it to finish, then try again. Nothing "
                    f"here has been changed.") from None
            fd = None
        held = _HELD[key] = [fd, 1]
    try:
        yield
    finally:
        held[1] -= 1
        if held[1] == 0:
            del _HELD[key]
            if held[0] is not None:
                os.close(held[0])


# Up to 1.5.8 the last step deleted the old copy and then renamed the new one
# into place, and agent.py checked the new one only after that. A copy that
# failed its checks had already cost the owner the one that worked, and a
# rename that failed cost them both: nothing at all was left at the path
# (demonstrated 22 Sep 2026). Now the old copy is renamed aside instead, to a
# sibling in the same folder -- so the rename is atomic and costs no space --
# and only deleted once the new one is proven.
#
# The sibling's name has no .app extension, so Launch Services never
# registers it and Spotlight, Launchpad and "Open With" never offer it, and
# the leading dot keeps it out of Finder. It carries the pid and time of the
# build that made it, and the name it came from, so the next run can find it
# and say where it goes back to (leftovers()).
#
#   .clickgraft-previous-<pid>-<yyyymmdd-hhmmss>-<name>  the owner's copy, set
#       aside. Never deleted without the owner saying so.
#   .clickgraft-record-<pid>-<yyyymmdd-hhmmss>-<name>    beside it, a small
#       JSON file: which copy the build put at <name>.app in its place, and
#       what verify made of that one (_write_record).
#   .clickgraft-discard-<pid>-<yyyymmdd-hhmmss>-<name>   a copy on its way to
#       the bin: a new copy that failed its checks, or an old one the new one
#       has replaced. Renamed first so a slow or interrupted delete never
#       stands in the way; whatever is left is removed by the next build into
#       the same folder.
PREVIOUS = "previous"
DISCARD = "discard"
_SET_ASIDE = re.compile(r"^\.clickgraft-(previous|discard)-(\d+)-(\d{8}-\d{6})(?:\.\d+)?-(.+)$")
_RECORD = re.compile(r"^\.clickgraft-record-(\d+)-(\d{8}-\d{6})(?:\.\d+)?-(.+)$")
_PREVIOUS_PREFIX = ".clickgraft-previous-"
_RECORD_PREFIX = ".clickgraft-record-"


def _set_aside_path(output_app_path, kind):
    """A free sibling name for output_app_path, of kind PREVIOUS or DISCARD.

    A second one in the same second gets ".2" after the time, never after the
    name, which has to come back out intact.
    """
    folder, name = os.path.split(output_app_path)
    stem = name[:-4] if name.endswith(".app") else name
    stamp = f"{os.getpid()}-{time.strftime('%Y%m%d-%H%M%S')}"
    path, n = os.path.join(folder, f".clickgraft-{kind}-{stamp}-{stem}"), 1
    while os.path.lexists(path):
        n += 1
        path = os.path.join(folder, f".clickgraft-{kind}-{stamp}.{n}-{stem}")
    return path


# Why a record, and not just the two names: a leftover's name says where it
# goes back to, never what is there now. Up to the 1.5.9 review, putting one
# back renamed whatever was at that path to a discard name and deleted it.
# After two interrupted builds, with "Decide later" pressed in between, the
# first "Put it back" returned the owner's copy and the second deleted it to
# make room for the first build's unchecked one (demonstrated 22 Sep 2026).
# Now a copy is only removed to make room when it is the one that build put
# there, and the wizard can say what verify made of it.
#
# A directory is identified by its inode and creation time, which a rename
# within a folder keeps -- and a rename within a folder is all a copy set
# aside or put in place ever undergoes. Not the device number, which macOS can
# assign differently after a restart, and a leftover can wait that long.


def _record_path(previous):
    folder, name = os.path.split(os.path.normpath(previous or ""))
    if not name.startswith(_PREVIOUS_PREFIX):
        return None
    return os.path.join(folder, _RECORD_PREFIX + name[len(_PREVIOUS_PREFIX):])


def _identity(path):
    st = os.lstat(path)
    return [st.st_ino, getattr(st, "st_birthtime", 0.0)]


def _read_record(previous):
    p = _record_path(previous)
    if p is None:
        return None
    try:
        with open(p, encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def _write_record(previous, record):
    p = _record_path(previous)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f)
    os.replace(tmp, p)


def _drop_record(previous):
    p = _record_path(previous)
    if p is None:
        return
    try:
        os.remove(p)
    except OSError:
        pass


def record_outcome(previous, state, check=""):
    """Note on the record of a copy set aside what verify made of the one that
    replaced it: "passed", or "failed" with the check. Called before the copy
    is deleted or put back, so that if that step fails, the next run can say
    which it was (leftovers). Best effort: without it the record still says
    "installed", which the wizard words as a build that stopped mid-check.
    """
    rec = _read_record(previous)
    if rec is None:
        return
    rec.update(state=state, check=check or "")
    try:
        _write_record(previous, rec)
    except OSError:
        pass


def _leftover_state(path, restores_to):
    """-> (state, check) for a copy set aside at `path`.

    "missing"    nothing at restores_to: the build stopped between its renames
    "installed"  the copy that build put at restores_to is there, and was never
                 fully checked (the build was stopped, or `clickgraft build`,
                 which does not check, was)
    "passed"     that copy is there and passed; deleting this one failed
    "failed"     that copy is there and failed `check`; putting this one back
                 failed
    "other"      something else is at restores_to -- not the copy that build
                 put there, or there is no record to say -- so nothing may be
                 removed to make room for this one
    """
    if not os.path.lexists(restores_to):
        return "missing", ""
    rec = _read_record(path)
    try:
        here = _identity(restores_to)
    except OSError:
        return "other", ""
    if rec is None or rec.get("installed") != here:
        return "other", ""
    state = rec.get("state")
    if state not in ("installed", "passed", "failed"):
        state = "installed"
    return state, (rec.get("check") or "") if state == "failed" else ""


def _clickgraft_running(pid):
    """Is `pid` a ClickGraft process that is still running?

    A set-aside copy whose build is still going belongs to that build: it may
    be about to put it back, or delete it. Checked by command line as well as
    by pid, so a pid reused by something else does not hide a leftover forever.
    """
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    return "clickgraft" in out.lower()


def leftovers(folder, kind=PREVIOUS):
    """Copies set aside in `folder` by a build that is no longer running.

    [{"path", "restores_to", "pid", "set_aside" ("2026-09-22 14:30:05")}],
    newest first, and for PREVIOUS also "state" and "check" (_leftover_state).
    A build that finished, or failed and put things back, leaves none; one that
    crashed or was killed between putting the new copy in place and finishing
    its checks leaves the owner's previous copy here.

    Newest first because leftovers for one path stack: each was the copy at
    that path when the next build set it aside, so they can only be undone in
    the reverse order. A build refuses to start while one for its path is
    waiting (LeftoverPendingError), so since the 1.5.9 review there is at most
    one per path; the order is for anything made before that.
    """
    out = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return out
    for name in names:
        m = _SET_ASIDE.match(name)
        if not m or m.group(1) != kind:
            continue
        path = os.path.join(folder, name)
        if not os.path.isdir(path) or os.path.islink(path):
            continue
        pid = int(m.group(2))
        if _clickgraft_running(pid):
            continue
        when = time.strptime(m.group(3), "%Y%m%d-%H%M%S")
        item = {"path": path, "restores_to": os.path.join(folder, m.group(4) + ".app"),
                "pid": pid, "set_aside": time.strftime("%Y-%m-%d %H:%M:%S", when)}
        if kind == PREVIOUS:
            item["state"], item["check"] = _leftover_state(path, item["restores_to"])
        out.append(item)
    out.sort(key=lambda e: (e["set_aside"], e["path"]), reverse=True)
    return out


def is_set_aside(path, kind=PREVIOUS):
    """Is `path` named as a copy of `kind` that a build set aside?"""
    m = _SET_ASIDE.match(os.path.basename(os.path.normpath(path)))
    return bool(m) and m.group(1) == kind


def install_copy(staging_dir, output_app_path):
    """Put the finished copy at output_app_path. -> where the old one went, or None.

    The old copy is renamed aside, never deleted: that is the caller's to do
    once the new one is proven (discard_previous), or to undo if it is not
    (restore_previous). If the new copy cannot be renamed into place, the old
    one is renamed back before the error is raised, as InstallError.
    """
    previous = None
    if os.path.lexists(output_app_path):
        # Checked here, and not only in agent.py before the build: that check
        # runs about a minute earlier, the wizard tells the owner to quit the
        # copy on the screen before it, and nothing stops them opening it again
        # while the build runs -- perhaps to print something. `clickgraft
        # build` had no check at all. Nothing has moved when this raises.
        refuse_if_open(output_app_path)
        previous = _set_aside_path(output_app_path, PREVIOUS)
        # Before anything moves, so a copy set aside always has one. The
        # staging copy keeps its inode when it is renamed into place.
        _write_record(previous, {"installed": _identity(staging_dir),
                                 "state": "installed", "check": ""})
        try:
            os.rename(output_app_path, previous)
        except BaseException:
            _drop_record(previous)
            raise
    try:
        os.rename(staging_dir, output_app_path)
    except BaseException as exc:
        if previous is None:
            raise
        try:
            os.rename(previous, output_app_path)
        except OSError as undo:
            raise InstallError(
                f"The new copy could not be put at {output_app_path} ({exc}), and "
                f"the copy that was there could not be put back ({undo}). That "
                f"copy is safe, set aside at {previous}; rename it to "
                f"{os.path.basename(output_app_path)} to use it.",
                "aside", previous) from exc
        _drop_record(previous)
        if not isinstance(exc, Exception):
            raise
        raise InstallError(
            f"The new copy could not be put at {output_app_path}: {exc}. The copy "
            f"that was there has been put back as it was.", "restored") from exc
    return previous


def discard_previous(previous):
    """Delete a copy that was set aside. -> None when all of it is gone, else
    the path of what is left.

    Renamed to a discard name first, so that if the delete stops part way
    what is left is never mistaken for the owner's copy, and the next build
    into this folder finishes the job (sweep_discards). Raises OSError, with
    nothing moved, if even that rename fails.
    """
    if previous is None:
        return None
    if not os.path.lexists(previous):
        _drop_record(previous)
        return None
    folder, name = os.path.split(os.path.normpath(previous))
    m = _SET_ASIDE.match(name)
    target = (_set_aside_path(os.path.join(folder, m.group(4) + ".app"), DISCARD)
              if m else previous)
    if target != previous:
        os.rename(previous, target)
    _drop_record(previous)
    shutil.rmtree(target, ignore_errors=True)
    return target if os.path.lexists(target) else None


def restore_previous(output_app_path, previous):
    """Put the copy set aside at `previous` back at output_app_path.

    Whatever is at output_app_path now is renamed to a discard name first and
    deleted once the old one is back, so the swap is two renames and the slow
    part comes after -- but only when it is the copy the build that set this
    one aside put there (its record says). Anything else may be a copy the
    owner wants, so it is never removed to make room. -> True when a copy was
    removed from output_app_path.

    Raises, with nothing moved: OutputInUseError when the copy there is open,
    NotTheBuildsCopyError when it is not that build's, and InstallError
    "aside" when it cannot be moved out of the way. InstallError "aside" too
    when the copy set aside cannot be renamed back, after the one moved out of
    the way has been put back.
    """
    moved = None
    if os.path.lexists(output_app_path):
        refuse_if_open(output_app_path)
        rec = _read_record(previous)
        try:
            here = _identity(output_app_path)
        except OSError:
            here = None
        if rec is None or rec.get("installed") != here:
            name = os.path.basename(output_app_path)
            raise NotTheBuildsCopyError(
                f"The copy at {output_app_path} isn't the one ClickGraft put there "
                f"when it set this one aside, so ClickGraft won't remove it to make "
                f"room: it may be one you want. Nothing has been moved. To put the "
                f"set-aside copy back, first move {name} out of that folder "
                f"yourself (to the Trash, say), then try again.", "aside", previous)
        moved = _set_aside_path(output_app_path, DISCARD)
        try:
            os.rename(output_app_path, moved)
        except OSError as exc:
            raise InstallError(
                f"The copy at {output_app_path} could not be moved out of the way "
                f"({exc}), so the one set aside at {previous} has not been put "
                f"back. Nothing has been moved; it is safe where it is.",
                "aside", previous) from exc
    try:
        os.rename(previous, output_app_path)
    except OSError as exc:
        if moved is not None:
            try:
                os.rename(moved, output_app_path)
            except OSError:
                pass
        raise InstallError(
            f"The copy set aside at {previous} could not be put back at "
            f"{output_app_path}: {exc}. It is still there, safe.", "aside",
            previous) from exc
    _drop_record(previous)
    if moved is not None:
        shutil.rmtree(moved, ignore_errors=True)
    return moved is not None


def sweep_discards(folder):
    """Remove what earlier builds in `folder` meant to delete and did not
    finish, and records whose set-aside copy is gone."""
    for d in leftovers(folder, kind=DISCARD):
        shutil.rmtree(d["path"], ignore_errors=True)
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        m = _RECORD.match(name)
        if not m:
            continue
        tail = name[len(_RECORD_PREFIX):]
        if tail.endswith(".tmp"):
            tail = tail[:-4]
        if os.path.lexists(os.path.join(folder, _PREVIOUS_PREFIX + tail)) \
                or _clickgraft_running(int(m.group(1))):
            continue
        try:
            os.remove(os.path.join(folder, name))
        except OSError:
            pass


def _install_and_report(staging_dir, output_app_path, _log):
    """Step 11: put the copy in place, set the old one aside (install_copy),
    and say so. -> where the old one went, or None.

    Every message after install_copy is inside the try that puts the old copy
    back. They go to the caller's progress callback, which can fail -- the
    wizard gone and its pipe closed -- and a build that stops there has not
    finished, so it must leave the old copy where it was. The "set aside"
    message was once outside the try: a failure on it left the old copy set
    aside and the unchecked one in its place, reported as "untouched" (found in
    review, 22 Sep 2026).
    """
    _log("Finalizing application bundle...", 0.98)
    previous = install_copy(staging_dir, output_app_path)
    try:
        if previous:
            _log(f"The copy that was here is set aside at {previous} until the "
                 f"new one has passed its checks", 0.98)
        _log(f"BUILD COMPLETED SUCCESSFULLY! Native arm64 app written to: {output_app_path}", 1.0)
    except BaseException as exc:
        if previous:
            try:
                restore_previous(output_app_path, previous)
            except Exception as undo:                              # noqa: BLE001
                raise InstallError(
                    f"The build stopped after the new copy was put in place "
                    f"({exc!r}), and the copy that was there could not be put "
                    f"back: {undo}", "aside", previous) from exc
            if isinstance(exc, Exception):
                raise InstallError(
                    f"The build stopped after the new copy was put in place "
                    f"({exc!r}). The copy that was there has been put back "
                    f"as it was.", "restored") from exc
        raise
    return previous


def build_apple_silicon_bundle(
    source_app_path,
    output_app_path=None,
    manifest=None,
    preload=True,
    progress_callback=None
, allow_foreign_host=False):
    """
    Executes end-to-end build pipeline, holding ClickGraft's lock on the output
    folder throughout (folder_lock). See _build_apple_silicon_bundle.
    """
    output = os.path.abspath(output_app_path or os.path.join(
        os.path.dirname(os.path.abspath(source_app_path)), "HP Click (Apple Silicon).app"))
    with folder_lock(os.path.dirname(output)):
        return _build_apple_silicon_bundle(
            source_app_path, output, manifest=manifest, preload=preload,
            progress_callback=progress_callback, allow_foreign_host=allow_foreign_host)


def _build_apple_silicon_bundle(
    source_app_path,
    output_app_path=None,
    manifest=None,
    preload=True,
    progress_callback=None
, allow_foreign_host=False):
    """
    Executes end-to-end build pipeline.
    source_app_path: Path to existing HP Click.app
    output_app_path: Target path (default: alongside source, e.g. HP Click (Apple Silicon).app)
    manifest: Manifest dict (if None, looked up from manifests/ or probed)
    preload: True to include DYLD_INSERT_LIBRARIES in launcher script
    progress_callback: optional function(step_str, float_percentage)

    Returns Built(output, previous). `previous` is where the copy that was at
    the output path was set aside, or None if there was none; it is left for
    the caller to delete or put back (install_copy). Any failure before the new
    copy is in place leaves the old one untouched, and any failure after it
    puts the old one back, so a build that raises never leaves the output path
    without the copy that was there -- except as InstallError "aside", which
    says where it is. Refuses to start, as LeftoverPendingError, while a copy
    an earlier build set aside at the same path is waiting for the owner.
    """

    def _log(msg, pct=0.0):
        if progress_callback:
            progress_callback(msg, pct)

    # Before anything is fetched or written. Everything downstream is
    # arch-independent except the smoke launch, so an Intel Mac CAN produce a
    # correct arm64 copy for another machine — deliberately, not by accident.
    from clickgraft.hostarch import is_apple_silicon
    if not allow_foreign_host and not is_apple_silicon():
        raise ValueError(
            "This Mac has an Intel processor. ClickGraft's job is putting the "
            "Apple Silicon engine into a copy of HP Click, and that copy will "
            "not run here. Nothing has been downloaded or written. If you are "
            "building for a different Mac, that is supported - pass "
            "allow_foreign_host=True.")

    source_app_path = os.path.abspath(source_app_path)
    if not os.path.exists(source_app_path):
        raise ValueError(f"Source app path does not exist: {source_app_path}")

    # Determine default output_app_path if not specified
    if output_app_path is None:
        parent_dir = os.path.dirname(source_app_path)
        output_app_path = os.path.join(parent_dir, "HP Click (Apple Silicon).app")
    output_app_path = os.path.abspath(output_app_path)

    # Writable BEFORE anything is fetched, staged or written.
    #
    # Staging lives beside the output because the final step is os.rename(),
    # which cannot cross a filesystem — so if the output directory refuses a
    # write, the build cannot start at all. Without this check it started
    # anyway, downloaded the Electron runtime, and died at 20% on ditto's
    # "Permission denied": an error about a staging path the user never chose
    # and cannot act on. One reporter hit it nine times in a row.
    #
    # Probing by creating a directory, not os.access(): permission bits are not
    # the whole story on macOS, where App Management consent and MDM policy can
    # refuse a write that the mode says is fine.
    out_dir = os.path.dirname(output_app_path)
    if not os.path.isdir(out_dir):
        raise ValueError(
            f"There is no folder at {out_dir} to put the finished copy in. "
            f"Nothing has been downloaded or written.")
    _probe = os.path.join(out_dir, f".clickgraft-write-probe-{os.getpid()}")
    try:
        os.mkdir(_probe)
        os.rmdir(_probe)
    except OSError:
        raise ValueError(
            f"This account cannot write to {out_dir}, so the finished copy "
            f"cannot be put there. Nothing has been downloaded or written.\n\n"
            f"That folder usually needs an administrator account. Either sign "
            f"in as an administrator, or build into your own Applications "
            f"folder instead:\n\n"
            f"    {os.path.expanduser('~/Applications')}\n\n"
            f"A copy there works exactly the same, and appears in Launchpad "
            f"and Spotlight, but is available only to you.") from None

    # Not while a copy an earlier build set aside at this path is waiting for
    # the owner to say what happens to it. This build would set the copy now at
    # the path aside on top of it, and a second leftover for the same path is
    # one the owner can only undo in the right order, which nothing on screen
    # tells them (found in review, 22 Sep 2026). The wizard says so on Review,
    # before the button; this is the backstop, and all `clickgraft build` has.
    name = os.path.basename(output_app_path)
    pending = [item for item in leftovers(out_dir)
               if os.path.basename(item["restores_to"]) == name]
    if pending:
        raise LeftoverPendingError(
            f"A copy ClickGraft set aside the last time it replaced {name} is "
            f"still here, at {pending[0]['path']}: the build that set it aside "
            f"stopped before it had finished. ClickGraft won't replace {name} "
            f"again until you've decided what happens to that copy. Open "
            f"ClickGraft to decide, or do it yourself: to keep the copy that's at "
            f"{output_app_path} now, delete the set-aside one; to go back to the "
            f"set-aside one, move {name} out of the way and rename the set-aside "
            f"copy to {name}. Nothing has been downloaded or changed.",
            output_app_path, pending)

    # 1. Manifest lookup / validation
    _log("Validating manifest...", 0.05)
    if manifest is None:
        from clickgraft.manifest import ManifestManager
        mm = ManifestManager()
        archive = AsarArchive(os.path.join(source_app_path, "Contents", "Resources", "app.asar"))
        import hashlib
        with open(os.path.join(source_app_path, "Contents", "Resources", "app.asar"), "rb") as f:
            asar_hash = hashlib.sha256(f.read()).hexdigest()
        manifest = mm.find_manifest(asar_sha256=asar_hash)
        if not manifest:
            raise ValueError(f"No manifest found matching app.asar SHA-256 {asar_hash}. Use probe to draft a manifest.")
    else:
        from clickgraft.manifest import ManifestManager
        mm = ManifestManager()
        mm.validate_manifest(manifest)

    # Only allowlisted ops, however the manifest arrived, plus any local
    # never-distribute signatures (clickgraft/manifest_guard.py).
    from clickgraft.manifest_guard import check_manifest
    check_manifest(manifest)

    # The source MUST be stock: patches are anchored to exact strings in
    # specific minified files. Validate here regardless of how the manifest
    # arrived -- this check used to run only when manifest was None, so any
    # caller passing manifest= (the GUI always does) skipped it entirely and
    # got a 1.4 GB copy, an Electron download, and then a cryptic
    # "anchor occurred 0 times" failure minutes later.
    import hashlib
    source_asar = os.path.join(source_app_path, "Contents", "Resources", "app.asar")
    if not os.path.exists(source_asar):
        raise ValueError(f"Source bundle has no Contents/Resources/app.asar: {source_app_path}")
    with open(source_asar, "rb") as f:
        source_asar_hash = hashlib.sha256(f.read()).hexdigest()
    if source_asar_hash != manifest["asar_sha256"]:
        raise ValueError(
            f"Source is not a stock HP Click {manifest['app_version']} bundle.\n"
            f"  expected app.asar SHA-256: {manifest['asar_sha256']}\n"
            f"  this bundle's:             {source_asar_hash}\n"
            f"If this bundle has already been patched, choose your original, "
            f"untouched HP Click instead."
        )

    # 1b. The oldest macOS the copy will run on, before anything is downloaded
    # or written, so that a Mac too old for it hears so now rather than from a
    # copy that aborts at launch (clickgraft/macos_floor.py has the history).
    # Choosing the Homebrew bottles is a query to Homebrew's formula API, not a
    # download, and the floor depends on which ones are used.
    _log("Working out which macOS the copy will need...", 0.07)
    bottles = choose_bottles(manifest)
    floor_plan = plan_floor(source_app_path, manifest, bottles=bottles)
    # Only when the copy is for this Mac. An Intel Mac building for another one
    # says nothing about that Mac's macOS; the copy's own LSMinimumSystemVersion,
    # stamped at step 9c, is what speaks for it there.
    if is_apple_silicon():
        refuse_if_too_old(floor_plan["floor"], floor_reasons(floor_plan),
                          manifest.get("app_version"))

    electron_version = manifest["electron_version"]

    # 2. Fetch Electron runtime zip
    _log(f"Fetching/verifying Electron {electron_version} arm64 runtime...", 0.10)
    electron_zip = fetch_electron(electron_version)

    # 3. Create staging directory (non-.app name to prevent App Management locks)
    staging_dir = os.path.join(os.path.dirname(output_app_path), f"staging_clickgraft_{os.getpid()}")
    if os.path.exists(staging_dir):
        shutil.rmtree(staging_dir)
    # Copies an earlier build here meant to delete and did not finish: a
    # failed new copy, or an old one already replaced. Never the owner's
    # previous copy, which keeps a name of its own (install_copy).
    sweep_discards(os.path.dirname(output_app_path))

    _log("Staging source app copy...", 0.20)
    run_cmd(["ditto", source_app_path, staging_dir])

    try:
        # Extract Electron arm64 runtime using ditto to preserve macOS framework symlinks
        with tempfile.TemporaryDirectory() as tmp_dir:
            el_extract_dir = os.path.join(tmp_dir, "electron_extract")
            os.makedirs(el_extract_dir, exist_ok=True)
            _log("Extracting Electron arm64 runtime...", 0.30)
            run_cmd(["ditto", "-x", "-k", electron_zip, el_extract_dir])
            el_app = os.path.join(el_extract_dir, "Electron.app")

            # 4. Swap Runtime Frameworks & Binaries
            _log("Swapping Electron runtime frameworks and helper binaries...", 0.40)
            el_fw_dir = os.path.join(el_app, "Contents", "Frameworks")
            dst_fw_dir = os.path.join(staging_dir, "Contents", "Frameworks")
            for fw_name in os.listdir(el_fw_dir):
                if fw_name.endswith(".framework"):
                    src_fw = os.path.join(el_fw_dir, fw_name)
                    dst_fw = os.path.join(dst_fw_dir, fw_name)
                    if os.path.exists(dst_fw):
                        shutil.rmtree(dst_fw)
                    run_cmd(["ditto", src_fw, dst_fw])

            # Replace helper app executables while keeping HP Info.plist
            helpers = [
                ("HP Click Helper.app", "HP Click Helper"),
                ("HP Click Helper (GPU).app", "HP Click Helper (GPU)"),
                ("HP Click Helper (Plugin).app", "HP Click Helper (Plugin)"),
                ("HP Click Helper (Renderer).app", "HP Click Helper (Renderer)")
            ]
            for helper_app, helper_exe in helpers:
                src_exe = os.path.join(el_app, "Contents", "Frameworks", "Electron Helper.app", "Contents", "MacOS", "Electron Helper")
                dst_exe = os.path.join(staging_dir, "Contents", "Frameworks", helper_app, "Contents", "MacOS", helper_exe)
                if os.path.exists(dst_exe):
                    os.remove(dst_exe)
                shutil.copy2(src_exe, dst_exe)

            # Replace main executable
            src_main_exe = os.path.join(el_app, "Contents", "MacOS", "Electron")
            dst_hp_exe = os.path.join(staging_dir, "Contents", "MacOS", "HPClickExe")
            if os.path.exists(dst_hp_exe):
                os.remove(dst_hp_exe)
            shutil.copy2(src_main_exe, dst_hp_exe)
            os.chmod(dst_hp_exe, 0o755)

        # 5. Fetch/Bundle Required Dylibs
        _log("Bundling required dylibs...", 0.50)
        dst_lib_dir = os.path.join(staging_dir, "Contents", "Resources", "app", "appData", "macx", "lib")
        os.makedirs(dst_lib_dir, exist_ok=True)

        for dylib_info in manifest.get("required_dylibs", []):
            d_name = dylib_info["name"]
            got = resolve_dylib(dylib_info, floor=floor_plan["floor"],
                                bottle=bottles.get(dylib_info.get("brew_formula")))
            src_dylib = got["path"]
            where = {"homebrew": "this Mac's Homebrew",
                     "cache": f"cache, Homebrew bottle {got['bottle_tag']}",
                     "download": f"downloaded, Homebrew bottle {got['bottle_tag']}"}[got["source"]]
            _log(f"{d_name}: {where}, needs macOS "
                 f"{format_version(got['minos']) or 'unknown'}", 0.50)
            dst_dylib = os.path.join(dst_lib_dir, d_name)
            if os.path.exists(dst_dylib):
                os.remove(dst_dylib)
            shutil.copy2(src_dylib, dst_dylib)
            os.chmod(dst_dylib, 0o755)
            run_cmd(["install_name_tool", "-id", f"@rpath/{d_name}", dst_dylib])

        # Supply png_init_filter_functions_neon, which HP references and nobody
        # provides.
        #
        # The arm64 slice of DjCoreServicesNative-Electron.node has an undefined
        # flat-namespace reference to it; the x86_64 slice does not. HP never
        # trips over this because they ship an Intel Electron and never load the
        # arm64 slice. Grafting an arm64 runtime makes that code live, the call
        # binds to null, and the app dies with PC=0x0 the moment it decodes a
        # PNG. Confirmed from a real crash: EXC_BAD_ACCESS at 0x0 with LR inside
        # DjCoreServices, while importing PNGs.
        #
        # A no-op is correct rather than a fudge: libpng installs its portable C
        # filter implementations first and calls this only to override them with
        # NEON versions. Doing nothing leaves the C paths, which is exactly what
        # this build has always actually used -- the symbol was never resolvable.
        #
        # Compiled here rather than shipped as a binary: the toolchain is already
        # a hard requirement (preflight checks for it), and a 16KB .dylib in git
        # that nobody can diff is worse than four lines of C.
        shim_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shims", "pngshim.c")
        shim_name = "libclickgraft-pngshim.dylib"
        shim_dst = os.path.join(dst_lib_dir, shim_name)
        if os.path.exists(shim_src):
            _compile_pngshim(shim_src, shim_dst, shim_name, _log)
            os.chmod(shim_dst, 0o755)
            _log("Built libpng NEON shim (HP references a symbol nothing exports)", 0.55)
        else:
            raise FileNotFoundError(f"png shim source missing: {shim_src}")

        # Rewrite internal Homebrew paths inside bundled dylibs to @rpath
        for dylib_info in manifest.get("required_dylibs", []):
            dst_d = os.path.join(dst_lib_dir, dylib_info["name"])
            if os.path.exists(dst_d):
                otool_out = run_cmd(["otool", "-L", dst_d], check=False)
                for line in otool_out.splitlines()[1:]:
                    dep = line.strip().split()[0]
                    if dep.startswith("/opt/homebrew") or dep.startswith("/usr/local"):
                        dep_name = os.path.basename(dep)
                        run_cmd(["install_name_tool", "-change", dep, f"@rpath/{dep_name}", dst_d], check=False)

        # 6. Rewrite Qt5 install names to @rpath across native modules
        _log("Rewriting Qt5 install names to @rpath...", 0.60)
        qt_libs = ["libQt5Gui.5.dylib", "libQt5Network.5.dylib", "libQt5Xml.5.dylib", "libQt5Core.5.dylib"]
        for root, dirs, files in os.walk(os.path.join(staging_dir, "Contents", "Resources", "app")):
            for f in files:
                if f.endswith(".node") or f.endswith(".dylib"):
                    fp = os.path.join(root, f)
                    otool_out = run_cmd(["otool", "-L", fp], check=False)
                    for line in otool_out.splitlines()[1:]:
                        dep = line.strip().split()[0]
                        for qlib in qt_libs:
                            if dep.endswith(qlib) and not dep.startswith("@rpath/"):
                                run_cmd(["install_name_tool", "-change", dep, f"@rpath/{qlib}", fp], check=False)

        # 7. Update Bundle Identifiers in Info.plist files
        _log("Updating bundle identifiers...", 0.65)
        main_plist_p = os.path.join(staging_dir, "Contents", "Info.plist")
        if os.path.exists(main_plist_p):
            with open(main_plist_p, "rb") as pf:
                plist = plistlib.load(pf)
            plist["CFBundleIdentifier"] = "com.hp.hpclick.arm64"
            with open(main_plist_p, "wb") as pf:
                plistlib.dump(plist, pf)

        helpers_dir = os.path.join(staging_dir, "Contents", "Frameworks")
        for h in os.listdir(helpers_dir):
            if h.endswith(".app"):
                h_plist = os.path.join(helpers_dir, h, "Contents", "Info.plist")
                if os.path.exists(h_plist):
                    with open(h_plist, "rb") as pf:
                        h_p = plistlib.load(pf)
                    h_p["CFBundleIdentifier"] = "com.hp.hpclick.arm64.helper"
                    with open(h_plist, "wb") as pf:
                        plistlib.dump(h_p, pf)

        # 8. Patch ASAR
        _log("Patching app.asar...", 0.70)
        src_asar = os.path.join(source_app_path, "Contents", "Resources", "app.asar")
        dst_asar = os.path.join(staging_dir, "Contents", "Resources", "app.asar")

        patch_engine = PatchEngine(manifest["patches"])
        header_hash = patch_and_repack_asar(src_asar, dst_asar, patch_engine, manifest)

        # Update ElectronAsarIntegrity in Info.plist
        with open(main_plist_p, "rb") as pf:
            plist = plistlib.load(pf)
        plist["ElectronAsarIntegrity"] = {
            "Resources/app.asar": {
                "algorithm": "SHA256",
                "hash": header_hash
            }
        }
        with open(main_plist_p, "wb") as pf:
            plistlib.dump(plist, pf)

        # 9. Write Shell Launcher Script
        _log("Writing shell launcher script...", 0.80)
        launcher_path = os.path.join(staging_dir, "Contents", "MacOS", "HP Click")
        if os.path.exists(launcher_path):
            os.remove(launcher_path)

        preload_lines = ""
        if preload:
            preload_dylibs = []
            for dinfo in manifest.get("required_dylibs", []):
                if dinfo.get("preload") is True:
                    preload_dylibs.append(f"$APP_DATA_DIR/lib/{dinfo['name']}")
            # The shim must be inserted too, or the symbol stays unresolved.
            shim_path = os.path.join(dst_lib_dir, "libclickgraft-pngshim.dylib")
            if os.path.exists(shim_path):
                preload_dylibs.append("$APP_DATA_DIR/lib/libclickgraft-pngshim.dylib")
            if preload_dylibs:
                preload_str = ":".join(preload_dylibs)
                preload_lines = f'export DYLD_INSERT_LIBRARIES="{preload_str}"'

        launcher_script = f"""#!/bin/bash
DIR="$( cd "$( dirname "${{BASH_SOURCE[0]}}" )" && pwd )"
CONTENTS_DIR="$(dirname "$DIR")"
APP_DATA_DIR="$CONTENTS_DIR/Resources/app/appData/macx"

export DYLD_FRAMEWORK_PATH="$APP_DATA_DIR/Frameworks"
export DYLD_LIBRARY_PATH="$APP_DATA_DIR/lib"
{preload_lines}

exec "$DIR/HPClickExe" "$@"
"""
        with open(launcher_path, "w", encoding="utf-8") as lf:
            lf.write(launcher_script)
        os.chmod(launcher_path, 0o755)

        # 9b. Neutralise Squirrel's installer.
        #
        # HP ships Squirrel, whose ShipIt helper replaces the whole .app in
        # place. Pointed at a ClickGraft copy it would swap the arm64 build for
        # HP's Intel one, and any local patches on top of it, once the person
        # agreed to the restart HP's bar asks for. (It would probably fail
        # first: the copy's ad-hoc signature is not HP's, which Squirrel checks
        # before installing, and HP's own ShipIt dies on a missing
        # Mantle.framework. Probably is not a lock.)
        #
        # The manifest already stubs app-updater.js so startup() returns before
        # the updater is configured, which means nothing should ever reach this
        # binary. This is the second lock: even if a future build re-enables the
        # check, the step that overwrites the bundle cannot run.
        #
        # ONLY the ShipIt executable is replaced. Squirrel.framework's dylib is
        # linked by Electron Framework itself (@rpath/Squirrel.framework/Squirrel)
        # -- delete that and the app will not launch at all.
        _log("Disabling Squirrel's in-place installer...", 0.85)
        shipit_stub = (
            "#!/bin/sh\n"
            "# Replaced by ClickGraft. The real ShipIt overwrites the .app in place.\n"
            "logger -t ClickGraft \"blocked an auto-update: ShipIt was invoked in a patched copy\"\n"
            "echo \"ClickGraft: auto-update blocked. Installing HP's update here would\" >&2\n"
            "echo \"replace this patched arm64 copy with HP's Intel build.\" >&2\n"
            "exit 1\n"
        )
        shipit_count = 0
        squirrel_root = os.path.join(staging_dir, "Contents", "Frameworks", "Squirrel.framework")
        for root, _dirs, files in os.walk(squirrel_root):
            for fn in files:
                if fn != "ShipIt":
                    continue
                target = os.path.join(root, fn)
                if os.path.islink(target):
                    continue
                os.remove(target)
                with open(target, "w", encoding="utf-8") as sf:
                    sf.write(shipit_stub)
                os.chmod(target, 0o755)
                shipit_count += 1
        if shipit_count:
            _log(f"Squirrel installer disabled ({shipit_count} ShipIt binary replaced)", 0.88)

        # 9c. The copy's own minimum macOS: the highest any Mach-O in it
        # declares, and never lower than HP's. Stamped into the copy's
        # Info.plist, never HP's, before signing seals it, so that a Mac too old
        # for it gets macOS's own "requires macOS 15.0 or later" instead of a
        # crash at launch. Electron's runtime is counted here for the first
        # time, so this is checked against this Mac again.
        _log("Setting the oldest macOS the copy will run on...", 0.89)
        floor, reached_by = exact_floor(staging_dir, hp_declared=floor_plan["declared"])
        if is_apple_silicon():
            # Step 1b already refused a Mac older than HP's files and the
            # bottles, and a dylib newer than that floor is never accepted, so
            # a floor above it here can only have come from the engine.
            refuse_if_too_old(floor, floor_reasons(floor_plan)
                              if floor == floor_plan["floor"]
                              else ["the files of the Apple Silicon engine ClickGraft puts in the copy"],
                              manifest.get("app_version"), after_build=True)
        stamp_minimum(staging_dir, floor)
        _log(f"The copy needs macOS {format_version(floor)} or later"
             + (f" ({len(reached_by)} file(s) declare it, e.g. {reached_by[0][0]})"
                if reached_by else " (HP's own minimum)"), 0.89)

        # 10. Code Signing. A failed codesign raises here (signing.py), on the
        # staging copy, so nothing at the output path has been touched yet.
        _log("Signing application bundle inner-to-outer...", 0.90)
        for note in sign_bundle(staging_dir):
            _log(f"Signing note (optional step): {note}", 0.90)

        # 11. Put the staging copy in place, setting the old one aside rather
        # than deleting it (install_copy). The staging copy goes in the finally
        # block below if this fails, so nothing is left behind either.
        previous = _install_and_report(staging_dir, output_app_path, _log)

        return Built(output_app_path, previous)

    finally:
        if os.path.exists(staging_dir):
            shutil.rmtree(staging_dir, ignore_errors=True)


def _macos_sdks():
    """Every macOS SDK on the machine, newest first.

    Both toolchain locations, because a machine can have Command Line Tools,
    Xcode, or both, and the broken one is not always the one `xcode-select`
    points at.
    """
    import glob
    roots = [
        "/Library/Developer/CommandLineTools/SDKs/MacOSX*.sdk",
        "/Applications/Xcode*.app/Contents/Developer/Platforms/MacOSX.platform"
        "/Developer/SDKs/MacOSX*.sdk",
    ]
    found = []
    for pattern in roots:
        found.extend(glob.glob(pattern))

    def version_key(path):
        digits = "".join(c if c.isdigit() or c == "." else " "
                         for c in os.path.basename(path))
        parts = [int(n) for n in digits.split(".")[0].split() if n.isdigit()]
        return parts[0] if parts else -1

    return sorted(set(found), key=version_key, reverse=True)


def _salient_error_line(message, limit=110):
    """The most informative line of a captured stderr, for a failure list."""
    # 1. Ignore empty/whitespace-only lines.
    raw_lines = [line for line in message.splitlines() if line.strip()]
    if not raw_lines:
        return "(no output)"

    # 2. Ignore the leading `Command failed: ...` line and a bare `Stderr:` line.
    #    A line beginning `Stderr:` that has text after the colon keeps that text.
    cleaned_lines = []
    for i, line in enumerate(raw_lines):
        stripped = line.strip()
        if i == 0 and stripped.startswith("Command failed:"):
            continue
        if stripped == "Stderr:":
            continue
        if stripped.startswith("Stderr:"):
            after = stripped[len("Stderr:"):].strip()
            if not after:
                continue
            cleaned_lines.append(after)
        else:
            cleaned_lines.append(stripped)

    # 3. Ignore lines that are pure toolchain boilerplate, matched case-insensitively
    #    as a SUBSTRING so a prefixed line still matches:
    #        - `linker command failed with exit code`
    #        - `use -v to see invocation`
    #        - `error generated.`  and  `errors generated.`
    boilerplate = (
        "linker command failed with exit code",
        "use -v to see invocation",
        "error generated.",
        "errors generated.",
    )
    remaining_lines = [
        line for line in cleaned_lines
        if not any(bp in line.lower() for bp in boilerplate)
    ]

    # 4. From what remains, prefer the FIRST line containing any of, case-insensitive:
    #    `error:`, `ld:`, `tapi`, `fatal`, `cannot`, `no such`, `not found`.
    salient_keywords = (
        "error:",
        "ld:",
        "tapi",
        "fatal",
        "cannot",
        "no such",
        "not found",
    )
    selected = None
    for line in remaining_lines:
        lower = line.lower()
        if any(kw in lower for kw in salient_keywords):
            selected = line
            break

    # 5. If none match, take the first remaining line.
    if selected is None and remaining_lines:
        selected = remaining_lines[0]

    # 6. If nothing remains at all, return the last non-empty line of the original
    #    message; if the message has no non-empty line, return `"(no output)"`.
    if selected is None:
        if cleaned_lines:
            selected = cleaned_lines[-1]
        elif raw_lines:
            for line in reversed(raw_lines):
                s = line.strip()
                if s != "Stderr:":
                    selected = s
                    break
            if selected is None:
                selected = raw_lines[-1]
        else:
            return "(no output)"

    # 7. Collapse internal whitespace runs to a single space, strip, then truncate to
    #    `limit` characters. If truncated, the result must end with `…` (U+2026) and
    #    the total length must be exactly `limit`.
    collapsed = " ".join(selected.split())
    if limit <= 0:
        return ""
    if len(collapsed) > limit:
        return collapsed[:limit - 1] + "…"
    return collapsed


def _compile_pngshim(src, dst, name, log=None):
    """Build the shim, surviving a toolchain whose SDK its own linker can't read.

    The default SDK is tried first because it is right on almost every machine.
    When it is not, the failure is ugly and looks like ClickGraft's fault:

        ld: tapi error: malformed file
        .../MacOSX27.0.sdk/usr/lib/libSystem.B.tbd: error: unknown architecture
                           arm64e.x1-macos, arm64e.x1-maccatalyst ]

    That is an SDK newer than the linker being asked to parse it -- a
    half-updated Xcode or Command Line Tools. Reported from the field on
    10 Sep 2026, macOS 26.6 with a macOS 27 SDK.

    `-nostdlib` looks like the obvious escape, since this shim is a no-op that
    references nothing, but the linker refuses: "dynamic executables or dylibs
    must link with libSystem.dylib". So instead pick a different SDK. Machines
    carry several -- this one has five -- and an older one parses fine.
    """
    # -mmacosx-version-min, because clang's default deployment target is the
    # macOS it runs on: built on macOS 27 without it, this shim declared minos
    # 27.0 (measured 22 Sep 2026), and the copy's floor is the highest minimum
    # in it. 11.0 is the first macOS on Apple Silicon, and the shim calls
    # nothing, so it never raises the floor.
    base = ["clang", "-arch", "arm64", "-dynamiclib", "-O2",
            "-mmacosx-version-min=11.0",
            "-install_name", f"@rpath/{name}"]
    attempts = [(None, base + ["-o", dst, src])]
    for sdk in _macos_sdks():
        attempts.append((sdk, base + ["-isysroot", sdk, "-o", dst, src]))

    failures = []
    for sdk, cmd in attempts:
        try:
            run_cmd(cmd)
            if sdk is not None and log is not None:
                log(f"Default SDK unusable; built the shim against "
                    f"{os.path.basename(sdk)} instead", 0.55)
            return
        except RuntimeError as e:
            # CLT and Xcode ship SDKs with identical basenames, so name the
            # toolchain too or the list looks like it repeated itself.
            where = "Xcode" if "/Xcode" in (sdk or "") else "CLT"
            label = f"{where} {os.path.basename(sdk)}" if sdk else "default SDK"
            failures.append(f"  {label}: {_salient_error_line(str(e))}")

    raise RuntimeError(
        "Could not compile the PNG shim with any SDK on this Mac.\n\n"
        "This is a broken developer toolchain rather than a problem with your "
        "HP Click. It usually means Xcode and the Command Line Tools are at "
        "different versions, so the linker cannot read its own SDK.\n\n"
        "Try:  sudo rm -rf /Library/Developer/CommandLineTools\n"
        "      sudo xcode-select --install\n\n"
        "and if you have Xcode installed, open it once so it finishes setting "
        "up. Tried " + str(len(attempts)) + " SDK(s):\n" + "\n".join(failures))
