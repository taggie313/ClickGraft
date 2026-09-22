"""
clickgraft.agent — a line-oriented JSON interface to the backend.

The Swift front end drives ClickGraft by spawning python3 and reading JSON,
rather than reimplementing any of the build logic. One JSON object per line on
stdout; nothing else is ever printed there.

    python3 -m clickgraft.cli agent env
    python3 -m clickgraft.cli agent plan  --source PATH [--out PATH]
    python3 -m clickgraft.cli agent build --source PATH [--out PATH] [--accept-printer-loss]
                                          [--expect-replacing TOKEN]
    python3 -m clickgraft.cli agent probe --source PATH
    python3 -m clickgraft.cli agent printerinfo
    python3 -m clickgraft.cli agent restore-previous --backup PATH
    python3 -m clickgraft.cli agent discard-previous --backup PATH

`build` streams:
    {"type":"progress","pct":0.4,"msg":"..."}
    ...
    {"type":"done","results":{...}}      or {"type":"error","error":"..."}

An error carries a "stage" when the wizard has something specific to say:
"in_use" and "printers_lost" (the copy it would replace), "replacement_changed"
(the copy at the output path is not the one Review showed, or changed during
the build; see below), "macos_too_old" (this Mac is older than the copy needs;
with "needs" and "this_mac"), "leftover_pending" (a copy an earlier build set
aside at this path is waiting for the owner; with "leftovers"), "busy"
(another ClickGraft is working in the same folder), "build" and "verify".

`plan` carries "replacing_token": an opaque string for what is at the output
path now -- which copy, its version, and the printers replacing it would lose
-- or for nothing being there. Review passes it back as `build
--expect-replacing TOKEN`, and the build refuses, as "replacement_changed",
when the output path no longer holds what Review showed, so the printer-loss
tick and the "Replacing" line mean the copy they were shown for. Without the
flag, as from `clickgraft build`, the build pins what is there when it starts.
Either way the same check runs again just before the install. The error has:

    change        "removed"   a copy was there, and is gone
                  "appeared"  nothing was there, and a copy is now
                  "changed"   a different copy, or the same one edited
    during_build  true when found just before the install, false when found
                  before anything was downloaded
    previous_copy "gone" for "removed": nothing is at the output path, and
                  nothing was put there; "untouched" otherwise: what is there
                  now has been left alone

Since 1.5.9 the copy a build replaces is set aside, not deleted, until the new
one has passed verify (build.install_copy), and every "done" and "error" from
`build` says what became of it, so the wizard never has to guess:

    previous_copy  "none"       nothing was at the output path
                   "untouched"  the build stopped before it moved anything
                   "gone"       (replacement_changed only) the copy that was
                                there was removed by something else
                   "restored"   it was set aside, and has been put back
                   "aside"      it was set aside and could not be put back;
                                it is safe at previous_path
                   "replaced"   (done only) the new copy passed and it is gone
                   "aside" on a done: the new copy passed, and the old one could
                   not be deleted; the next `agent env` offers it
    needs_macos    (done) the copy's own LSMinimumSystemVersion, "15.0"
    new_copy       (verify errors) "removed" to make room for the previous
                   copy, or "kept" at the output path
    check          (verify errors) which check failed: a VerifyError.check
    restore_reason (verify errors, previous_copy "aside") "open" when the new
                   copy was open, else "error"; restore_error has the detail
"""
import hashlib
import io
import json
import os
import shutil
import stat
import sys
import time

from clickgraft import graftable
from clickgraft.build import (BuildInProgressError, InstallError, LeftoverPendingError,
                              OutputInUseError, build_apple_silicon_bundle, discard_previous,
                              folder_lock, is_set_aside, leftovers, record_outcome,
                              restore_previous)
from clickgraft.deps import check_clt
from clickgraft.hostarch import host_info
from clickgraft.macos_floor import (MacOSTooOldError, declared_minimum, floor_reasons,
                                    format_version, host_macos, parse_version, plan_floor,
                                    too_old_message)
from clickgraft.macho import get_archs
from clickgraft.manifest import ManifestManager
from clickgraft.printerinfo import as_text as printer_text
from clickgraft.probe import probe_app_bundle
from clickgraft.verify import VerifyError, processes_inside, verify_app_bundle

# vtool since 1.5.9: it reads the minimum macOS each file in a copy declares
# (clickgraft/macos_floor.py). Listed here so the Requirements screen's detail,
# and test_toolchain_fallback's run on the Command Line Tools alone, cover it.
REQUIRED_TOOLS = ["codesign", "install_name_tool", "lipo", "otool", "nm", "vtool", "ditto"]
APP_NAME = "HP Click (Apple Silicon).app"
# What build.py step 7 sets on every copy, and nothing of HP's uses.
COPY_BUNDLE_ID = "com.hp.hpclick.arm64"
SYSTEM_APPS = "/Applications"
USER_APPS = os.path.expanduser("~/Applications")


def _can_create_here(directory):
    """Can this account actually create something in `directory`?

    Not os.access(): that answers from the permission bits, and on macOS a
    directory can be mode-writable and still refuse the write — App Management
    consent, an MDM policy, a managed volume. The only honest test of "may I
    create a directory here" is creating one and removing it again.
    """
    probe = os.path.join(directory, f".clickgraft-write-probe-{os.getpid()}")
    try:
        os.mkdir(probe)
    except OSError:
        return False
    try:
        os.rmdir(probe)
    except OSError:                                          # pragma: no cover
        pass
    return True


