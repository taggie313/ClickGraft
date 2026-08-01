"""
clickgraft.agent — a line-oriented JSON interface to the backend.

The Swift front end drives ClickGraft by spawning python3 and reading JSON,
rather than reimplementing any of the build logic. One JSON object per line on
stdout; nothing else is ever printed there.

    python3 -m clickgraft.cli agent env
    python3 -m clickgraft.cli agent plan  --source PATH [--out PATH]
    python3 -m clickgraft.cli agent build --source PATH [--out PATH]
    python3 -m clickgraft.cli agent probe --source PATH

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

from clickgraft.build import build_apple_silicon_bundle
from clickgraft.deps import check_clt
from clickgraft.macho import get_archs
from clickgraft.manifest import ManifestManager
from clickgraft.probe import probe_app_bundle
from clickgraft.verify import verify_app_bundle

REQUIRED_TOOLS = ["codesign", "install_name_tool", "lipo", "otool", "nm", "ditto"]
DEFAULT_OUTPUT = "/Applications/HP Click (Apple Silicon).app"


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
    return {
        "clt": check_clt(),
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
        version = (m or {}).get("app_version", "") or _bundle_version(path)
        out.append({
            "path": path,
            "name": name,
            "archs": archs,
            "sha256": sha,
            "version": version,
            "usable": bool(m),
            "reason": reason,
            "why": "" if m else (
                "This one was already made by ClickGraft. Choose your original instead."
                if already else "ClickGraft doesn't know this version yet"),
        })
    return out


def _bundle_version(path):
    """CFBundleShortVersionString, for apps with no manifest to name them."""
    import plistlib
    try:
        with open(os.path.join(path, "Contents", "Info.plist"), "rb") as f:
            return plistlib.load(f).get("CFBundleShortVersionString", "") or ""
    except Exception:
        return ""


def _first_sentence(text):
    import re
    m = re.match(r"^[\s\S]*?[.!?](?=\s|$)", text or "")
    return m.group(0) if m else (text or "")


def build_plan(manifest, source, output):
    return {
        "source": source,
        "output": output,
        "app_version": manifest["app_version"],
        "electron": manifest["electron_version"],
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
    output = arg("--out") or DEFAULT_OUTPUT

    if cmd == "env":
        emit({"type": "env", "env": environment(mm), "candidates": candidates(mm),
              "default_output": DEFAULT_OUTPUT})
        return 0

    if cmd == "plan":
        m = _manifest_for(mm, source or "")
        if not m:
            emit({"type": "error", "error": "That is not a supported HP Click version."})
            return 1
        emit({"type": "plan", "plan": build_plan(m, source, output)})
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
                                       manifest=m, progress_callback=progress)
        except Exception as exc:                                   # noqa: BLE001
            emit({"type": "error", "error": str(exc)})
            return 1

        emit({"type": "progress", "pct": 1.0, "msg": "Verifying the result…"})
        try:
            _ok, results = verify_app_bundle(output, manifest=m)
        except Exception as exc:                                   # noqa: BLE001
            emit({"type": "error", "error": str(exc)})
            return 1

        emit({"type": "done", "results": results, "output": output, "log_path": log or ""})
        return 0

    emit({"type": "error", "error": f"unknown agent subcommand: {cmd}"})
    return 2
