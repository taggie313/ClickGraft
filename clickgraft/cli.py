"""
clickgraft.cli — Command-line interface for clickgraft.
Subcommands: preflight, build, verify, probe, gui.
Target: Python 3.9+ (Standard Library only)
"""

import argparse
import json
import os
import sys

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


# What each of the wizard's stages means, for someone at a terminal. Until the
# 22 Sep 2026 review this printed the stage names themselves ("[ERROR] in_use:
# ..."), which are the wizard's vocabulary, not the reader's.
_FAILED = {
    "busy": "Another ClickGraft is working in that folder",
    "in_use": "Not replaced: the copy there is open",
    "printers_lost": "Not replaced: the new copy would lose printers",
    "replacement_changed": "Not replaced: the copy there changed",
    "macos_too_old": "This Mac's macOS is too old for the copy",
    "leftover_pending": "Not replaced: a copy set aside by an earlier build is waiting",
    "verify": "The new copy did not pass its checks",
}

# agent.py's previous_copy, as a sentence that is true of each.
_PREVIOUS = {
    "none": "There was no copy at the output path.",
    "untouched": "The copy at the output path has been left as it was.",
    "gone": "The copy that was at the output path was removed by something else "
            "during the build, and nothing has been put in its place.",
    "restored": "The copy that was there has been put back as it was.",
    "aside": "The copy that was there could not be put back. It is safe, set aside at {path}.",
}


def _print_build_event(ev, progress):
    """Print one of agent.run_build's events for a person."""
    kind = ev["type"]
    if kind == "progress":
        progress(ev["msg"], ev["pct"])
    elif kind == "start":
        if ev.get("log_path"):
            print(f"[+] Writing a log to {ev['log_path']}")
    elif kind == "error":
        print(f"[ERROR] {_FAILED.get(ev.get('stage'), 'Build failed')}.")
        print(f"        {ev['error']}")
        if ev.get("stage") == "printers_lost":
            print("[+] Use --accept-printer-loss only if you accept losing support "
                  "for those printers.")
        previous = _PREVIOUS.get(ev.get("previous_copy", ""))
        if previous:
            print("[+] " + previous.format(path=ev.get("previous_path", "")))
        if ev.get("new_copy") == "kept":
            # The one case where a copy that has not passed is left in place:
            # there was nothing before it to put back (agent._after_failed_verify).
            # The 22 Sep 2026 review found the CLI said only "Previous copy: none".
            print(f"[WARN] The new copy was left at {ev.get('output', '')}, but it did NOT "
                  f"pass its checks. Don't rely on it: delete it, or build again once "
                  f"the problem above is fixed.")
        elif ev.get("new_copy") == "removed":
            print("[+] The new copy has been removed.")
        if ev.get("log_path"):
            print(f"[+] The full log is at {ev['log_path']}")
    elif kind == "done":
        print(format_human_report("Verification Suite Results", ev["results"]))
        print(f"[+] BUILD SUCCESSFUL! Result: {ev['output']}")
        if ev.get("previous_copy") == "aside":
            print(f"[WARN] Could not remove the copy it replaced. It is still set aside "
                  f"at {ev['previous_path']}; open ClickGraft to remove it, or delete it "
                  f"yourself.")
        if ev.get("log_path"):
            print(f"[+] The full log is at {ev['log_path']}")


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

    out_path = os.path.abspath(out_app or os.path.join(
        os.path.dirname(os.path.abspath(source_app)), "HP Click (Apple Silicon).app"))
    from clickgraft.agent import run_build

    options = [flag for flag, enabled in (
        ("--no-preload", not preload),
        ("--accept-printer-loss", getattr(args, "accept_printer_loss", False)),
        ("--allow-intel-host", getattr(args, "allow_intel_host", False))) if enabled]
    code = run_build(source_app, out_path, options,
                     emit_event=lambda ev: _print_build_event(ev, _progress))
    if code:
        sys.exit(code)


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
        if not ok:
            sys.exit(1)
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


