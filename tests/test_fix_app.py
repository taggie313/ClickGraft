"""
tests/test_fix_app.py — the GUI app and the shell script must not drift.

There are two implementations of the same diagnosis: site/hpclick-update-loop.sh
for people who would rather read a script, and packaging/ClickUpdaterFix.swift
for people who should not have to open a terminal. They are handed to the same
users about the same machine, so they have to agree. The app's --print mode
exists for this: it renders what the window would say, as text.

Nothing here applies a fix; both fix paths were validated by hand against a
re-submitted launchd job.
"""

import os
import re
import subprocess

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
APP = os.path.join(ROOT, "dist", "Click Updater Fix.app", "Contents", "MacOS", "ClickUpdaterFix")
SH = os.path.join(ROOT, "site", "hpclick-update-loop.sh")
SRC = os.path.join(ROOT, "packaging", "ClickUpdaterFix.swift")

pytestmark = pytest.mark.skipif(not os.path.exists(APP),
                                reason="dist app not built (./packaging/build_fix_app.sh)")


def app_print():
    r = subprocess.run([APP, "--print"], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    d = {"installs": {}}
    for line in r.stdout.splitlines():
        if line.rstrip() == "installs:":
            continue                      # header, not a key
        if line.startswith("  ") and "|" in line:
            name, ver, state = (p.strip() for p in line.strip().split("|"))
            d["installs"][name] = (ver, state)
        elif ":" in line:
            k, _, v = line.partition(":")
            d[k.strip()] = v.strip()
    return d


def sh_check():
    r = subprocess.run(["bash", SH, "--check"], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_print_mode_changes_nothing():
    before = app_print()
    app_print()
    assert app_print() == before


def test_the_two_implementations_agree_on_every_install():
    a = app_print()
    s = sh_check()
    installs = a.get("installs", {})
    if not installs:
        pytest.skip("no stock HP Click installed")
    for name, (ver, state) in installs.items():
        line = next((l for l in s.splitlines() if l.strip().startswith(name)), None)
        assert line, "%s missing from the script's report" % name
        assert ver in line, "version disagrees for %s: %r vs %r" % (name, ver, line)
        if state == "broken":
            assert "broken" in line, "script does not call %s broken: %r" % (name, line)
        elif state == "works":
            assert "works" in line, "script does not call %s working: %r" % (name, line)


def test_the_two_implementations_agree_on_the_loop():
    a = app_print()
    s = sh_check()
    running = a["jobRunning"] == "true"
    assert running == ("RUNNING" in s), "disagree on whether the job is running"

    blocked = a["blocked"] == "true"
    if not running:
        assert blocked == ("blocked from starting again" in s), "disagree on the block"

    log = int(a["logBytes"])
    assert (log > 0) == ("log file    : none" not in s), "disagree on the log"


def test_the_app_never_touches_a_signed_bundle():
    """Patching ShipIt's rpath in place would break the app's seal and macOS
    would refuse to open HP Click. Nothing may write inside /Applications."""
    src = open(SRC).read()
    writes = re.findall(r"(?:removeItem|createFile|write\(to|copyItem|moveItem)[^\n]*", src)
    for w in writes:
        assert "/Applications" not in w, "writes into an app bundle: %s" % w
        assert "kCacheDir" in w or "kLogPath" in w, "unexpected write target: %s" % w


def test_it_only_claims_stock_hp_click():
    """Grafts are com.hp.hpclick.arm64 with the updater patched out; flagging
    their ShipIt binary would be a false alarm."""
    src = open(SRC).read()
    assert 'CFBundleIdentifier") == "com.hp.hpclick"' in src