def resolve_output():
    """Where the patched copy goes -> (path, is_per_user).

    /Applications belongs to admin accounts. A standard user, or anyone on a
    managed Mac, gets no warning about that: the build ran for twenty percent
    and then died on `ditto: Permission denied`, which one reporter hit nine
    times in a row before giving up. ~/Applications is a real macOS location
    that Launchpad and Spotlight index like any other, and any account can
    write to it, so fall back there rather than fail.

    When neither works, hand back the system path anyway. build() checks the
    directory before it fetches anything and will say so precisely; inventing a
    third location here would only move the failure somewhere less expected.
    """
    if _can_create_here(SYSTEM_APPS):
        return os.path.join(SYSTEM_APPS, APP_NAME), False
    try:
        os.makedirs(USER_APPS, exist_ok=True)
    except OSError:
        return os.path.join(SYSTEM_APPS, APP_NAME), False
    if _can_create_here(USER_APPS):
        return os.path.join(USER_APPS, APP_NAME), True
    return os.path.join(SYSTEM_APPS, APP_NAME), False


# The stdout a write has already failed on, because nobody is reading it.
_unread = None


def emit(obj):
    """One JSON line to the wizard. A closed pipe is not an error.

    Quitting the wizard does not stop this process: the wizard never
    terminates it, and it should not, since a build killed between putting the
    new copy in place and settling the old one leaves the old one stranded.
    So the build carries on to the end with nobody reading, and every write
    after the wizard has gone raised BrokenPipeError. Raised from a progress
    message, that skipped the step after it -- once, the deletion of the copy a
    new one had just replaced after passing every check (found in review, 22
    Sep 2026). Now the rest of the output goes to /dev/null, so the build
    settles exactly as it would have with the wizard watching; the log file
    still has every line.
    """
    global _unread
    if _unread is sys.stdout:
        return
    try:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()
    except BrokenPipeError:
        _unread = sys.stdout
        # Or the interpreter's own flush at exit reports the same error.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError, AttributeError, io.UnsupportedOperation):
            pass


def _log_path():
    d = os.path.expanduser("~/Library/Logs/clickgraft")
    try:
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, time.strftime("clickgraft-%Y%m%d-%H%M%S.log"))
    except OSError:
        return None


def _previous_copies(folders):
    """Copies set aside by a build that did not finish, for `agent env`.

    A build that crashed or was killed after putting the new copy in place,
    and before verify passed, leaves the owner's previous copy set aside
    (build.install_copy). Nothing deletes it: the wizard says it is there and
    offers to put it back or remove it, and does neither on its own.
    """
    out, seen = [], set()
    for folder in folders:
        folder = os.path.realpath(folder) if folder else ""
        if not folder or folder in seen:
            continue
        seen.add(folder)
        out += _describe_leftovers(leftovers(folder))
    return out


def _describe_leftovers(items):
    """build.leftovers() entries, with what the wizard says about each.

    "state" (build._leftover_state) is what the leftover screen is worded
    from: whether the copy at the path is the one that build put there, and
    what verify made of it. Before the 1.5.9 review the screen said "never
    fully checked" of every copy it found there, including ones that had
    passed and ones that had failed.
    """
    out = []
    for item in items:
        item = dict(item)
        info = _info_plist(item["path"])
        there = item["restores_to"]
        item.update({
            "version": _bundle_version(item["path"]),
            "made_by_clickgraft": info.get("CFBundleIdentifier") == COPY_BUNDLE_ID,
            "restores_to_exists": os.path.lexists(there),
            "current_version": _bundle_version(there) if os.path.isdir(there) else "",
        })
        out.append(item)
    return out


def environment(mm):
    h = host_info()
    return {
        "clt": check_clt(),
        "apple_silicon": h["apple_silicon"],
        "host": h,
        "tools": {t: (shutil.which(t) or "") for t in REQUIRED_TOOLS},
        "versions": sorted(mm.manifests),
        # This Mac's macOS, e.g. "15.6.1"; "" if unreadable. The plan's
        # macos_floor says what a copy needs.
        "macos": format_version(host_macos()) or "",
    }


