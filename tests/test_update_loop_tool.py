"""
tests/test_update_loop_tool.py — the tool handed to strangers.

tools/hpclick-update-loop.sh is published for people who are not running it
under supervision, so the two things worth pinning are that looking changes
nothing, and that it tells a broken updater from a working one by testing the
actual fault rather than by matching a version number.

Nothing here calls --fix or --undo: those touch the real launchd domain.
Their behaviour was validated by hand against a re-submitted job.
"""

import os
import shutil
import subprocess

import pytest

TOOL = os.path.join(os.path.dirname(__file__), "..", "site", "hpclick-update-loop.sh")
PLISTBUDDY = "/usr/libexec/PlistBuddy"

pytestmark = pytest.mark.skipif(
    not os.path.isfile(TOOL) or not os.path.exists(PLISTBUDDY),
    reason="needs the tool and macOS PlistBuddy")


def _run(*args):
    return subprocess.run(["bash", TOOL, *args], capture_output=True, text=True, timeout=300)


def _stock_clicks():
    """(path, version) for every STOCK HP Click in /Applications.

    Grafts carry com.hp.hpclick.arm64 and are deliberately out of scope: their
    updater is patched out, so flagging their ShipIt binary would be a false
    alarm.
    """
    out = []
    for name in sorted(os.listdir("/Applications")):
        app = os.path.join("/Applications", name)
        if not name.endswith(".app"):
            continue
        plist = os.path.join(app, "Contents", "Info.plist")
        try:
            bid = subprocess.run([PLISTBUDDY, "-c", "Print :CFBundleIdentifier", plist],
                                 capture_output=True, text=True).stdout.strip()
            ver = subprocess.run([PLISTBUDDY, "-c", "Print :CFBundleShortVersionString", plist],
                                 capture_output=True, text=True).stdout.strip()
        except Exception:
            continue
        if bid == "com.hp.hpclick":
            out.append((app, ver))
    return out


def _vtuple(v):
    try:
        return tuple(int(p) for p in v.split("."))
    except ValueError:
        return ()


def test_check_is_read_only():
    """The promise made to anyone who runs it before trusting it."""
    cache = os.path.expanduser("~/Library/Caches/com.hp.hpclick.ShipIt")
    before_cache = sorted(os.listdir(cache)) if os.path.isdir(cache) else None
    before_disabled = subprocess.run(
        ["launchctl", "print-disabled", "gui/%d" % os.getuid()],
        capture_output=True, text=True).stdout

    assert _run("--check").returncode == 0

    after_cache = sorted(os.listdir(cache)) if os.path.isdir(cache) else None
    after_disabled = subprocess.run(
        ["launchctl", "print-disabled", "gui/%d" % os.getuid()],
        capture_output=True, text=True).stdout
    assert before_cache == after_cache, "--check altered the ShipIt cache"
    assert before_disabled == after_disabled, "--check altered launchd's disabled list"


def test_it_reports_every_stock_click_installed():
    clicks = _stock_clicks()
    if not clicks:
        pytest.skip("no stock HP Click in /Applications")
    out = _run("--check").stdout
    for app, ver in clicks:
        assert os.path.basename(app) in out, "%s missing from the report" % app


def test_the_broken_and_fixed_builds_are_told_apart():
    """HP fixed ShipIt's rpath in 4.11.32. Below that it cannot start at all.

    The tool decides by running ShipIt, not by reading the version, so this
    checks that empirical answer against the known boundary.
    """
    clicks = _stock_clicks()
    older = [(a, v) for a, v in clicks if _vtuple(v) and _vtuple(v) < (4, 11, 32)]
    newer = [(a, v) for a, v in clicks if _vtuple(v) and _vtuple(v) >= (4, 11, 32)]
    if not older and not newer:
        pytest.skip("no stock HP Click with a usable version string")

    out = _run("--check").stdout
    lines = {ln.strip() for ln in out.splitlines() if ".app" in ln}

    def line_for(app):
        base = os.path.basename(app)
        return next((l for l in lines if l.startswith(base)), None)

    for app, ver in older:
        ln = line_for(app)
        assert ln and "broken" in ln, "%s (%s) should read as broken: %r" % (app, ver, ln)
    for app, ver in newer:
        ln = line_for(app)
        assert ln and "works" in ln, "%s (%s) should read as working: %r" % (app, ver, ln)


def test_it_refuses_an_unknown_argument():
    r = _run("--wat")
    assert r.returncode == 2 and "usage" in (r.stdout + r.stderr).lower()
