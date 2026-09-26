"""
tests/test_capabilities.py — the recorded table, and the advice drawn from it.

The cases that matter are the ones a wrong answer would send someone chasing a
build that cannot exist: a T-series owner on an old macOS, and the printer-name
matching that decides whether they are told so.
Target: Python 3.9+
"""

import json
import os
import plistlib
import shutil
import tempfile

import pytest

from clickgraft import capabilities as C

RECORDED = C.recorded()


def test_table_is_recorded_and_ordered():
    assert RECORDED, "data/capabilities.json is empty; run packaging/measure_capabilities.py"
    versions = [r["version"] for r in RECORDED]
    keyed = sorted(versions, key=lambda v: [int(p) for p in v.split(".")])
    assert versions == keyed, "the table should be written in version order"


def test_no_graft_reached_the_table():
    """A ClickGraft copy would poison every column the table exists to record."""
    for row in RECORDED:
        assert row["exe_archs"] != ["arm64"], f"{row['version']} looks like a ClickGraft output"


def test_both_floors_are_recorded_and_differ():
    """The declared and library floors disagree, and the table must keep both.

    Losing the distinction is what would make runs_on() refuse macOS 12 to a
    build HP ships for macOS 12.
    """
    for row in RECORDED:
        assert row["declared_floor"], f"{row['version']} has no declared floor"
        assert row["measured_floor"], f"{row['version']} has no measured library floor"
    by_version = {r["version"]: r for r in RECORDED}
    if "4.8.117" in by_version:
        row = by_version["4.8.117"]
        assert row["declared_floor"].startswith("12")
        assert row["measured_floor"].startswith("15")


def test_runs_on_uses_the_declared_floor_not_the_library_floor():
    row = C.recorded("4.8.117")
    if row is None:
        pytest.skip("4.8.117 is not recorded")
    # 12.0 declared, 15.0 inside. The declared floor is the enforced one.
    assert C.runs_on(row, (12, 0)) is True
    assert C.runs_on(row, (11, 6)) is False
    assert C.runs_on(row, None) is None or isinstance(C.runs_on(row, None), bool)


def test_runs_on_is_none_when_either_side_is_unknown():
    assert C.runs_on({"declared_floor": ""}, (15, 0)) is None


@pytest.mark.parametrize("query,expected", [
    ("T350", True),      # the five HP dropped are in 4.8.117
    ("T3500", True),     # a different printer that merely starts with the same digits
    ("T730", True),
    ("t720", True),      # case does not matter
    ("HP DesignJet T1600dr Printer", True),
    ("SureColor P900", False),
])
def test_printer_matching_on_the_reference_version(query, expected):
    row = C.recorded("4.8.117")
    if row is None:
        pytest.skip("4.8.117 is not recorded")
    assert C.lists_printer(row, query) is expected


def test_t350_does_not_match_t3500_in_a_version_that_lacks_it():
    """The substring bug this matcher exists to prevent.

    4.6.47 lists T3500 and does NOT list T350. A substring test answers True for
    T350 here, which would tell a T350 owner that a build without their printer
    supports it.
    """
    row = C.recorded("4.6.47")
    if row is None:
        pytest.skip("4.6.47 is not recorded")
    assert C.lists_printer(row, "T3500") is True
    assert C.lists_printer(row, "T350") is False


def test_a_bare_carriage_width_is_not_a_model():
    row = C.recorded("4.8.117")
    if row is None:
        pytest.skip("4.8.117 is not recorded")
    assert C.lists_printer(row, "24") is None
    assert C.lists_printer(row, "36-in") is None


def test_printers_round_trips_through_the_delta():
    """printers() is rebuilt from the reference plus a delta; the count must agree."""
    for row in RECORDED:
        names = C.printers(row)
        if names is None:
            continue
        assert len(names) == row["printer_count"], row["version"]


def test_old_builds_are_not_graftable_and_say_why():
    for version in ("3.7.83", "4.4.59", "4.5.21", "4.6.47"):
        row = C.recorded(version)
        if row is None:
            continue
        assert C.graftable_version(row) is False
        assert row["blockers"], f"{version} should carry its reasons"
        assert any("Electron" in b for b in row["blockers"])