def candidates(mm):
    """Every HP Click bundle found, with enough detail for the user to choose.

    Unsupported ones are returned too, with a reason — an already-patched
    bundle silently accepted as a source fails deep inside the build with an
    anchor error, which is a terrible way to learn you picked the wrong app.
    """
    out = []
    base = "/Applications"
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".app") or "click" not in name.lower():
            continue
        path = os.path.join(base, name)
        asar = os.path.join(path, "Contents", "Resources", "app.asar")
        if not os.path.exists(asar):
            continue
        try:
            with open(asar, "rb") as f:
                sha = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            continue
        exe = os.path.join(path, "Contents", "MacOS", "HPClickExe")
        archs = get_archs(exe) if os.path.exists(exe) else []
        m = mm.find_manifest(asar_sha256=sha)
        # An arm64-only slice can only have got that way through ClickGraft;
        # HP ships x86_64. Distinguishing the two rejections matters to the UI:
        # only an unsupported *version* is worth filing a report about.
        already = "arm64" in archs and "x86_64" not in archs
        reason = "" if m else ("already_copy" if already else "unsupported")
        # HP's own Apple Silicon build (4.11.31 on). Nothing to graft, and
        # calling it "unknown" would invite a report about an app that is fine.
        if reason == "unsupported" and graftable.hp_native(path):
            reason = "hp_native"
        # Unknown, or unknowable? Only an unknown version is worth a report; one
        # whose own code has no arm64 in it needs a different HP Click instead.
        blocked = graftable.blockers(path) if reason == "unsupported" else []
        if blocked:
            reason = "cannot_graft"
        version = (m or {}).get("app_version", "") or _bundle_version(path)
        entry = {
            "path": path,
            "name": name,
            "archs": archs,
            "sha256": sha,
            "version": version,
            "usable": bool(m),
            "reason": reason,
            "why": {
                "": "",
                "already_copy": "This one was already made by ClickGraft. "
                                "Choose your original instead.",
                "unsupported": "ClickGraft doesn't know this version yet",
                "cannot_graft": "Too old for any version of ClickGraft",
                "hp_native": "Already runs natively on Apple Silicon. No copy needed",
            }[reason],
        }
        if reason == "hp_native":
            entry["reference_version"] = graftable.REFERENCE_VERSION
            entry["printers_dropped"] = graftable.printers_dropped_since_reference(path)
        if blocked:
            entry["blockers"] = blocked
            entry["reference_version"] = graftable.REFERENCE_VERSION
            entry["printers_lost"] = graftable.printers_lost_by_moving(path)
        out.append(entry)
    return out


def _info_plist(path):
    """The bundle's Contents/Info.plist as a dict; {} if it can't be read."""
    import plistlib
    try:
        with open(os.path.join(path, "Contents", "Info.plist"), "rb") as f:
            p = plistlib.load(f)
        return p if isinstance(p, dict) else {}
    except Exception:
        return {}


def _bundle_version(path):
    """CFBundleShortVersionString, for apps with no manifest to name them."""
    v = _info_plist(path).get("CFBundleShortVersionString", "")
    return v if isinstance(v, str) else ""


def existing_copy(output, source, ps_output=None):
    """What Review has to say about the copy a build would replace, or None.

    The copy's name is fixed (APP_NAME), so each build replaces the one before,
    and Review used to say only "Replacing". A T-series owner whose stock 4.8.117
    had auto-updated to 4.8.118 would have swapped a copy that prints to their
    plotter for one that can't, with nothing on screen to say so.

    The version comes from the copy's Info.plist. build.py rewrites that file
    but only to change CFBundleIdentifier and ElectronAsarIntegrity, so
    CFBundleShortVersionString is still HP's. Not from the archive's
    package.json: asar.py's fake_version, which the old repack script offered
    as --fake-version, rewrites that "version" to 99.99.999. Nothing calls it
    today, but a copy made that way would name a version that never existed.

    open_pids: processes running from the copy. build.py sets the old copy
    aside and deletes it once the new one passes its checks, so it must not be
    open. ps_output is for tests.
    """
    if not output or not os.path.isdir(output):
        return None
    info = _info_plist(output)
    version = info.get("CFBundleShortVersionString", "")
    try:
        running = processes_inside(output, ps_output)
    except ValueError:              # not an .app path; nothing can be matched to it
        running = []
    return {
        "path": output,
        "version": version if isinstance(version, str) else "",
        "made_by_clickgraft": info.get("CFBundleIdentifier") == COPY_BUNDLE_ID,
        "source_version": _bundle_version(source) if source else "",
        "printers_lost": graftable.printers_lost_by_replacing(output, source) if source else None,
        "open_pids": [pid for pid, _command in running],
    }


class ReplacementChangedError(ValueError):
    """The output path does not hold what the build was going to replace.

    .change is "removed", "appeared" or "changed" (see the module docstring).
    Raised before anything at the output path has been moved.
    """

    def __init__(self, message, change):
        super().__init__(message)
        self.change = change


def _replacement_facts(output, source):
    """What replacing_token is made from, or None when nothing is at output.

    The directory's inode and creation time say which copy it is: a rename
    keeps both, a copy put in its place has others. Not the device number,
    for the reason build._identity gives. Info.plist's bytes and existing_copy's
    version and printer list say what it is, so an edit in place is seen too
    (the 22 Sep 2026 review changed CFBundleShortVersionString in place, same
    inode, mid-build). open_pids is left out: whether it is open is checked
    separately, and quitting it must not make it a different copy. So is the
    path, which the plan and the build may spell differently.
    """
    try:
        st = os.lstat(output)
    except (FileNotFoundError, NotADirectoryError):
        return None
    try:
        with open(os.path.join(output, "Contents", "Info.plist"), "rb") as f:
            plist = hashlib.sha256(f.read()).hexdigest()
    except OSError:
        plist = None
    copy = existing_copy(output, source)
    if copy:
        copy = {k: v for k, v in copy.items() if k not in ("open_pids", "path")}
    return {"ino": st.st_ino, "born": getattr(st, "st_birthtime", 0.0),
            "mode": stat.S_IFMT(st.st_mode), "info_plist": plist, "copy": copy}


def replacement_token(output, source):
    """plan's "replacing_token": an opaque name for what is at output now.

    Review's consent -- the "Replacing" line, and the printer-loss tick --
    was a boolean passed to a build that started later, so a copy changed
    between Review and pressing the button was replaced on the strength of a
    tick given for another (found in review, 22 Sep 2026). The token lets the
    build check it is replacing what Review showed.
    """
    return _token_of(_replacement_facts(output, source))


