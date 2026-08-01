"""
clickgraft.gui.wizard — Tkinter 8-screen step-by-step wizard interface.
Target: Python 3.9+ (Tk 8.5+ compatible)
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import messagebox, ttk

from clickgraft.build import build_apple_silicon_bundle
from clickgraft.deps import check_clt, install_clt_interactive
from clickgraft.macho import get_archs
from clickgraft.manifest import ManifestManager
from clickgraft.probe import probe_app_bundle
from clickgraft.verify import verify_app_bundle


class ClickGraftWizard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ClickGraft — Apple Silicon Repacker for HP Click")
        self.geometry("640x480")
        self.resizable(False, False)

        self.source_app = "/Applications/HP Click.app"
        if not os.path.exists(self.source_app) and os.path.exists("/Applications/HP Click (x86_64 Backup).app"):
            self.source_app = "/Applications/HP Click (x86_64 Backup).app"

        self.output_app = "/Applications/HP Click (Apple Silicon).app"
        self.manifest_manager = ManifestManager()
        self.matched_manifest = None
        self.build_results = {}
        self.current_step = 1

        self.log_path = self._open_log()

        self.container = ttk.Frame(self, padding="15")
        self.container.pack(fill="both", expand=True)

        self.show_screen_1_welcome()

    # ---- session log -------------------------------------------------
    def _open_log(self):
        """Every run writes a log the user can open from the final screen.

        Without one, a failed build leaves nothing to attach to a bug report --
        which matters most for the users the GUI exists for, who will not be
        re-running anything from a terminal.
        """
        log_dir = os.path.expanduser("~/Library/Logs/clickgraft")
        try:
            os.makedirs(log_dir, exist_ok=True)
            path = os.path.join(log_dir, time.strftime("clickgraft-%Y%m%d-%H%M%S.log"))
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"ClickGraft session {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            return path
        except OSError:
            return None

    def log(self, msg):
        line = f"{time.strftime('%H:%M:%S')}  {msg}"
        if self.log_path:
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass
        return line

    # ---- reusable "Show technical detail" disclosure ------------------
    def add_detail(self, parent, get_text):
        """Collapsible detail panel. `get_text` is a callable so the panel
        reflects state at the moment it is opened, not screen-build time."""
        holder = ttk.Frame(parent)
        holder.pack(anchor="w", fill="both", expand=False, pady=(4, 0))
        state = {"open": False, "widget": None}

        def toggle():
            if state["open"]:
                if state["widget"] is not None:
                    state["widget"].destroy()
                    state["widget"] = None
                state["open"] = False
                btn.config(text="▸ Show technical detail")
                return
            txt = tk.Text(holder, height=9, wrap="none", relief="sunken", borderwidth=1)
            try:
                body = get_text()
            except Exception as exc:                      # never let detail break a screen
                body = f"<could not render detail: {exc}>"
            txt.insert("1.0", body)
            txt.config(state="disabled")
            sb = ttk.Scrollbar(holder, orient="vertical", command=txt.yview)
            txt.config(yscrollcommand=sb.set)
            txt.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
            state["widget"], state["open"] = txt, True
            btn.config(text="▾ Hide technical detail")

        btn = ttk.Button(holder, text="▸ Show technical detail", command=toggle)
        btn.pack(anchor="w")
        return holder

    def clear_container(self):
        for widget in self.container.winfo_children():
            widget.destroy()

    # --- Screen 1: Welcome ---
    def show_screen_1_welcome(self):
        self.current_step = 1
        self.clear_container()

        lbl_title = ttk.Label(self.container, text="Welcome to ClickGraft", font=("Helvetica", 16, "bold"))
        lbl_title.pack(anchor="w", pady=(0, 10))

        msg = (
            "This tool creates a 100% native Apple Silicon (arm64) copy of HP Click for macOS.\n\n"
            "• Your original HP Click installation is NEVER modified.\n"
            "• No Homebrew or compiler required.\n"
            "• Downloads official Electron arm64 runtime and LGPL dylibs.\n"
            "• Generates 'HP Click (Apple Silicon).app' alongside your original app."
        )
        lbl_msg = ttk.Label(self.container, text=msg, wraplength=600, justify="left")
        lbl_msg.pack(anchor="w", pady=10)

        self.add_detail(self.container, lambda: (
            f"tool          : ClickGraft (python {sys.version.split()[0]})\n"
            f"session log   : {self.log_path}\n"
            f"manifests dir : {self.manifest_manager.manifests_dir}\n"
            f"known versions: {', '.join(sorted(self.manifest_manager.manifests)) or 'none'}\n"
            f"default output: {self.output_app}\n"
            "\nThe source bundle is opened read-only. Output is a separate .app."))

        frame_btn = ttk.Frame(self.container)
        frame_btn.pack(side="bottom", fill="x", pady=10)
        btn_next = ttk.Button(frame_btn, text="Next >", command=self.show_screen_2_requirements)
        btn_next.pack(side="right")

    # --- Screen 2: Requirements ---
    def show_screen_2_requirements(self):
        self.current_step = 2
        self.clear_container()

        lbl_title = ttk.Label(self.container, text="System Requirements Check", font=("Helvetica", 14, "bold"))
        lbl_title.pack(anchor="w", pady=(0, 10))

        clt_ok = check_clt()
        status_str = "Installed & Ready" if clt_ok else "MISSING (Required for ad-hoc signing)"
        color_str = "green" if clt_ok else "red"

        lbl_clt = ttk.Label(self.container, text=f"Xcode Command Line Tools: {status_str}", font=("Helvetica", 11, "bold"), foreground=color_str)
        lbl_clt.pack(anchor="w", pady=10)

        if not clt_ok:
            btn_inst = ttk.Button(self.container, text="Install Xcode Command Line Tools", command=self.trigger_clt_install)
            btn_inst.pack(anchor="w", pady=5)

        def _clt_detail():
            rows = []
            for tool in ("codesign", "install_name_tool", "lipo", "otool", "nm", "ditto", "xattr"):
                rows.append(f"{tool:20} {shutil.which(tool) or 'NOT FOUND'}")
            rows.append("")
            rows.append("Xcode Command Line Tools is the ONLY dependency. It also provides")
            rows.append("the python3 this wizard runs on. Homebrew is not required.")
            return "\n".join(rows)
        self.add_detail(self.container, _clt_detail)

        frame_btn = ttk.Frame(self.container)
        frame_btn.pack(side="bottom", fill="x", pady=10)
        btn_back = ttk.Button(frame_btn, text="< Back", command=self.show_screen_1_welcome)
        btn_back.pack(side="left")

        btn_next = ttk.Button(frame_btn, text="Next >", command=self.show_screen_3_choose_app, state="normal" if clt_ok else "disabled")
        btn_next.pack(side="right")

    def trigger_clt_install(self):
        threading.Thread(target=self._clt_thread, daemon=True).start()

    def _clt_thread(self):
        install_clt_interactive()
        # Tk is not thread-safe: touching widgets from a worker thread is
        # undefined behaviour. Marshal back to the UI thread, as the build and
        # verify workers already do.
        self.after(0, self.show_screen_2_requirements)

    # --- Screen 3: Choose App ---
    def scan_candidates(self):
        """Find every HP Click bundle in /Applications and classify each.

        Guessing a single path was wrong: on a machine that has already been
        patched, /Applications/HP Click.app IS the patched arm64 build, and
        offering it as the source sends the user into a build that cannot
        succeed. Show every candidate with its architecture and whether it
        matches a manifest, and let them choose.
        """
        import hashlib
        candidates = []
        for base in ("/Applications",):
            if not os.path.isdir(base):
                continue
            for name in sorted(os.listdir(base)):
                if not name.endswith(".app") or "click" not in name.lower():
                    continue
                path = os.path.join(base, name)
                asar_p = os.path.join(path, "Contents", "Resources", "app.asar")
                if not os.path.exists(asar_p):
                    continue
                try:
                    with open(asar_p, "rb") as f:
                        digest = hashlib.sha256(f.read()).hexdigest()
                except OSError:
                    continue
                archs = get_archs(os.path.join(path, "Contents", "MacOS", "HPClickExe"))
                manifest = self.manifest_manager.find_manifest(asar_sha256=digest)
                candidates.append({
                    "path": path, "sha256": digest, "archs": archs, "manifest": manifest,
                })
        # Usable sources first.
        candidates.sort(key=lambda c: (c["manifest"] is None, c["path"]))
        return candidates

    def show_screen_3_choose_app(self):
        self.current_step = 3
        self.clear_container()

        ttk.Label(self.container, text="Select Source HP Click App",
                  font=("Helvetica", 14, "bold")).pack(anchor="w", pady=(0, 6))
        ttk.Label(self.container, wraplength=600, justify="left",
                  text="Choose your ORIGINAL, unmodified HP Click. A bundle this tool "
                       "already produced cannot be used as a source.").pack(anchor="w", pady=(0, 8))

        self.candidates = self.scan_candidates()
        self.selected_source = tk.StringVar(value="")

        if not self.candidates:
            ttk.Label(self.container, text="[X] No HP Click installation found in /Applications",
                      foreground="red").pack(anchor="w", pady=5)
        for cand in self.candidates:
            usable = cand["manifest"] is not None
            if usable:
                detail = f"supported — version {cand['manifest']['app_version']}"
                colour = "green"
            elif "arm64" in cand["archs"] and "x86_64" not in cand["archs"]:
                detail = "already patched by this tool — not usable as a source"
                colour = "red"
            else:
                detail = "unrecognised version — no manifest matches"
                colour = "red"

            row = ttk.Frame(self.container)
            row.pack(anchor="w", fill="x", pady=2)
            ttk.Radiobutton(row, text=os.path.basename(cand["path"]),
                            variable=self.selected_source, value=cand["path"],
                            state="normal" if usable else "disabled",
                            command=self._on_source_selected).pack(side="left")
            ttk.Label(row, text=f"  [{'/'.join(cand['archs']) or '?'}]  {detail}",
                      foreground=colour).pack(side="left")
            ttk.Label(self.container, text=f"      app.asar {cand['sha256'][:16]}…",
                      foreground="grey").pack(anchor="w")

            if usable and not self.selected_source.get():
                self.selected_source.set(cand["path"])

        self.add_detail(self.container, lambda: "\n\n".join(
            f"{c['path']}\n  archs      : {'/'.join(c['archs']) or 'unknown'}\n"
            f"  app.asar   : {c['sha256']}\n"
            f"  manifest   : {c['manifest']['app_version'] if c['manifest'] else 'no match'}"
            for c in self.candidates) or "no candidates found")

        frame_btn = ttk.Frame(self.container)
        frame_btn.pack(side="bottom", fill="x", pady=10)
        ttk.Button(frame_btn, text="< Back", command=self.show_screen_2_requirements).pack(side="left")
        self.btn_next_3 = ttk.Button(frame_btn, text="Next >", command=self._advance_from_screen_3,
                                     state="normal" if self.selected_source.get() else "disabled")
        self.btn_next_3.pack(side="right")

    def _on_source_selected(self):
        if hasattr(self, "btn_next_3"):
            self.btn_next_3.config(state="normal" if self.selected_source.get() else "disabled")

    def _advance_from_screen_3(self):
        self.source_app = self.selected_source.get()
        self.show_screen_4_compatibility()

    # --- Screen 4: Compatibility ---
    def show_screen_4_compatibility(self):
        self.current_step = 4
        self.clear_container()

        lbl_title = ttk.Label(self.container, text="App Compatibility Check", font=("Helvetica", 14, "bold"))
        lbl_title.pack(anchor="w", pady=(0, 10))

        from clickgraft.asar import AsarArchive
        import hashlib
        asar_p = os.path.join(self.source_app, "Contents", "Resources", "app.asar")
        archive = AsarArchive(asar_p)
        with open(asar_p, "rb") as f:
            asar_sha256 = hashlib.sha256(f.read()).hexdigest()

        # Strict: match on the source asar hash and nothing else.
        #
        # This previously fell back to the 4.8.117 manifest whenever the hash
        # did not match, which made the "unsupported" branch unreachable and
        # showed a green "Matched Version: 4.8.117" for an already-patched
        # arm64 bundle. Patches are anchored to exact strings in stock files;
        # a near-miss is not a match.
        self.matched_manifest = self.manifest_manager.find_manifest(asar_sha256=asar_sha256)

        if self.matched_manifest:
            lbl_info = ttk.Label(
                self.container,
                text=f"Matched Version: {self.matched_manifest['app_version']}\n"
                     f"Electron Runtime: {self.matched_manifest['electron_version']}\n"
                     f"ASAR Entries: {self.matched_manifest['asar_entries']['packed']} packed, {self.matched_manifest['asar_entries']['unpacked']} unpacked",
                foreground="green"
            )
            lbl_info.pack(anchor="w", pady=10)
        else:
            lbl_info = ttk.Label(self.container, text="[!] Unsupported or unknown HP Click version", foreground="red")
            lbl_info.pack(anchor="w", pady=10)

            btn_probe = ttk.Button(self.container, text="Generate Compatibility Report", command=self.run_probe)
            btn_probe.pack(anchor="w", pady=5)

        self.add_detail(self.container, lambda: (
            f"source          : {self.source_app}\n"
            f"source app.asar : {asar_sha256}\n"
            f"matched         : {self.matched_manifest['app_version'] if self.matched_manifest else 'NONE'}\n"
            f"expected hash   : {self.matched_manifest['asar_sha256'] if self.matched_manifest else 'n/a'}\n"
            f"asar entries    : {archive.count_entries()}\n\n"
            "Matching is strict: patches are anchored to exact strings in stock\n"
            "files, so a version that merely looks similar is not a match."))

        frame_btn = ttk.Frame(self.container)
        frame_btn.pack(side="bottom", fill="x", pady=10)
        btn_back = ttk.Button(frame_btn, text="< Back", command=self.show_screen_3_choose_app)
        btn_back.pack(side="left")

        btn_next = ttk.Button(frame_btn, text="Next >", command=self.show_screen_5_review_plan, state="normal" if self.matched_manifest else "disabled")
        btn_next.pack(side="right")

    def run_probe(self):
        try:
            draft, report_str = probe_app_bundle(self.source_app)
        except Exception as e:
            messagebox.showerror("Probe Error", str(e))
            return

        self.log("PROBE REPORT\n" + report_str)
        report_file = None
        if self.log_path:
            report_file = os.path.join(os.path.dirname(self.log_path),
                                       time.strftime("probe-%Y%m%d-%H%M%S.txt"))
            try:
                with open(report_file, "w", encoding="utf-8") as f:
                    f.write(report_str)
                    f.write("\n\n--- draft manifest ---\n")
                    f.write(json.dumps(draft, indent=2))
            except OSError:
                report_file = None

        top = tk.Toplevel(self)
        top.title("Compatibility Report")
        top.geometry("760x520")

        ttk.Label(top, padding=8, wraplength=720, justify="left",
                  text="This version is not yet supported. Send this report to the project's "
                       "issue tracker and it can be added.").pack(anchor="w")

        body = ttk.Frame(top)
        body.pack(fill="both", expand=True, padx=8)
        txt = tk.Text(body, wrap="none")
        vsb = ttk.Scrollbar(body, orient="vertical", command=txt.yview)
        hsb = ttk.Scrollbar(body, orient="horizontal", command=txt.xview)
        txt.config(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        txt.insert("1.0", report_str)
        txt.config(state="disabled")
        txt.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        bar = ttk.Frame(top, padding=8)
        bar.pack(fill="x")

        def copy_report():
            self.clipboard_clear()
            self.clipboard_append(report_str)
            messagebox.showinfo("Copied", "Report copied to the clipboard.", parent=top)

        ttk.Button(bar, text="Copy to Clipboard", command=copy_report).pack(side="left")
        if report_file:
            ttk.Button(bar, text="Reveal Saved Report",
                       command=lambda: self.reveal(report_file)).pack(side="left", padx=6)
            ttk.Label(bar, text=os.path.basename(report_file), foreground="grey").pack(side="left")
        ttk.Button(bar, text="Close", command=top.destroy).pack(side="right")

    # --- Screen 5: Review Plan ---
    def show_screen_5_review_plan(self):
        self.current_step = 5
        self.clear_container()

        lbl_title = ttk.Label(self.container, text="Review Build Plan", font=("Helvetica", 14, "bold"))
        lbl_title.pack(anchor="w", pady=(0, 10))

        # Everything on this screen is derived from the manifest. Hardcoding it
        # meant the list silently went stale -- it claimed three dylibs after a
        # fourth (libnghttp2) had been added, on the one screen whose entire
        # job is stating accurately what is about to happen.
        m = self.matched_manifest
        dylibs = [d["name"] for d in m.get("required_dylibs", [])]
        preloaded = [d["name"] for d in m.get("required_dylibs", []) if d.get("preload")]

        lines = [
            f"Source:  {self.source_app}",
            f"Output:  {self.output_app}",
            "",
            "This will:",
            "  1. Copy the source bundle — your original is opened read-only and never modified",
            f"  2. Download Electron {m['electron_version']} (darwin-arm64) and verify its SHA-256",
            f"  3. Add {len(dylibs)} arm64 support librar{'y' if len(dylibs) == 1 else 'ies'}: {', '.join(dylibs)}",
        ]
        if preloaded:
            lines.append(f"       preloaded at launch: {', '.join(preloaded)}")
        lines.append(f"  4. Apply {len(m['patches'])} patch(es) inside app.asar:")
        for p in m["patches"]:
            # Split on sentence boundaries, not bare dots -- "hp_configs.crashAutoSubmit"
            # is one token, and splitting on "." truncated the reason to "hp_configs".
            why = (p.get("why") or "").split(". ")[0].strip()
            if len(why) > 88:
                why = why[:88].rsplit(" ", 1)[0] + "…"
            lines.append(f"       • {p['path']} — {why}")
        lines += [
            "  5. Re-sign the copy ad-hoc (inner to outer)",
            "",
            "Nothing is written until you press Start Build.",
        ]

        txt = tk.Text(self.container, height=16, wrap="word", relief="flat",
                      background=self.cget("background"))
        txt.insert("1.0", "\n".join(lines))
        txt.config(state="disabled")
        txt.pack(anchor="w", fill="both", expand=True, pady=(0, 6))

        self.add_detail(self.container, lambda: json.dumps(self.matched_manifest, indent=2))

        frame_btn = ttk.Frame(self.container)
        frame_btn.pack(side="bottom", fill="x", pady=10)
        btn_back = ttk.Button(frame_btn, text="< Back", command=self.show_screen_4_compatibility)
        btn_back.pack(side="left")

        btn_next = ttk.Button(frame_btn, text="Start Build >", command=self.show_screen_6_build)
        btn_next.pack(side="right")

    # --- Screen 6: Build ---
    def show_screen_6_build(self):
        self.current_step = 6
        self.clear_container()

        lbl_title = ttk.Label(self.container, text="Building Native Apple Silicon Bundle...", font=("Helvetica", 14, "bold"))
        lbl_title.pack(anchor="w", pady=(0, 10))

        self.progress_bar = ttk.Progressbar(self.container, orient="horizontal", mode="determinate")
        self.progress_bar.pack(fill="x", pady=10)

        self.lbl_status = ttk.Label(self.container, text="Initializing build pipeline...")
        self.lbl_status.pack(anchor="w", pady=5)

        threading.Thread(target=self._build_worker, daemon=True).start()

    def _build_worker(self):
        def _cb(msg, pct):
            self.log(f"build {pct * 100:5.1f}%  {msg}")
            self.after(0, lambda: self._update_build_ui(msg, pct))

        try:
            build_apple_silicon_bundle(
                source_app_path=self.source_app,
                output_app_path=self.output_app,
                manifest=self.matched_manifest,
                progress_callback=_cb
            )
            self.after(0, self.show_screen_7_verify)
        except Exception as e:
            # Bind the message NOW. Python deletes `e` when the except block
            # ends, so a deferred lambda closing over it raises NameError and
            # the error is never shown -- which is what the original
            # messagebox.showerror(..., str(e)) did.
            msg, detail = str(e), traceback.format_exc()
            self.log("BUILD FAILED\n" + detail)
            self.after(0, lambda: self.show_failure_screen(
                "Build failed", msg, detail, retry=self.show_screen_6_build,
                back=self.show_screen_5_review_plan))

    # --- Failure screen (shared by build and verify) ---
    def show_failure_screen(self, title, message, detail, retry, back):
        """A dismissed error dialog used to leave the user staring at a frozen
        progress bar with no way forward. Give them the error, the log, and a
        way back."""
        self.clear_container()

        ttk.Label(self.container, text=title, font=("Helvetica", 15, "bold"),
                  foreground="red").pack(anchor="w", pady=(0, 8))
        ttk.Label(self.container, text=message, wraplength=600,
                  justify="left").pack(anchor="w", pady=(0, 6))

        if self.log_path:
            ttk.Label(self.container, text=f"Full log: {self.log_path}",
                      foreground="grey", wraplength=600).pack(anchor="w")
            ttk.Button(self.container, text="Open Log",
                       command=lambda: self.reveal(self.log_path)).pack(anchor="w", pady=4)

        self.add_detail(self.container, lambda: detail)

        frame_btn = ttk.Frame(self.container)
        frame_btn.pack(side="bottom", fill="x", pady=10)
        ttk.Button(frame_btn, text="< Back", command=back).pack(side="left")
        ttk.Button(frame_btn, text="Retry", command=retry).pack(side="right")

    def _update_build_ui(self, msg, pct):
        self.lbl_status.config(text=msg)
        self.progress_bar["value"] = pct * 100

    # --- Screen 7: Verify ---
    def show_screen_7_verify(self):
        self.current_step = 7
        self.clear_container()

        lbl_title = ttk.Label(self.container, text="Running Post-Build Verification...", font=("Helvetica", 14, "bold"))
        lbl_title.pack(anchor="w", pady=(0, 10))

        self.lbl_verify_status = ttk.Label(self.container, text="Verifying bundle integrity and running smoke-launch test...")
        self.lbl_verify_status.pack(anchor="w", pady=10)

        threading.Thread(target=self._verify_worker, daemon=True).start()

    def _verify_worker(self):
        try:
            ok, results = verify_app_bundle(self.output_app, manifest=self.matched_manifest)
            self.build_results = results
            for k, v in results.items():
                self.log(f"verify  {k}: {v}")
            self.after(0, self.show_screen_8_done)
        except Exception as e:
            msg, detail = str(e), traceback.format_exc()   # see _build_worker
            self.log("VERIFICATION FAILED\n" + detail)
            self.after(0, lambda: self.show_failure_screen(
                "Verification failed", msg, detail,
                retry=self.show_screen_7_verify, back=self.show_screen_5_review_plan))

    # --- Screen 8: Done ---
    def show_screen_8_done(self):
        self.current_step = 8
        self.clear_container()

        lbl_title = ttk.Label(self.container, text="Build Completed Successfully!", font=("Helvetica", 16, "bold"), foreground="green")
        lbl_title.pack(anchor="w", pady=(0, 10))

        done_msg = (
            f"Native arm64 app bundle created at:\n{self.output_app}\n\n"
            "IMPORTANT NOTES:\n"
            "1. The original x86_64 app and native arm64 app share settings/printers, so THEY CANNOT RUN AT THE SAME TIME.\n"
            "2. To uninstall, simply move 'HP Click (Apple Silicon).app' to Trash. Your original app was never touched."
        )

        lbl_msg = ttk.Label(self.container, text=done_msg, justify="left", wraplength=600)
        lbl_msg.pack(anchor="w", pady=10)

        row = ttk.Frame(self.container)
        row.pack(anchor="w", pady=5)
        ttk.Button(row, text="Reveal in Finder", command=self.reveal_in_finder).pack(side="left")
        if self.log_path:
            ttk.Button(row, text="Open Log",
                       command=lambda: self.reveal(self.log_path)).pack(side="left", padx=6)

        self.add_detail(self.container, lambda: "\n".join(
            [f"log: {self.log_path}", ""] +
            [f"{k}: {v}" for k, v in self.build_results.items()]))

        frame_btn = ttk.Frame(self.container)
        frame_btn.pack(side="bottom", fill="x", pady=10)
        btn_finish = ttk.Button(frame_btn, text="Finish", command=self.destroy)
        btn_finish.pack(side="right")

    def reveal(self, path):
        # subprocess with an argument list, not os.system with an interpolated
        # string -- a path containing a quote would otherwise be shell syntax.
        subprocess.run(["open", "-R", path], check=False)

    def reveal_in_finder(self):
        self.reveal(self.output_app)


def run_wizard():
    app = ClickGraftWizard()
    app.mainloop()


if __name__ == "__main__":
    run_wizard()
