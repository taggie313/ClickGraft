"""
clickgraft.gui.server — the wizard, served locally and shown in the browser.

Replaces a tkinter implementation that could not work: Apple's /usr/bin/python3
links the deprecated system Tk 8.5.9, which on macOS 26 maps widgets with
correct geometry but never paints labels. The window opened with a correct
title and nothing inside it. Every programmatic check passed, because the
widget tree was fine -- only the pixels were missing.

HTML has no equivalent failure mode, and this keeps the dependency list at
exactly one (Xcode Command Line Tools): http.server is standard library and
every Mac has a browser.

Binds to 127.0.0.1 on an ephemeral port and requires a per-run token, because
this UI can write to /Applications and should not be drivable by anything else
on the machine.
"""
import json
import os
import secrets
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from clickgraft.build import build_apple_silicon_bundle
from clickgraft.deps import check_clt, install_clt_interactive
from clickgraft.macho import get_archs
from clickgraft.manifest import ManifestManager
from clickgraft.probe import probe_app_bundle
from clickgraft.verify import verify_app_bundle

HERE = os.path.dirname(os.path.abspath(__file__))
REQUIRED_TOOLS = ["codesign", "install_name_tool", "lipo", "otool", "nm", "ditto"]
DEFAULT_OUTPUT = "/Applications/HP Click (Apple Silicon).app"
IDLE_TIMEOUT = 15.0          # seconds without a heartbeat before shutting down