def _token_of(facts):
    blob = json.dumps(facts, sort_keys=True, separators=(",", ":"))
    return "cg1-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:40]


# The token for an empty output path, which is how a mismatch is told apart
# as a copy that appeared rather than one that changed.
_NOTHING_TOKEN = _token_of(None)


def _replacement_change(output, had_copy):
    """-> "removed", "appeared" or "changed", for a token that no longer matches.

    had_copy is whether something was there when the expected token was made.
    """
    there = os.path.lexists(output)
    if had_copy and not there:
        return "removed"
    if not had_copy and there:
        return "appeared"
    return "changed"


_CHANGE_SINCE_REVIEW = {
    "removed": "The copy at {out} has gone since you reviewed it, so there is "
               "nothing there for ClickGraft to replace.",
    "appeared": "A copy has appeared at {out} since you reviewed it. ClickGraft "
                "won't replace a copy you haven't seen on Review, so it has left "
                "it alone.",
    "changed": "The copy at {out} has changed since you reviewed it. ClickGraft "
               "won't replace a copy you haven't seen on Review, so it has left "
               "it alone.",
}
_CHANGE_DURING_BUILD = {
    "removed": "The copy at {out} was removed while the new one was being made. "
               "ClickGraft has not put the new one there in its place, and has "
               "thrown it away.",
    "appeared": "A copy appeared at {out} while the new one was being made. "
                "ClickGraft has left it alone and thrown the new one away.",
    "changed": "The copy at {out} changed while the new one was being made, so "
               "it is not the copy this build was going to replace. ClickGraft "
               "has left it alone and thrown the new one away.",
}


def _first_sentence(text):
    import re
    m = re.match(r"^[\s\S]*?[.!?](?=\s|$)", text or "")
    return m.group(0) if m else (text or "")


# Which of Review's "small fixes" each patched file stands for. One point per
# HP problem, not per file: on 4.8.x constants.js and industries.js carry the
# same repair. Read from the manifest so a point appears only where its patch
# is applied -- up to 1.5.7 the screen was hard-coded, and told 4.10.42 users
# it fixed a startup error their index.html cannot raise, because it never
# loads constants.js as a script. tests/test_review_replace.py checks that every
# op manifest_guard allows has an entry here and a sentence in ClickGraft.swift.
FIX_FOR_PATH = {
    "app/node/main/app-updater.js": "updater",
    "app/package.json": "crash_reports",
    "app/bundle.js": "snmp_log",
    "app/shared/constants.js": "startup_error",
    "app/shared/industries.js": "startup_error",
}


def fixes_for(manifest):
    """Review's small-fix ids for this manifest, in manifest order, each once."""
    out = []
    for p in manifest.get("patches", []):
        fix = FIX_FOR_PATH.get(p.get("path"))
        if fix and fix not in out:
            out.append(fix)
    return out


# HP's own Apple Silicon build, which needs no copy. Offered when this Mac is
# too old for one: HP's download page lists 4.11.31 for macOS 12 to 26, and
# its Info.plist says LSMinimumSystemVersion 12.0 (both read 22 Sep 2026; it
# runs on 27 too, which HP's list doesn't have yet). It carries the same
# minos-15.0 libmagic and OpenSSL as 4.10.42 under that 12.0, so this is HP's
# word for it, and the wizard says so -- in HP's words, "12 to 26", not "12 and
# later". It lacks the DesignJet T310/T320/T350/T720/T750, like every HP Click
# after 4.8.117. packaging/ClickGraft.swift says the same on its Requirements
# screen, where the backend has not run yet.
HP_NATIVE_VERSION = "4.11.31"
HP_NATIVE_LISTED_FROM = (12, 0, 0)
HP_NATIVE_LISTED_TO = (26, 0, 0)


def native_alternative(this_mac):
    """{"version", "hp_lists_from", "hp_lists_to"} when HP's own build is an
    option for this Mac; None when this Mac is older than HP lists it for, or
    unreadable."""
    if this_mac is None or this_mac < HP_NATIVE_LISTED_FROM:
        return None
    return {"version": HP_NATIVE_VERSION,
            "hp_lists_from": format_version(HP_NATIVE_LISTED_FROM),
            "hp_lists_to": format_version(HP_NATIVE_LISTED_TO)}


