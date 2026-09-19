"""
clickgraft.agent — a line-oriented JSON interface to the backend.

The Swift front end drives ClickGraft by spawning python3 and reading JSON,
rather than reimplementing any of the build logic. One JSON object per line on
stdout; nothing else is ever printed there.

    python3 -m clickgraft.cli agent env
    python3 -m clickgraft.cli agent plan  --source PATH [--out PATH]
    python3 -m clickgraft.cli agent build --source PATH [--out PATH] [--accept-printer-loss]
    python3 -m clickgraft.cli agent probe --source PATH
    python3 -m clickgraft.cli agent printerinfo

`build` streams:
    {"type":"progress","pct":0.4,"msg":"..."}
    ...
    {"type":"done","results":{...}}      or {"type":"error","error":"..."}
"""
import hashlib
import json
import os
import shutil
import sys
import time

from clickgraft import graftable
from clickgraft.build import OutputInUseError, build_apple_silicon_bundle
from clickgraft.deps import check_clt
from clickgraft.hostarch import host_info
from clickgraft.macho import get_archs
from clickgraft.manifest import ManifestManager
from clickgraft.printerinfo import as_text as printer_text
from clickgraft.probe import probe_app_bundle
from clickgraft.verify import processes_inside, verify_app_bundle

REQUIRED_TOOLS = ["codesign", "install_name_tool", "lipo", "otool", "nm", "ditto"]
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


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _log_path():
    d = os.path.expanduser("~/Library/Logs/clickgraft")
    try:
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, time.strftime("clickgraft-%Y%m%d-%H%M%S.log"))
    except OSError:
        return None


def environment(mm):
    h = host_info()
    return {
        "clt": check_clt(),
        "apple_silicon": h["apple_silicon"],
        "host": h,
        "tools": {t: (shutil.which(t) or "") for t in REQUIRED_TOOLS},
        "versions": sorted(mm.manifests),
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

    open_pids: processes running from the copy. build.py deletes the old copy
    outright before renaming the new one into place, so it must not be open.
    ps_output is for tests.
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


def build_plan(manifest, source, output):
    return {
        "source": source,
        "output": output,
        "app_version": manifest["app_version"],
        "electron": manifest["electron_version"],
        "fixes": fixes_for(manifest),
        # None when nothing is there yet. Otherwise what is being replaced:
        # its version, whether it is open, and any printers the new copy drops.
        "replacing": existing_copy(output, source),
        "patches": [{"path": p["path"], "why": _first_sentence(p.get("why", ""))}
                    for p in manifest["patches"]],
        "dylibs": [{"name": d["name"], "preload": bool(d.get("preload")),
                    "why": _first_sentence(d.get("why", ""))}
                   for d in manifest.get("required_dylibs", [])],
        "downloads": [f"Electron {manifest['electron_version']} (darwin-arm64), "
                      f"SHA-256 checked against the release's SHASUMS256.txt"]
        + [f"{d['name']} from Homebrew's CDN, SHA-256 checked"
           for d in manifest.get("required_dylibs", [])],
    }


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
              "output_per_user": out_per_user})
        return 0

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
        m = _manifest_for(mm, source or "")
        if not m:
            emit({"type": "error", "error": "That is not a supported HP Click version."})
            return 1

        # Checked again here rather than trusted from Review: the copy can be
        # opened, or swapped for another, between that screen and this button.
        # Nothing has been fetched or written when either of these returns.
        replacing = existing_copy(output, source)
        if replacing and replacing["open_pids"]:
            # build.py deletes the old copy before renaming the new one into
            # place. Quitting it is the owner's call -- it may be mid-print --
            # so ClickGraft says so and stops, and never kills it.
            emit({"type": "error", "stage": "in_use", "output": output,
                  "output_exists": True, "pids": replacing["open_pids"],
                  "error": f"The copy at {output} is open (process "
                           f"{', '.join(str(p) for p in replacing['open_pids'])}). "
                           f"Quit it and try again. Nothing has been replaced."})
            return 1
        if replacing and replacing["printers_lost"] and "--accept-printer-loss" not in rest:
            # Review asks for a tick before it passes the flag. Without it the
            # build would replace a copy that supports printers this one can't.
            emit({"type": "error", "stage": "printers_lost", "output": output,
                  "output_exists": True, "printers_lost": replacing["printers_lost"],
                  "error": f"The copy at {output} supports printers that a copy of "
                           f"HP Click {replacing['source_version'] or m['app_version']} "
                           f"would not: {', '.join(replacing['printers_lost'])}. "
                           f"Nothing has been replaced."})
            return 1

        log = _log_path()
        emit({"type": "start", "log_path": log or ""})

        def progress(msg, pct):
            emit({"type": "progress", "pct": float(pct), "msg": msg})
            if log:
                try:
                    with open(log, "a", encoding="utf-8") as f:
                        f.write(f"{time.strftime('%H:%M:%S')}  {pct * 100:5.1f}%  {msg}\n")
                except OSError:
                    pass

        try:
            build_apple_silicon_bundle(source_app_path=source, output_app_path=output,
                                       manifest=m, progress_callback=progress,
                                       allow_foreign_host=("--allow-intel-host" in rest))
        except OutputInUseError as exc:
            # The copy was quiet when this command started and was opened during
            # the build. Same stage as the check above, so the screen that says
            # "quit it and check again" is the one shown, and it is true: the
            # build stopped before it deleted anything.
            emit({"type": "error", "stage": "in_use", "output": exc.output,
                  "output_exists": True, "pids": exc.pids, "error": str(exc),
                  "during_build": True, "log_path": log or ""})
            return 1
        except Exception as exc:                                   # noqa: BLE001
            emit({"type": "error", "error": str(exc), "stage": "build",
                  "output_exists": os.path.isdir(output), "output": output,
                  "log_path": log or ""})
            return 1

        emit({"type": "progress", "pct": 1.0, "msg": "Verifying the result…"})
        try:
            _ok, results = verify_app_bundle(output, manifest=m)
        except Exception as exc:                                   # noqa: BLE001
            # The build finished before this ran. The copy is on disk, and in
            # every case seen so far it launches fine — a failed check here
            # means "we could not confirm it", not "it is broken", and
            # certainly not "nothing was installed".
            emit({"type": "error", "error": str(exc), "stage": "verify",
                  "output_exists": os.path.isdir(output), "output": output,
                  "log_path": log or ""})
            return 1

        emit({"type": "done", "results": results, "output": output, "log_path": log or ""})
        return 0

    emit({"type": "error", "error": f"unknown agent subcommand: {cmd}"})
    return 2