class Session:
    """All wizard state. One per process."""

    def __init__(self):
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.mm = ManifestManager()
        self.source = None
        self.manifest = None
        self.output = DEFAULT_OUTPUT
        self.phase = "idle"          # idle | building | verifying | done | error
        self.pct = 0.0
        self.message = ""
        self.log = []
        self.error = None
        self.results = {}
        self.probe_report = None
        self.last_seen = time.time()
        self.log_path = self._open_log()

    # -- logging ---------------------------------------------------------
    def _open_log(self):
        d = os.path.expanduser("~/Library/Logs/clickgraft")
        try:
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, time.strftime("clickgraft-%Y%m%d-%H%M%S.log"))
            with open(p, "w", encoding="utf-8") as f:
                f.write(f"ClickGraft session {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            return p
        except OSError:
            return None

    def say(self, msg):
        line = f"{time.strftime('%H:%M:%S')}  {msg}"
        with self.lock:
            self.log.append(line)
            self.log[:] = self.log[-400:]
        if self.log_path:
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    # -- discovery -------------------------------------------------------
    def candidates(self):
        """Every HP Click bundle we can find, with enough detail to choose."""
        found = []
        for base in ("/Applications",):
            try:
                names = sorted(os.listdir(base))
            except OSError:
                continue
            for name in names:
                if not name.endswith(".app") or "click" not in name.lower():
                    continue
                path = os.path.join(base, name)
                asar = os.path.join(path, "Contents", "Resources", "app.asar")
                if not os.path.exists(asar):
                    continue
                import hashlib
                try:
                    with open(asar, "rb") as f:
                        sha = hashlib.sha256(f.read()).hexdigest()
                except OSError:
                    continue
                exe = os.path.join(path, "Contents", "MacOS", "HPClickExe")
                archs = get_archs(exe) if os.path.exists(exe) else []
                m = self.mm.find_manifest(asar_sha256=sha)
                found.append({
                    "path": path, "archs": archs, "sha256": sha,
                    "version": m["app_version"] if m else None,
                    "usable": bool(m),
                    "why": None if m else (
                        "Already patched by ClickGraft — choose your original instead."
                        if "arm64" in archs and "x86_64" not in archs
                        else "Not a supported HP Click version."),
                })
        return found

    def env(self):
        import shutil as sh
        return {
            "clt": check_clt(),
            "tools": {t: (sh.which(t) or None) for t in REQUIRED_TOOLS},
            "versions": sorted(self.mm.manifests),
            "log_path": self.log_path,
        }

    def plan(self):
        if not self.manifest:
            return None
        m = self.manifest
        return {
            "source": self.source,
            "output": self.output,
            "app_version": m["app_version"],
            "electron": m["electron_version"],
            "patches": [{"path": p["path"], "why": p.get("why", "")} for p in m["patches"]],
            "dylibs": [{"name": d["name"], "formula": d.get("brew_formula"),
                        "preload": bool(d.get("preload")), "why": d.get("why", "")}
                       for d in m.get("required_dylibs", [])],
            "downloads": [{
                "what": f"Electron {m['electron_version']} (darwin-arm64)",
                "from": "github.com/electron/electron/releases",
                "checked": "SHA-256 against the release's published SHASUMS256.txt",
            }] + [{
                "what": d["name"],
                "from": "Homebrew's CDN (formulae.brew.sh), or your local Homebrew",
                "checked": "SHA-256 from the formula metadata",
            } for d in m.get("required_dylibs", [])],
        }

    # -- work ------------------------------------------------------------
    def start_build(self):
        with self.lock:
            if self.phase in ("building", "verifying"):
                return False
            self.phase, self.pct, self.error, self.results = "building", 0.0, None, {}
        threading.Thread(target=self._worker, daemon=True).start()
        return True

    def _worker(self):
        def progress(msg, pct):
            with self.lock:
                self.message, self.pct = msg, float(pct)
            self.say(f"{pct * 100:5.1f}%  {msg}")

        try:
            build_apple_silicon_bundle(
                source_app_path=self.source, output_app_path=self.output,
                manifest=self.manifest, progress_callback=progress)
        except Exception as exc:                                  # noqa: BLE001
            self.say(f"BUILD FAILED: {exc}")
            with self.lock:
                self.phase, self.error = "error", str(exc)
            return

        with self.lock:
            self.phase, self.message, self.pct = "verifying", "Verifying the result…", 1.0
        try:
            _, results = verify_app_bundle(self.output, manifest=self.manifest)
            for k, v in results.items():
                self.say(f"verify  {k}: {v}")
            with self.lock:
                self.results, self.phase = results, "done"
        except Exception as exc:                                  # noqa: BLE001
            self.say(f"VERIFICATION FAILED: {exc}")
            with self.lock:
                self.phase, self.error = "error", str(exc)

    def state(self):
        with self.lock:
            return {
                "phase": self.phase, "pct": self.pct, "message": self.message,
                "error": self.error, "results": self.results,
                "log": self.log[-160:], "log_path": self.log_path,
                "source": self.source, "output": self.output,
                "matched": self.manifest["app_version"] if self.manifest else None,
            }


SESSION = Session()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass                                    # keep the terminal quiet

    # -- helpers ---------------------------------------------------------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        token = self.headers.get("X-ClickGraft-Token")
        if not token:
            from urllib.parse import parse_qs, urlparse
            token = (parse_qs(urlparse(self.path).query).get("t") or [""])[0]
        if secrets.compare_digest(token or "", SESSION.token):
            SESSION.last_seen = time.time()
            return True
        self._send(403, {"error": "bad token"})
        return False

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except ValueError:
            return {}

    # -- routes ----------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "ui.html"), "r", encoding="utf-8") as f:
                page = f.read().replace("__TOKEN__", SESSION.token)
            return self._send(200, page, "text/html; charset=utf-8")
        if not self._authed():
            return
        if path == "/api/env":
            return self._send(200, {"env": SESSION.env(), "candidates": SESSION.candidates()})
        if path == "/api/plan":
            return self._send(200, {"plan": SESSION.plan()})
        if path == "/api/state":
            return self._send(200, SESSION.state())
        self._send(404, {"error": "no such endpoint"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if not self._authed():
            return
        body = self._body()

        if path == "/api/select":
            src = body.get("source") or ""
            m = None
            asar = os.path.join(src, "Contents", "Resources", "app.asar")
            if os.path.exists(asar):
                import hashlib
                with open(asar, "rb") as f:
                    m = SESSION.mm.find_manifest(asar_sha256=hashlib.sha256(f.read()).hexdigest())
            SESSION.source, SESSION.manifest = src, m
            SESSION.say(f"source selected: {src} ({'supported' if m else 'UNSUPPORTED'})")
            return self._send(200, {"matched": m["app_version"] if m else None})

        if path == "/api/output":
            SESSION.output = body.get("output") or DEFAULT_OUTPUT
            return self._send(200, {"output": SESSION.output})

        if path == "/api/install-clt":
            threading.Thread(target=install_clt_interactive, daemon=True).start()
            return self._send(200, {"started": True})

        if path == "/api/probe":
            try:
                _draft, report = probe_app_bundle(body.get("source") or SESSION.source)
                SESSION.probe_report = report
                return self._send(200, {"report": report})
            except Exception as exc:                              # noqa: BLE001
                return self._send(200, {"error": str(exc)})

        if path == "/api/build":
            return self._send(200, {"started": SESSION.start_build()})

        if path == "/api/reveal":
            target = body.get("path") or SESSION.output
            subprocess.run(["open", "-R", target], check=False)
            return self._send(200, {"ok": True})

        if path == "/api/quit":
            self._send(200, {"ok": True})
            threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()
            return

        self._send(404, {"error": "no such endpoint"})


def _watchdog(server):
    """Close down once the page stops checking in, so quitting the browser
    tab does not leave a server running forever."""
    while True:
        time.sleep(3)
        if time.time() - SESSION.last_seen > IDLE_TIMEOUT and SESSION.phase not in (
                "building", "verifying"):
            server.shutdown()
            return


def run_wizard(open_browser=True):
    """Entry point. Name kept so cli.py and the .app launcher are unchanged."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/?t={SESSION.token}"
    print(f"ClickGraft is open at {url}", flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:                                         # noqa: BLE001
            subprocess.run(["open", url], check=False)
    threading.Thread(target=_watchdog, args=(server,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return url