def macos_floor_plan(manifest, source, floor=None):
    """What Review can say about macOS before anything is downloaded.

    The copy's minimum macOS is the highest any file in it declares, and never
    lower than HP's own (clickgraft/macos_floor.py). build.py refuses to start
    when this Mac is older, and this is the same arithmetic done first, so the
    wizard can say so on Review rather than after the button.

        needs        "15.0": the copy's minimum as far as it can be known now.
                     Electron's runtime is not downloaded yet and so not
                     counted; the build counts it and stamps the exact value
                     (the same 15.0 for 4.8.117, 4.8.118 and 4.10.42 on
                     22 Sep 2026).
        this_mac     "27.0", or "" if unreadable
        for_this_mac False on an Intel Mac, where the copy is for another Mac
                     and this Mac's macOS says nothing about it
        this_mac_ok  True/False; None when unknown or not for_this_mac
        message      the refusal build would give, when this_mac_ok is False
        reasons      what sets it, as phrases ("files HP ships inside HP Click
                     itself", "the support files ClickGraft adds from Homebrew")
        hp_declares  HP's own LSMinimumSystemVersion, "12.0"
        hp_needs, hp_file   HP's highest-minimum file that the copy keeps
        bottles      {formula: {"tag": "arm64_sequoia", "needs": "15.0"}}
        complete     False when Homebrew could not be asked, in which case
                     needs leaves the Homebrew files out and problem says why
        alternative  when this_mac_ok is False: native_alternative(), HP's own
                     Apple Silicon version if HP lists it for this Mac
    """
    plan = floor or plan_floor(source, manifest)
    this_mac = host_macos()
    for_this_mac = bool(host_info()["apple_silicon"])
    need = plan["floor"]
    ok = None
    if for_this_mac and need is not None and this_mac is not None:
        ok = this_mac >= need
    reasons = floor_reasons(plan)
    return {
        "needs": format_version(need) or "",
        "this_mac": format_version(this_mac) or "",
        "for_this_mac": for_this_mac,
        "this_mac_ok": ok,
        "message": (too_old_message(need, this_mac, reasons, manifest.get("app_version"))
                    if ok is False else ""),
        "reasons": reasons,
        "hp_declares": format_version(plan["declared"]) or "",
        "hp_needs": format_version(plan["hp"][1]) if plan["hp"] else "",
        "hp_file": plan["hp"][0] if plan["hp"] else "",
        "bottles": {f: {"tag": b["tag"], "needs": format_version(b["macos"])}
                    for f, b in plan["bottles"].items()},
        "complete": plan["complete"],
        "problem": plan["problem"],
        "alternative": native_alternative(this_mac) if ok is False else None,
    }


def build_plan(manifest, source, output):
    floor = macos_floor_plan(manifest, source)

    def download_line(d):
        b = floor["bottles"].get(d.get("brew_formula"))
        bottle = (f" ({b['tag']} bottle, for macOS {b['needs']} and later)"
                  if b else "")
        return f"{d['name']} from Homebrew's CDN{bottle}, SHA-256 checked"

    return {
        "source": source,
        "output": output,
        "app_version": manifest["app_version"],
        "electron": manifest["electron_version"],
        "fixes": fixes_for(manifest),
        # None when nothing is there yet. Otherwise what is being replaced:
        # its version, whether it is open, and any printers the new copy drops.
        "replacing": existing_copy(output, source),
        # What the Create button's consent is for: passed back as
        # `build --expect-replacing` (replacement_token).
        "replacing_token": replacement_token(output, source),
        "patches": [{"path": p["path"], "why": _first_sentence(p.get("why", ""))}
                    for p in manifest["patches"]],
        "dylibs": [{"name": d["name"], "preload": bool(d.get("preload")),
                    "why": _first_sentence(d.get("why", ""))}
                   for d in manifest.get("required_dylibs", [])],
        # A pinned manifest never fetches SHASUMS256.txt (deps.fetch_electron);
        # until the 22 Sep 2026 review this line said it did.
        "downloads": [f"Electron {manifest['electron_version']} (darwin-arm64), "
                      + ("SHA-256 checked against the value pinned in this "
                         "ClickGraft (from the release's SHASUMS256.txt)"
                         if manifest.get("electron_sha256") else
                         "SHA-256 checked against the release's SHASUMS256.txt")]
        + [download_line(d) for d in manifest.get("required_dylibs", [])],
        "macos_floor": floor,
        # A copy an earlier build set aside at this path and never put back or
        # deleted. The build refuses until the owner has decided
        # (build.LeftoverPendingError), so Review says so before the button.
        "leftover": _pending_leftover(output),
    }


def _pending_leftover(output):
    """The leftover waiting at `output`, described as `agent env` does, or None."""
    if not output:
        return None
    name = os.path.basename(output)
    items = [i for i in leftovers(os.path.dirname(os.path.abspath(output)))
             if os.path.basename(i["restores_to"]) == name]
    return _describe_leftovers(items)[0] if items else None


def _manifest_for(mm, source):
    asar = os.path.join(source, "Contents", "Resources", "app.asar")
    if not os.path.exists(asar):
        return None
    with open(asar, "rb") as f:
        return mm.find_manifest(asar_sha256=hashlib.sha256(f.read()).hexdigest())


def main(argv):
    if not argv:
        emit({"type": "error", "error": "no agent subcommand"})
        return 2
    cmd, rest = argv[0], argv[1:]

    def arg(flag, default=None):
        return rest[rest.index(flag) + 1] if flag in rest else default

    mm = ManifestManager()
    source = arg("--source")
    default_out, out_per_user = resolve_output()
    output = arg("--out") or default_out

    if cmd == "env":
        emit({"type": "env", "env": environment(mm), "candidates": candidates(mm),
              "default_output": default_out,
              # The interface has to be able to say WHY the path is unusual.
              # A copy appearing in the home folder instead of /Applications,
              # with no explanation, reads as the tool putting it in the wrong
              # place rather than the only place this account may write.
              "output_per_user": out_per_user,
              # 1.5.9: a previous copy a build set aside and never got to put
              # back or delete, because it crashed or was killed mid-verify.
              "leftovers": _previous_copies([os.path.dirname(default_out),
                                             SYSTEM_APPS, USER_APPS])})
        return 0

    if cmd in ("restore-previous", "discard-previous"):
        return _settle_leftover(cmd, arg("--backup") or "")

    if cmd == "plan":
        m = _manifest_for(mm, source or "")
        if not m:
            emit({"type": "error", "error": "That is not a supported HP Click version."})
            return 1
        emit({"type": "plan", "plan": build_plan(m, source, output)})
        return 0

    if cmd == "printerinfo":
        # Only ever called when the user has ticked the box. Returns an
        # allowlisted subset — see clickgraft/printerinfo.py for why it must
        # never become a denylist.
        emit({"type": "printerinfo", "text": printer_text()})
        return 0

    if cmd == "probe":
        try:
            _draft, report = probe_app_bundle(source)
            emit({"type": "probe", "report": report})
            return 0
        except Exception as exc:                                   # noqa: BLE001
            emit({"type": "error", "error": str(exc)})
            return 1

    if cmd == "build":
        return run_build(source, output, rest)

    emit({"type": "error", "error": f"unknown agent subcommand: {cmd}"})
    return 2


