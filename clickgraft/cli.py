"""
clickgraft.cli — Command-line interface for clickgraft.
Subcommands: preflight, build, verify, probe, gui.
Target: Python 3.9+ (Standard Library only)
"""

import argparse
import json
import os
import sys

from clickgraft.build import (BuildInProgressError, InstallError, build_apple_silicon_bundle,
                              discard_previous, folder_lock)
from clickgraft.deps import check_clt
from clickgraft.manifest import ManifestManager
from clickgraft.probe import probe_app_bundle
from clickgraft.report import format_human_report, format_json_report
from clickgraft.verify import verify_app_bundle


def cmd_preflight(args):
    print("[+] === Running Preflight Checks ===")
    if not check_clt():
        print("[ERROR] Xcode Command Line Tools are missing or incomplete.")
        print("Run 'xcode-select --install' to install required tools (codesign, install_name_tool, lipo, otool, ditto).")
        sys.exit(1)
    print("[+] Xcode Command Line Tools: INSTALLED")

    mm = ManifestManager()
    print(f"[+] Loaded {len(mm.manifests)} version manifest(s) from {mm.manifests_dir}")
    for ver, m in mm.manifests.items():
        print(f"  - Version {ver}: Electron {m['electron_version']}, SHA256 {m['asar_sha256'][:12]}...")

    print("[+] ALL PREFLIGHT CHECKS PASSED SUCCESSFULLY!")


def cmd_build(args):
    source_app = args.source or "/Applications/HP Click.app"
    if not os.path.exists(source_app) and os.path.exists("/Applications/HP Click (x86_64 Backup).app"):
        source_app = "/Applications/HP Click (x86_64 Backup).app"

    out_app = args.out
    preload = not args.no_preload

    print(f"[+] Starting arm64 Build from source: {source_app}")
    print(f"[+] Output bundle target: {out_app or 'HP Click (Apple Silicon).app'}")

    def _progress(msg, pct):
        print(f"[{pct*100:5.1f}%] {msg}")

    # The build sets the copy it replaces aside rather than deleting it, and
    # hands back where (build.install_copy). A build that raises has already
    # put it back, or says where it is (InstallError "aside"), so all that is
    # left here is deleting it once the build has succeeded. The wizard waits
    # for verify before it does that; this command does not verify, so the
    # build finishing is all it has to go on, as it always was. Both under the
    # output folder's lock, as in the wizard (build.folder_lock).
    out_path = os.path.abspath(out_app or os.path.join(
        os.path.dirname(os.path.abspath(source_app)), "HP Click (Apple Silicon).app"))
    try:
        with folder_lock(os.path.dirname(out_path)):
            _build_and_settle(source_app, out_path, preload, _progress)
    except BuildInProgressError as e:
        # Another ClickGraft is building, checking or settling a copy there.
        print(f"[ERROR] Build failed: {e}")
        print("[+] Nothing at the output path was replaced.")
        sys.exit(1)


def _build_and_settle(source_app, out_app, preload, _progress):
    try:
        built = build_apple_silicon_bundle(
            source_app_path=source_app,
            output_app_path=out_app,
            preload=preload,
            progress_callback=_progress
        )
    except InstallError as e:
        print(f"[ERROR] Build failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] Build failed: {e}")
        print("[+] Nothing at the output path was replaced.")
        sys.exit(1)
    print(f"[+] BUILD SUCCESSFUL! Result: {built.output}")
    if built.previous:
        try:
            left = discard_previous(built.previous)
        except OSError as e:
            left = built.previous
            print(f"[WARN] {e}")
        if not left:
            print("[+] Removed the copy it replaced.")
        else:
            print(f"[WARN] Could not remove all of the copy it replaced; what is left "
                  f"is at {left}. Delete it yourself if it is still there after the "
                  f"next build into that folder.")


