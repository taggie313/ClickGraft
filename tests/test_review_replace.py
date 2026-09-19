"""
tests/test_review_replace.py — Review names what a new copy replaces.

The copy's name is fixed, so every build replaces the one made before it, and
up to 1.5.7 Review said only "Replacing". A T-series owner whose stock 4.8.117
had auto-updated to 4.8.118 would have swapped a copy that prints to their
plotter for one that can't. These tests cover what the backend reports about
the copy at the output path, and that `agent build` refuses to replace one
that is open, or that supports printers the new copy won't, without the flag
Review passes only after a deliberate tick.

What this proves: the data Review renders, and the backend's own refusal. What
it does not: that the Swift screen draws it. That needs a screenshot of the
running app; the checks at the bottom only confirm the Swift source still has
a sentence for every fix the backend can name, and passes the same flag.

No HP Click is launched or needed: the bundles are fake trees in a temp folder,
Info.plist and printersValidate.json only, and no process is ever signalled.
Target: Python 3.9+
"""

import io
import json
import os
import plistlib
import re
import shutil
import tempfile
from contextlib import redirect_stdout

import pytest

from clickgraft import agent, graftable, manifest_guard
from clickgraft.manifest import ManifestManager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWIFT = os.path.join(ROOT, "packaging", "ClickGraft.swift")

T_SERIES = ["HP DesignJet T310 24-in", "HP DesignJet T750 36-in"]
COMMON = ["HP DesignJet T730", "HP DesignJet Z9dr 44in"]


@pytest.fixture
def tmp():
    d = tempfile.mkdtemp(prefix="cg-review-")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _bundle(parent, name, version, printers, bundle_id="com.hp.hpclick"):
    """A fake .app: Info.plist, and printersValidate.json when printers is a list.

    printers=None writes no list; a str is written as-is, to make it unreadable.
    """
    app = os.path.join(parent, name)
    res = os.path.join(app, "Contents", "Resources")
    os.makedirs(res)
    with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
        plistlib.dump({"CFBundleShortVersionString": version, "CFBundleVersion": version,
                       "CFBundleIdentifier": bundle_id}, f)
    if isinstance(printers, list):
        with open(os.path.join(res, "printersValidate.json"), "w") as f:
            json.dump([{"name": n, "nameExactMatch": True} for n in printers], f)
    elif isinstance(printers, str):
        with open(os.path.join(res, "printersValidate.json"), "w") as f:
            f.write(printers)
    return app


def _copy(parent, version, printers):
    return _bundle(parent, agent.APP_NAME, version, printers, bundle_id=agent.COPY_BUNDLE_ID)


def _source(parent, version, printers):
    return _bundle(parent, f"{version} HP Click.app", version, printers)


# --- what the backend reports about the copy it would replace ---------------

def test_reports_the_version_the_copy_was_made_from_and_printers_lost(tmp):
    old = _copy(tmp, "4.8.117", T_SERIES + COMMON)
    new = _source(tmp, "4.8.118", COMMON)
    r = agent.existing_copy(old, new, ps_output="")
    assert r["version"] == "4.8.117"
    assert r["source_version"] == "4.8.118"
    assert r["made_by_clickgraft"] is True
    assert r["printers_lost"] == T_SERIES
    assert r["open_pids"] == []


def test_nothing_lost_when_the_new_source_lists_every_printer(tmp):
    old = _copy(tmp, "4.8.118", COMMON)
    new = _source(tmp, "4.8.117", T_SERIES + COMMON)
    r = agent.existing_copy(old, new, ps_output="")
    assert r["printers_lost"] == []


def test_same_version_loses_nothing(tmp):
    old = _copy(tmp, "4.8.117", T_SERIES + COMMON)
    new = _source(tmp, "4.8.117", T_SERIES + COMMON)
    assert agent.existing_copy(old, new, ps_output="")["printers_lost"] == []


@pytest.mark.parametrize("old_list,new_list", [
    ("not json at all", COMMON),          # the copy's list is garbage
    (T_SERIES + COMMON, None),            # the new source has no list
    ("", COMMON),                         # zero-byte placeholder, skipped as absent
    ('{"names": [1, 2]}', COMMON),        # readable JSON, not a list of names
])
def test_unreadable_lists_make_no_claim(tmp, old_list, new_list):
    # None, not []: [] would read as "you lose nothing", which is a claim.
    old = _copy(tmp, "4.8.117", old_list)
    new = _source(tmp, "4.8.118", new_list)
    assert agent.existing_copy(old, new, ps_output="")["printers_lost"] is None
    assert graftable.printers_lost_by_replacing(old, new) is None