def run_build(source, output, options=(), emit_event=None):
    """The build, verify and settle that both the wizard (`agent build`) and
    `clickgraft build` run, under the output folder's lock.

    Success always means verification finished and the replacement was
    settled. Options and event fields mean the same for both: options are
    `agent build`'s flags, and emit_event receives each event the wizard would
    (emit, by default). Up to 1.5.9 `clickgraft build` had its own copy of
    this that did not verify, and deleted the copy it replaced as soon as the
    build finished.
    """
    send = emit_event or emit
    output = os.path.abspath(output)
    state = {"previous_copy": "untouched" if os.path.lexists(output) else "none",
             "output": output, "output_exists": os.path.isdir(output)}
    m = _manifest_for(ManifestManager(), source or "")
    if not m:
        # The 1.5.9 CLI said this for a --source that isn't there; the manifest
        # lookup that replaced it called that an unsupported version.
        error = ("That is not a supported HP Click version."
                 if source and os.path.isdir(source) else
                 f"Source app path does not exist: {os.path.abspath(source or '')}. "
                 f"Nothing has been downloaded or written.")
        send(dict({"type": "error", "stage": "build", "error": error}, **state))
        return 1
    expected = None
    if "--expect-replacing" in options:
        at = list(options).index("--expect-replacing") + 1
        expected = options[at] if at < len(options) else ""
    try:
        with folder_lock(os.path.dirname(output)):
            return _build_command(m, source, output, options, send, expected)
    except BuildInProgressError as exc:
        send(dict({"type": "error", "stage": "busy", "error": str(exc)}, **state))
        return 1