def cmd_verify(args):
    target_app = args.app
    if not target_app:
        for candidate in ["/Applications/HP Click (Apple Silicon).app", "build/HP Click (Apple Silicon).app", "/Applications/HP Click.app"]:
            if os.path.exists(candidate):
                target_app = candidate
                break

    if not target_app or not os.path.exists(target_app):
        print(f"[ERROR] Target application not found for verification: {target_app}")
        sys.exit(1)

    print(f"[+] Running Verification Suite against: {target_app}")
    try:
        ok, results = verify_app_bundle(target_app)
        print(format_human_report("Verification Suite Results", results))
        print("[+] ALL VERIFICATION CHECKS PASSED SUCCESSFULLY!")
    except Exception as e:
        print(f"[ERROR] Verification FAILED: {e}")
        sys.exit(1)


def cmd_probe(args):
    target_app = args.app or "/Applications/HP Click.app"
    if not os.path.exists(target_app) and os.path.exists("/Applications/HP Click (x86_64 Backup).app"):
        target_app = "/Applications/HP Click (x86_64 Backup).app"

    print(f"[+] Probing app bundle: {target_app}")
    try:
        draft_manifest, report_str = probe_app_bundle(target_app)
        print(report_str)

        out_m = args.out_manifest
        if out_m:
            with open(out_m, "w", encoding="utf-8") as f:
                json.dump(draft_manifest, f, indent=2)
            print(f"\n[+] Draft manifest saved to: {out_m}")
    except Exception as e:
        print(f"[ERROR] Probe failed: {e}")
        sys.exit(1)


def cmd_gui(args):
    """Open the native app. When running from a source checkout there is no
    bundle to open, so point the user at the built one."""
    import subprocess
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app = os.path.join(here, "dist", "ClickGraft.app")
    if os.path.exists(app):
        subprocess.run(["open", app], check=False)
    else:
        print("No built app found. Build one with:  ./packaging/build_app.sh")
        sys.exit(1)


def cmd_agent(args):
    from clickgraft.agent import main as agent_main
    sys.exit(agent_main(args.agent_args))


def main():
    parser = argparse.ArgumentParser(description="ClickGraft — Open-source Apple Silicon patcher for HP Click")
    subparsers = parser.add_subparsers(dest="subcommand", help="Subcommand to execute")

    # preflight
    subparsers.add_parser("preflight", help="Run environment preflight checks")

    # build
    build_p = subparsers.add_parser("build", help="Build native arm64 app copy")
    build_p.add_argument("--source", help="Path to source HP Click.app bundle")
    build_p.add_argument("--out", help="Path to target output HP Click (Apple Silicon).app bundle")
    build_p.add_argument("--no-preload", action="store_true", help="Disable DYLD_INSERT_LIBRARIES preload")

    # verify
    verify_p = subparsers.add_parser("verify", help="Verify built Apple Silicon app bundle")
    verify_p.add_argument("--app", help="Path to app bundle to verify")

    # probe
    probe_p = subparsers.add_parser("probe", help="Analyze unmanifested app bundle and generate draft manifest")
    probe_p.add_argument("--app", help="Path to source app bundle to probe")
    probe_p.add_argument("--out-manifest", help="Path to save draft manifest JSON file")

    # gui
    subparsers.add_parser("gui", help="Open the ClickGraft app")

    ap = subparsers.add_parser("agent", help="JSON interface used by the native app")
    ap.add_argument("agent_args", nargs=argparse.REMAINDER)
    ap.set_defaults(func=cmd_agent)

    args = parser.parse_args()

    if not args.subcommand or args.subcommand == "gui":
        cmd_gui(args)
    elif args.subcommand == "preflight":
        cmd_preflight(args)
    elif args.subcommand == "build":
        cmd_build(args)
    elif args.subcommand == "verify":
        cmd_verify(args)
    elif args.subcommand == "probe":
        cmd_probe(args)
    elif args.subcommand == "agent":
        cmd_agent(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