def test_the_native_build_needs_no_graft():
    row = C.recorded("4.11.31")
    if row is None:
        pytest.skip("4.11.31 is not recorded")
    assert row["hp_native"] is True
    assert C.graftable_version(row) is False
    assert "arm64" in row["exe_archs"] and "x86_64" in row["exe_archs"]


def test_rosetta_boundary():
    assert C.rosetta_available((26, 0)) is True
    assert C.rosetta_available((27, 0)) is True      # removed by the upgrade, reinstallable
    assert C.rosetta_available((28, 0)) is False
    assert C.rosetta_available(None) in (True, False, None)


def test_mojave_with_a_supported_printer_gets_the_newest_build_below_the_floor():
    got = C.recommend(printer="T730", macos=(10, 14), apple_silicon=False)
    assert got["version"] == "4.6.47", got
    assert got["needs_graft"] is False
    assert got["needs_rosetta"] is False


def test_mojave_with_a_t_series_printer_is_a_named_dead_end():
    """The case the table exists for: it must explain itself, not just fail."""
    got = C.recommend(printer="T750", macos=(10, 14), apple_silicon=False)
    assert got["version"] is None
    assert "4.8.117" in got["why"]
    assert "12.0" in got["why"]
    assert "T750" in got["why"]
    assert got["blocked"], "every ruled-out release should be listed"


def test_an_unknown_printer_is_not_a_floor_problem():
    got = C.recommend(printer="SureColor P900", macos=(10, 14), apple_silicon=False)
    assert got["version"] is None
    assert "lists" in got["why"]
    assert "12.0" not in got["why"], "this is not a macOS floor dead end"


def test_apple_silicon_without_rosetta_still_gets_the_t_series_via_a_graft():
    got = C.recommend(printer="T750", macos=(28, 0), apple_silicon=True)
    assert got["version"] == "4.8.117"
    assert got["needs_graft"] is True
    assert got["needs_rosetta"] is False


def test_a_listed_printer_on_a_modern_mac_prefers_hps_own_build():
    got = C.recommend(printer="T1600", macos=(15, 0), apple_silicon=True)
    assert got["version"] == "4.11.31"
    assert got["needs_graft"] is False


def test_of_bundle_rejects_nothing_and_reads_a_synthetic_bundle():
    d = tempfile.mkdtemp()
    try:
        app = os.path.join(d, "HP Click.app")
        os.makedirs(os.path.join(app, "Contents", "MacOS"))
        with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleShortVersionString": "9.9.9",
                           "LSMinimumSystemVersion": "13.0"}, f)
        cap = C.of_bundle(app)
        assert cap["version"] == "9.9.9"
        assert cap["declared_floor"].startswith("13")
        assert cap["source"] == "bundle"
        # Nothing to read means "don't know", never a claim.
        assert cap["printer_count"] is None
        assert cap["electron"] is None
        assert C.graftable_version(cap) is None
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_of_bundle_returns_none_for_something_that_is_not_a_bundle():
    assert C.of_bundle("/nonexistent/Nope.app") is None


def test_data_file_is_machine_written():
    with open(os.path.join(os.path.dirname(os.path.abspath(C.__file__)),
                           "data", "capabilities.json"), encoding="utf-8-sig") as f:
        raw = json.load(f)
    assert "measure_capabilities.py" in raw["$comment"], \
        "the table should say how it was produced, so a hand edit is visible"
    assert raw["reference_version"]


def test_a_question_about_10_x_is_a_question_about_an_intel_mac():
    """Asking about Mojave from an M-series Mac must not volunteer Rosetta.

    Apple Silicon starts at macOS 11, so apple_silicon is derived rather than
    read off this machine when the version asked about is older.
    """
    got = C.recommend(printer="T730", macos="10.14")
    assert got["version"] == "4.6.47"
    assert got["needs_rosetta"] is False
    assert got["needs_graft"] is False
    assert "Rosetta" not in got["why"]


def test_macos_accepts_a_string_or_a_short_tuple():
    row = C.recorded("4.8.117")
    if row is None:
        pytest.skip("4.8.117 is not recorded")
    assert C.runs_on(row, "12.0") is True
    assert C.runs_on(row, (12,)) is True
    assert C.runs_on(row, (12, 0)) is True
    assert C.runs_on(row, (12, 0, 0)) is True
    assert C.runs_on(row, "11.7") is False