def _build_command(m, source, output, rest, send, expected=None):
    """`agent build`, run holding ClickGraft's lock on the output folder
    (build.folder_lock): the build, verify and what happens to the copy it
    replaced, which belong together. `expected` is Review's replacing_token,
    or None when there was no Review."""
    # Checked again here rather than trusted from Review: the copy can be
    # opened, or swapped for another, between that screen and this button.
    # Nothing has been fetched or written when any of these returns.
    token = replacement_token(output, source)
    # Whether there is a copy here for the build to set aside: what a
    # failure before it moves anything has left "untouched".
    had_copy = os.path.lexists(output)
    untouched = "untouched" if had_copy else "none"
    if expected is not None and expected != token:
        change = _replacement_change(output, expected != _NOTHING_TOKEN)
        send({"type": "error", "stage": "replacement_changed", "change": change,
              "during_build": False,
              "previous_copy": "gone" if change == "removed" else "untouched",
              "output": output, "output_exists": os.path.isdir(output),
              "error": _CHANGE_SINCE_REVIEW[change].format(out=output)
                       + " Nothing has been downloaded or changed."})
        return 1
    replacing = existing_copy(output, source)
    if replacing and replacing["open_pids"]:
        # build.py sets the old copy aside and deletes it once the new one
        # passes. Quitting it is the owner's call -- it may be mid-print --
        # so ClickGraft says so and stops, and never kills it.
        send({"type": "error", "stage": "in_use", "output": output,
              "output_exists": True, "pids": replacing["open_pids"],
              "previous_copy": "untouched",
              "error": f"The copy at {output} is open (process "
                       f"{', '.join(str(p) for p in replacing['open_pids'])}). "
                       f"Quit it and try again. Nothing has been replaced."})
        return 1
    if replacing and replacing["printers_lost"] and "--accept-printer-loss" not in rest:
        # Review asks for a tick before it passes the flag. Without it the
        # build would replace a copy that supports printers this one can't.
        send({"type": "error", "stage": "printers_lost", "output": output,
              "output_exists": True, "printers_lost": replacing["printers_lost"],
              "previous_copy": "untouched",
              "error": f"The copy at {output} supports printers that a copy of "
                       f"HP Click {replacing['source_version'] or m['app_version']} "
                       f"would not: {', '.join(replacing['printers_lost'])}. "
                       f"Nothing has been replaced."})
        return 1

    # And once more just before the install: ClickGraft's folder lock keeps
    # out other ClickGrafts, not the owner or anything else, and the build
    # takes a minute (build.py step 11 has what was tried).
    def before_install():
        if replacement_token(output, source) != token:
            change = _replacement_change(output, had_copy)
            raise ReplacementChangedError(_CHANGE_DURING_BUILD[change].format(out=output),
                                          change)

    log = _log_path()
    send({"type": "start", "log_path": log or ""})

    def progress(msg, pct):
        send({"type": "progress", "pct": float(pct), "msg": msg})
        if log:
            try:
                with open(log, "a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%H:%M:%S')}  {pct * 100:5.1f}%  {msg}\n")
            except OSError:
                pass

    try:
        built = build_apple_silicon_bundle(source_app_path=source, output_app_path=output,
                                           manifest=m, progress_callback=progress,
                                           preload=("--no-preload" not in rest),
                                           before_install=before_install,
                                           allow_foreign_host=("--allow-intel-host" in rest))
    except LeftoverPendingError as exc:
        # A copy an earlier build set aside here is still waiting for the
        # owner. Review says so first; this is for when things changed after.
        send({"type": "error", "stage": "leftover_pending", "error": str(exc),
              "leftovers": _describe_leftovers(exc.leftovers),
              "previous_copy": untouched, "output": output,
              "output_exists": os.path.isdir(output), "log_path": log or ""})
        return 1
    except MacOSTooOldError as exc:
        # This Mac is older than the copy would need. Refused before any
        # download unless Electron's runtime set the floor, which is only
        # counted once the copy is made; either way nothing was replaced.
        send({"type": "error", "stage": "macos_too_old", "error": str(exc),
              "needs": exc.needs, "this_mac": exc.this_mac,
              "reasons": exc.reasons, "after_build": getattr(exc, "after_build", False),
              "alternative": native_alternative(parse_version(exc.this_mac)),
              "previous_copy": untouched, "output": output,
              "output_exists": os.path.isdir(output), "log_path": log or ""})
        return 1
    except ReplacementChangedError as exc:
        # Its own stage, not "build": nothing went wrong with ClickGraft, and
        # the wizard's answer is a fresh Review, not a report (22 Sep 2026: this
        # was sent as "build", and got "The copy wasn't finished" and Send a
        # report). The staging copy is gone; the output path is as it was found.
        send({"type": "error", "stage": "replacement_changed", "change": exc.change,
              "during_build": True,
              "previous_copy": "gone" if exc.change == "removed" else "untouched",
              "output": output, "output_exists": os.path.isdir(output),
              "error": str(exc), "log_path": log or ""})
        return 1
    except OutputInUseError as exc:
        # The copy was quiet when this command started and was opened during
        # the build. Same stage as the check above, so the screen that says
        # "quit it and check again" is the one shown, and it is true: the
        # build stopped before it moved anything.
        send({"type": "error", "stage": "in_use", "output": exc.output,
              "output_exists": True, "pids": exc.pids, "error": str(exc),
              "during_build": True, "previous_copy": "untouched",
              "log_path": log or ""})
        return 1
    except InstallError as exc:
        # The new copy could not be put in place, or the build stopped
        # just after it was: the old one was set aside by then, and has
        # been put back, or is safe at previous_path.
        send({"type": "error", "error": str(exc), "stage": "build",
              "previous_copy": exc.previous_copy,
              "previous_path": exc.previous_path or "",
              "output_exists": os.path.isdir(output), "output": output,
              "log_path": log or ""})
        return 1
    except Exception as exc:                                   # noqa: BLE001
        # Everything else stops before the old copy is moved: signing,
        # the last step that can fail, works on the staging copy.
        send({"type": "error", "error": str(exc), "stage": "build",
              "previous_copy": untouched,
              "output_exists": os.path.isdir(output), "output": output,
              "log_path": log or ""})
        return 1

    previous = getattr(built, "previous", None)
    send({"type": "progress", "pct": 1.0, "msg": "Verifying the result…"})
    try:
        ok, results = verify_app_bundle(output, manifest=m)
        if not ok:
            # verify_app_bundle raises for every failure it knows, and this
            # used to trust that, so a (False, results) would have been
            # settled as a pass and the copy it replaced deleted.
            raise VerifyError("Verification did not pass.", "verification", results)
    except BaseException as exc:                               # noqa: BLE001
        # The copy is built and in place, and has not been shown to work.
        # A copy that has -- the one it replaced -- goes back. With none,
        # the new copy stays: it is all there is, and in every case seen
        # before 1.5.9 a copy that failed here still launched fine.
        state = _after_failed_verify(output, previous, progress,
                                     getattr(exc, "check", "") or "")
        if not isinstance(exc, Exception):
            raise
        send(dict({"type": "error", "error": str(exc), "stage": "verify",
                   "check": getattr(exc, "check", "") or "",
                   "results": getattr(exc, "results", {}) or {},
                   "output_exists": os.path.isdir(output), "output": output,
                   "log_path": log or ""}, **state))
        return 1

    # The old copy is settled before anything is said, because saying it can
    # fail: quitting the wizard does not stop this process, and every write to
    # its closed pipe used to raise here, before the delete, leaving the
    # previous copy hidden beside a new one that had passed (found in review,
    # 22 Sep 2026). emit() now tolerates a closed pipe as well.
    done = {"type": "done", "results": results, "output": output,
            "previous_copy": "none", "needs_macos": format_version(declared_minimum(output)) or "",
            "log_path": log or ""}
    notes = []
    if previous:
        record_outcome(previous, "passed")
        try:
            left = discard_previous(previous)
        except OSError as exc:
            # Not even renamed for the bin, so it keeps its own name, and its
            # record says the new copy passed: the next `agent env` offers it
            # to the owner as exactly that.
            done.update(previous_copy="aside", previous_path=previous)
            notes.append(f"Could not remove the copy it replaced, at {previous}: {exc}")
        else:
            done["previous_copy"] = "replaced"
            notes.append("Checks passed. The copy it replaced has been removed.")
            if left:
                # Renamed for the bin by then, the next build here finishes the
                # job (build.sweep_discards); nothing for the owner to do.
                done["previous_left"] = left
                notes.append(f"Part of the copy it replaced is left, at {left}")
    for note in notes:
        progress(note, 1.0)
    send(done)
    return 0