def test_no_source_makes_no_printer_claim(tmp):
    old = _copy(tmp, "4.8.117", T_SERIES + COMMON)
    r = agent.existing_copy(old, None, ps_output="")
    assert r["printers_lost"] is None and r["version"] == "4.8.117"


def test_nothing_there_means_nothing_to_report(tmp):
    new = _source(tmp, "4.8.118", COMMON)
    assert agent.existing_copy(os.path.join(tmp, agent.APP_NAME), new, ps_output="") is None


def test_unreadable_info_plist_gives_no_version(tmp):
    old = _copy(tmp, "4.8.117", COMMON)
    with open(os.path.join(old, "Contents", "Info.plist"), "wb") as f:
        f.write(b"\x00 not a plist")
    r = agent.existing_copy(old, _source(tmp, "4.8.118", COMMON), ps_output="")
    assert r["version"] == ""
    assert r["made_by_clickgraft"] is False


def test_an_app_clickgraft_did_not_make_is_not_called_a_copy(tmp):
    old = _bundle(tmp, agent.APP_NAME, "4.10.42", COMMON)          # HP's bundle id
    r = agent.existing_copy(old, _source(tmp, "4.8.118", COMMON), ps_output="")
    assert r["made_by_clickgraft"] is False
    assert r["version"] == "4.10.42"


def test_an_open_copy_is_reported_and_only_that_copy(tmp):
    old = _copy(tmp, "4.8.117", COMMON)
    ps = "\n".join([
        f"  4242 {old}/Contents/MacOS/HPClickExe --type=browser",
        f"  4243 {old}/Contents/Frameworks/HP Click Helper.app/Contents/MacOS/HP Click Helper",
        # The stock app beside it, and a sibling whose name only starts the same.
        f"  5000 {os.path.join(tmp, 'HP Click.app')}/Contents/MacOS/HPClickExe",
        f"  5001 {old[:-4]} 2.app/Contents/MacOS/HPClickExe",
    ])
    r = agent.existing_copy(old, _source(tmp, "4.8.118", COMMON), ps_output=ps)
    assert r["open_pids"] == [4242, 4243]


# --- the plan Review reads ----------------------------------------------------