def cmd_capabilities(args):
    """What each recorded HP Click can do, and which one this person needs."""
    from clickgraft import capabilities

    if args.app:
        cap = capabilities.of_bundle(args.app)
        if cap is None:
            print(f"[ERROR] Not a readable app bundle: {args.app}")
            sys.exit(1)
        print(f"[+] {args.app}")
        print(f"    version        {cap['version'] or '?'}"
              f"{'' if cap['is_stock'] else '   (a ClickGraft copy, not a stock HP build)'}")
        print(f"    declared floor {cap['declared_floor'] or '?'}")
        print(f"    architectures  {'+'.join(cap['exe_archs']) or '?'}")
        print(f"    electron       {cap['electron'] or '?'}")
        print(f"    printers       {cap['printer_count'] if cap['printer_count'] is not None else '?'}")
        graft = capabilities.graftable_version(cap)
        answer = "yes" if graft else "no" if graft is False else "don't know"
        print(f"    graftable      {answer}")
        for reason in cap["blockers"]:
            print(f"                   {reason}")
        return

    if args.printer or args.macos:
        got = capabilities.recommend(printer=args.printer, macos=args.macos)
        if got["version"]:
            print(f"[+] Run HP Click {got['version']}")
            print(f"    {got['why']}")
            if got["needs_graft"]:
                print("    ClickGraft has to make an Apple Silicon copy of it first")
            if got["needs_rosetta"]:
                print("    It will run under Rosetta")
            if got["obtainable"] is False:
                print("    HP no longer serves this version")
            if got["alternatives"]:
                print(f"    also fits: {', '.join(got['alternatives'])}")
        else:
            print(f"[!] No HP Click fits: {got['why']}")
            for version, reason in got["blocked"]:
                print(f"    {version:<9} {reason}")
        return

    rows = capabilities.table()
    if not rows:
        print("No capability table recorded. Build one with:"
              "  python3 packaging/measure_capabilities.py <bundle> ...")
        sys.exit(1)
    head = f"{'version':<9} {'needs':<9} {'libraries':<10} {'architectures':<14} {'electron':<9} {'printers':<9} graft"
    print(head)
    print("-" * len(head))
    for r in rows:
        graft = "yes" if r["graftable"] else "HP native" if r["hp_native"] else \
                "no" if r["graftable"] is False else "?"
        print(f"{r['version']:<9} {r['declared_floor']:<9} {r['library_floor']:<10} "
              f"{r['archs']:<14} {r['electron']:<9} {str(r['printers']):<9} {graft}")
    print("\n'needs' is the floor macOS enforces; 'libraries' is the highest any library inside")
    print("declares, which is an upper bound and not a tested requirement.")


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
    build_p.add_argument("--no-preload", action="store_true",
                         help="Diagnostic build: leave the preloaded libraries out of the "
                              "launcher. Such a copy never passes verification: a copy it "
                              "would replace is put back, and with none there it is left "
                              "in place unverified")

    build_p.add_argument("--accept-printer-loss", action="store_true", help="Accept losing printer support when replacing a copy")
    build_p.add_argument("--allow-intel-host", action="store_true", help="Build on Intel for an Apple Silicon Mac; launch verification is skipped")

    # verify
    verify_p = subparsers.add_parser("verify", help="Verify built Apple Silicon app bundle")
    verify_p.add_argument("--app", help="Path to app bundle to verify")

    # probe
    probe_p = subparsers.add_parser("probe", help="Analyze unmanifested app bundle and generate draft manifest")
    probe_p.add_argument("--app", help="Path to source app bundle to probe")
    probe_p.add_argument("--out-manifest", help="Path to save draft manifest JSON file")

    # gui
    # capabilities
    cap_p = subparsers.add_parser("capabilities",
                                  help="What each HP Click version can do, and which one you need")
    cap_p.add_argument("--app", help="Describe one bundle instead of the recorded table")
    cap_p.add_argument("--printer", help="Printer model, e.g. T730, to get a recommendation")
    cap_p.add_argument("--macos", help="macOS version to assume, e.g. 10.14 (default: this Mac)")

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
    elif args.subcommand == "capabilities":
        cmd_capabilities(args)
    elif args.subcommand == "agent":
        cmd_agent(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