def _after_failed_verify(output, previous, progress, check=""):
    """Put the Mac back as it was before a build whose copy failed verify.

    -> the error's previous_copy / new_copy / previous_path fields. The failed
    copy is deleted, not kept beside the old one for diagnosis: what a report
    needs is the verify message and the test launch's own logs, which live
    outside the bundle (verify.smoke_launch keeps them when a launch fails),
    and 1.4 GB left hidden in Applications is a cost the owner cannot see. It
    can be made again from the same HP Click in a minute.

    Never raises: whatever happens, the wizard is told where the previous copy
    is. An OSError from a rename once escaped from here, and the wizard, never
    sent "done" or "error", stayed on "Checking the result" (found in review,
    22 Sep 2026).
    """
    if not previous:
        return {"previous_copy": "none", "new_copy": "kept"}
    # Written first, so that if putting it back fails, the next `agent env`
    # can say the new copy failed, and which check, rather than guess.
    record_outcome(previous, "failed", check)
    try:
        restore_previous(output, previous)
    except OutputInUseError as exc:
        # Opened in the seconds since verify's own launch was stopped. Deleting
        # it would pull its files out from under it, so both stay as they are.
        reason, detail = "open", str(exc)
    except Exception as exc:                                       # noqa: BLE001
        # InstallError says where things stand; anything else is a surprise,
        # and the old copy is still set aside unless the rename back happened.
        reason, detail = "error", str(exc)
    else:
        progress("The new copy did not pass its checks, so the previous copy has "
                 "been put back.", 1.0)
        return {"previous_copy": "restored", "new_copy": "removed"}
    if not os.path.lexists(previous) and os.path.isdir(output):
        # Back in place after all: the failure came after the rename.
        progress(f"The previous copy is back, but: {detail}", 1.0)
        return {"previous_copy": "restored", "new_copy": "removed"}
    progress(f"Could not put the previous copy back: {detail}", 1.0)
    return {"previous_copy": "aside", "previous_path": previous,
            "new_copy": "kept" if os.path.isdir(output) else "removed",
            "restore_reason": reason, "restore_error": detail}


def _settle_leftover(cmd, backup):
    """restore-previous / discard-previous: what the owner chose to do with a
    copy set aside by a build that did not finish (see _previous_copies).

    Only a path named the way build.install_copy names one, and not one a
    running build still owns, so this can never be pointed at anything else.
    Held under the folder's lock, like a build, and always answers with
    "restored", "discarded" or an error.
    """
    folder = os.path.dirname(os.path.normpath(backup)) if backup else ""
    try:
        with folder_lock(folder):
            return _settle_locked(cmd, backup, folder)
    except BuildInProgressError as exc:
        # Every refusal says whether the copy is still where it was set aside,
        # not only the one that could have moved it: the wizard adds "still
        # safe, set aside where it was" to this message, and until review
        # (22 Sep 2026) it assumed that when the field was missing. Both
        # refusals here can follow the copy being deleted in Finder, or put
        # back from a second window, after the leftover screen was drawn.
        emit({"type": "error", "stage": "leftover", "backup": backup, "error": str(exc),
              "backup_exists": os.path.isdir(backup or "")})
        return 1


def _settle_locked(cmd, backup, folder):
    match = [item for item in leftovers(folder) if folder
             and os.path.normpath(item["path"]) == os.path.normpath(backup)]
    if not backup or not is_set_aside(backup) or not match:
        emit({"type": "error", "stage": "leftover", "backup": backup,
              "error": f"{backup or 'That'} is not a copy ClickGraft set aside, or "
                       f"the build that set it aside is still running.",
              "backup_exists": os.path.isdir(backup or "")})
        return 1
    item = match[0]
    if cmd == "discard-previous":
        try:
            left = discard_previous(item["path"])
        except OSError as exc:
            left, why = item["path"], str(exc)
        else:
            why = f"some of its files could not be deleted, at {left}"
        if left:
            emit({"type": "error", "stage": "leftover", "backup": backup,
                  "error": f"Could not remove {backup}: {why}."})
            return 1
        emit({"type": "discarded", "backup": backup})
        return 0
    try:
        replaced = restore_previous(item["restores_to"], item["path"])
    except Exception as exc:                                       # noqa: BLE001
        # OutputInUseError, InstallError and NotTheBuildsCopyError all say
        # nothing was moved, or that it was moved back; anything else is still
        # answered, so the wizard never waits on a process that died.
        emit({"type": "error", "stage": "leftover", "backup": backup,
              "output": item["restores_to"], "error": str(exc),
              "backup_exists": os.path.isdir(item["path"])})
        return 1
    emit({"type": "restored", "output": item["restores_to"], "removed_copy": replaced})
    return 0