def _run(argv):
    out = io.StringIO()
    with redirect_stdout(out):
        code = agent.main(argv)
    return code, [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


@pytest.fixture
def wired(monkeypatch):
    """agent.main without the Mac: no /Applications write probe, no log file, no
    live process table, and a build and verify that only record being called."""
    manifest = ManifestManager().manifests["4.8.118"]
    calls = {"build": 0, "running": []}

    def fake_build(**kw):
        calls["build"] += 1

    monkeypatch.setattr(agent, "_manifest_for", lambda mm, source: manifest)
    monkeypatch.setattr(agent, "_log_path", lambda: None)
    monkeypatch.setattr(agent, "processes_inside", lambda path, ps=None: calls["running"])
    monkeypatch.setattr(agent, "build_apple_silicon_bundle", fake_build)
    monkeypatch.setattr(agent, "verify_app_bundle", lambda out, manifest=None: (True, {}))
    return calls


def test_plan_carries_what_is_replaced(tmp, wired, monkeypatch):
    old = _copy(tmp, "4.8.117", T_SERIES + COMMON)
    new = _source(tmp, "4.8.118", COMMON)
    monkeypatch.setattr(agent, "resolve_output", lambda: (old, False))
    code, events = _run(["plan", "--source", new, "--out", old])
    assert code == 0
    plan = events[-1]["plan"]
    assert plan["replacing"]["version"] == "4.8.117"
    assert plan["replacing"]["printers_lost"] == T_SERIES
    assert plan["app_version"] == "4.8.118"


def test_plan_says_nothing_is_replaced_when_nothing_is_there(tmp, wired, monkeypatch):
    out = os.path.join(tmp, agent.APP_NAME)
    monkeypatch.setattr(agent, "resolve_output", lambda: (out, False))
    code, events = _run(["plan", "--source", _source(tmp, "4.8.118", COMMON), "--out", out])
    assert code == 0 and events[-1]["plan"]["replacing"] is None


# --- the backend's own refusal ------------------------------------------------

def test_build_refuses_to_replace_an_open_copy(tmp, wired, monkeypatch):
    old = _copy(tmp, "4.8.117", COMMON)
    new = _source(tmp, "4.8.118", COMMON)
    monkeypatch.setattr(agent, "resolve_output", lambda: (old, False))
    wired["running"] = [(4242, f"{old}/Contents/MacOS/HPClickExe")]
    code, events = _run(["build", "--source", new, "--out", old, "--accept-printer-loss"])
    assert code == 1
    assert events[-1]["type"] == "error" and events[-1]["stage"] == "in_use"
    assert events[-1]["pids"] == [4242]
    assert wired["build"] == 0, "the build ran with the old copy open"


def test_build_refuses_to_drop_printers_without_the_flag(tmp, wired, monkeypatch):
    old = _copy(tmp, "4.8.117", T_SERIES + COMMON)
    new = _source(tmp, "4.8.118", COMMON)
    monkeypatch.setattr(agent, "resolve_output", lambda: (old, False))
    code, events = _run(["build", "--source", new, "--out", old])
    assert code == 1
    assert events[-1]["stage"] == "printers_lost"
    assert events[-1]["printers_lost"] == T_SERIES
    assert wired["build"] == 0


def test_build_drops_printers_only_when_told_to(tmp, wired, monkeypatch):
    old = _copy(tmp, "4.8.117", T_SERIES + COMMON)
    new = _source(tmp, "4.8.118", COMMON)
    monkeypatch.setattr(agent, "resolve_output", lambda: (old, False))
    code, events = _run(["build", "--source", new, "--out", old, "--accept-printer-loss"])
    assert code == 0 and events[-1]["type"] == "done"
    assert wired["build"] == 1


def test_build_needs_no_flag_when_nothing_is_lost(tmp, wired, monkeypatch):
    old = _copy(tmp, "4.8.118", COMMON)
    new = _source(tmp, "4.8.117", T_SERIES + COMMON)
    monkeypatch.setattr(agent, "resolve_output", lambda: (old, False))
    code, events = _run(["build", "--source", new, "--out", old])
    assert code == 0 and wired["build"] == 1


def test_unreadable_lists_do_not_block_the_build(tmp, wired, monkeypatch):
    # No claim either way: nothing to confirm, so nothing to refuse.
    old = _copy(tmp, "4.8.117", "garbage")
    new = _source(tmp, "4.8.118", COMMON)
    monkeypatch.setattr(agent, "resolve_output", lambda: (old, False))
    code, _events = _run(["build", "--source", new, "--out", old])
    assert code == 0 and wired["build"] == 1


# --- the small fixes Review lists ---------------------------------------------

def test_fixes_follow_the_manifest():
    mm = ManifestManager()
    assert agent.fixes_for(mm.manifests["4.8.117"]) == [
        "crash_reports", "updater", "startup_error", "snmp_log"]
    assert agent.fixes_for(mm.manifests["4.8.118"]) == [
        "crash_reports", "updater", "startup_error", "snmp_log"]
    # 4.10.42's index.html never loads constants.js, so its manifest no longer
    # patches it, and Review must not claim the fix.
    assert agent.fixes_for(mm.manifests["4.10.42"]) == ["crash_reports", "updater", "snmp_log"]


def test_every_allowed_patch_has_a_point_on_review():
    # A new op must be described before it can be shown: manifest_guard lets
    # it into a manifest, and Review would otherwise leave it off the list of
    # "exactly what will happen".
    missing = sorted({path for path, _op in manifest_guard.ALLOWED_OPS
                      if path not in agent.FIX_FOR_PATH})
    assert missing == []


def test_every_fix_id_has_a_sentence_in_the_swift():
    with open(SWIFT, encoding="utf-8") as f:
        src = f.read()
    m = re.search(r"static let fixPoints: .*?= \[(.*?)\n    \]", src, re.S)
    assert m, "Wizard.fixPoints is gone"
    ids = re.findall(r'\(\s*"([a-z_]+)",', m.group(1))
    assert sorted(ids) == sorted(set(agent.FIX_FOR_PATH.values()))


def test_swift_passes_the_flag_the_backend_checks():
    with open(SWIFT, encoding="utf-8") as f:
        src = f.read()
    assert '"--accept-printer-loss"' in src
    assert '"in_use"' in src and '"printers_lost"' in src
