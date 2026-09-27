"""
tests/test_capability_advice.py — the advice the wizard shows where it used to
name a version and leave the user guessing.

None of these need a Mac, a stock HP Click, or the GUI: the advice is computed
from the recorded table and a printer config file.
Target: Python 3.9+
"""

import json
import os
import tempfile

import pytest

from clickgraft import agent, capabilities


def _config(tmpdir, *product_names):
    path = os.path.join(tmpdir, "printers.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"printers": [{"printerInfo": {"productName": n}} for n in product_names]}, f)
    return path


def test_configured_printers_reads_model_names():
    with tempfile.TemporaryDirectory() as d:
        cfg = _config(d, "HP DesignJet T1600dr PostScript Printer", "HP DesignJet T750 36-in")
        assert agent.configured_printers(cfg) == [
            "HP DesignJet T1600dr PostScript Printer", "HP DesignJet T750 36-in"]


def test_configured_printers_is_empty_when_there_is_no_config():
    assert agent.configured_printers("/nonexistent/printers.json") == []


def test_a_t750_shop_is_sent_to_the_only_version_that_lists_it():
    """4.11.31 dropped the T750, so HP's own native build is the wrong answer.

    This is the case the old panel got wrong: it named a version and left the
    user to work out whether it drove their plotter.
    """
    with tempfile.TemporaryDirectory() as d:
        cfg = _config(d, "HP DesignJet T750 36-in")
        got = agent.capability_advice(this_mac="15.0", config=cfg)
        assert got["recommend"] == "4.8.117"
        assert got["needs_graft"] is True
        assert got["unlisted"] == []
        assert "T750" in got["why"]


def test_a_t1600_shop_is_left_on_hps_own_build():
    with tempfile.TemporaryDirectory() as d:
        cfg = _config(d, "HP DesignJet T1600dr PostScript Printer")
        got = agent.capability_advice(this_mac="15.0", config=cfg)
        assert got["recommend"] == "4.11.31"
        assert got["needs_graft"] is False


def test_a_mixed_shop_gets_the_version_that_covers_both():
    with tempfile.TemporaryDirectory() as d:
        cfg = _config(d, "HP DesignJet T1600dr Printer", "HP DesignJet T750 24-in")
        got = agent.capability_advice(this_mac="15.0", config=cfg)
        assert got["recommend"] == "4.8.117"
        assert got["unlisted"] == []


def test_a_printer_no_release_lists_is_named_rather_than_hidden():
    with tempfile.TemporaryDirectory() as d:
        cfg = _config(d, "HP DesignJet T1600dr Printer", "Epson SureColor P900")
        got = agent.capability_advice(this_mac="15.0", config=cfg)
        assert got["recommend"]
        assert "Epson SureColor P900" in got["unlisted"], \
            "a printer the answer cannot drive must be said, not dropped"


def test_no_printers_configured_still_advises():
    with tempfile.TemporaryDirectory() as d:
        cfg = _config(d)
        got = agent.capability_advice(this_mac="15.0", config=cfg)
        assert got["recommend"], "with no printer to satisfy, the newest fitting release"
        assert got["printers"] == []


def test_advice_never_claims_a_floor_was_tested():
    with tempfile.TemporaryDirectory() as d:
        cfg = _config(d, "HP DesignJet T730 24-in")
        got = agent.capability_advice(this_mac="15.0", config=cfg)
        assert got["floor_tested"] is False


def test_advice_is_none_without_a_table(monkeypatch):
    """No table means the panel is left exactly as it was."""
    monkeypatch.setattr(capabilities, "recorded", lambda version=None: [])
    assert agent.capability_advice() is None


def test_an_intel_mac_is_never_told_hp_built_an_intel_app_for_apple_silicon():
    """needs_graft is False for every release on an Intel Mac.

    Read as "HP ships this natively" it told an Intel Mac with a T750 that
    4.8.117 -- an Intel-only build -- was built for Apple Silicon. Intel Macs run
    macOS 13 through 15, so that panel was reachable.
    """
    got = capabilities.recommend(printer="HP DesignJet T750 36-in", macos="13.0",
                                 apple_silicon=False)
    assert got["version"] == "4.8.117"
    assert got["needs_graft"] is False, "nothing to translate on an Intel Mac"
    assert got["hp_native"] is False, "and HP did not build 4.8.117 for Apple Silicon"


def test_hp_native_is_true_only_for_the_build_hp_compiled():
    on_silicon = capabilities.recommend(printer="HP DesignJet T1600dr Printer",
                                        macos="15.0", apple_silicon=True)
    assert on_silicon["version"] == "4.11.31"
    assert on_silicon["hp_native"] is True


def test_the_recommendation_is_never_also_listed_as_blocked():
    """A panel rendering both read "Run 4.11.31" above "4.11.31 - does not list ..."."""
    got = capabilities.recommend(
        printer=["HP DesignJet T1600dr Printer", "Epson SureColor P900"],
        macos="15.0", apple_silicon=True)
    assert got["version"]
    assert all(v != got["version"] for v, _ in got["blocked"])
    assert "Epson SureColor P900" in got["unlisted"], "still said, just not as a reject"


def test_two_printers_with_no_answer_blames_the_printers_not_the_mac():
    got = capabilities.recommend(printer=["HP DesignJet T750 36-in",
                                          "HP DesignJet T310 24-in"], macos="10.14")
    assert got["version"] is None
    assert "T750" in got["why"] and "T310" in got["why"]
    assert got["why"] != "no recorded release runs on macOS 10.14"


def _env_lines(monkeypatch, table_bytes):
    """Run `agent env` with a given capabilities.json and count what it emitted."""
    import contextlib, io, pathlib
    real = pathlib.Path(capabilities.__file__).parent / "data" / "capabilities.json"
    backup = real.read_bytes()
    buf, raised = io.StringIO(), None
    try:
        real.write_bytes(table_bytes)
        try:
            with contextlib.redirect_stdout(buf):
                agent.main(["env"])
        except Exception as e:                                     # noqa: BLE001
            raised = f"{type(e).__name__}: {e}"
    finally:
        real.write_bytes(backup)
    return raised, [l for l in buf.getvalue().strip().splitlines() if l]


@pytest.mark.parametrize("label,blob", [
    ("not JSON at all", b"{ this is not json"),
    ("rows missing every key", b'{"reference_version":"4.8.117","versions":[{"nope":1}]}'),
    ("versions is not a list", b'{"reference_version":"4.8.117","versions":"oops"}'),
    ("a field has the wrong type", b'{"reference_version":"4.8.117","versions":[{"version":"4.8.117","declared_floor":12}]}'),
])
def test_a_bad_table_never_costs_the_wizard_its_first_screen(monkeypatch, label, blob):
    """candidates() builds the FIRST screen, so nothing here may raise.

    An exception escapes main() before a single line is emitted and the wizard
    shows "couldn't start", with no way even to choose a bundle -- for a panel
    that is only ever one extra sentence. The "versions is not a list" case did
    exactly that until 27 Sep 2026.
    """
    raised, lines = _env_lines(monkeypatch, blob)
    assert raised is None, f"{label}: {raised}"
    assert len(lines) == 1, f"{label}: emitted {len(lines)} lines, wanted exactly 1"
    payload = json.loads(lines[0])
    assert payload["type"] == "env"
    assert "candidates" in payload


def test_the_advice_is_simply_absent_when_the_table_is_bad():
    """And the panels fall back to the wording they had before the advice existed."""
    import contextlib, io, pathlib
    real = pathlib.Path(capabilities.__file__).parent / "data" / "capabilities.json"
    backup = real.read_bytes()
    try:
        real.write_bytes(b'{"reference_version":"4.8.117","versions":"oops"}')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            agent.main(["env"])
        payload = json.loads(buf.getvalue().strip().splitlines()[0])
    finally:
        real.write_bytes(backup)
    for entry in payload["candidates"]:
        assert "advice" not in entry, "advice must be omitted, not half-filled"


def test_the_overview_is_actually_present_when_the_table_is_fine():
    """The guard around capability_overview() returns None on ANY failure, which
    means a bug inside it disables the reference screen silently and forever.

    That happened while writing it: agent.py imports macos_floor's names rather
    than the module, so a call through `macos_floor.` raised NameError and the
    guard turned it into "no table", with nothing to see. This test is the alarm.
    """
    import contextlib, io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        agent.main(["env"])
    payload = json.loads(buf.getvalue().strip().splitlines()[0])
    caps = payload.get("capabilities")
    assert caps is not None, "the overview went missing — a guard is hiding a bug"
    assert caps["table"], "the table is empty"
    assert caps["recommended"], "no version recommended"
    assert caps["your_macos"], "this Mac's macOS was not reported"
    assert caps["floor_tested"] is False


def test_every_table_row_carries_somewhere_to_get_it():
    for row in capabilities.table():
        assert row["download"]["zip"].endswith(f"HPClick-{row['version']}.zip")
        assert row["download"]["dmg"].endswith(f"HPClick-{row['version']}.dmg")
        assert row["download"]["zip"].startswith("https://ftp.hp.com/")


def test_download_urls_needs_a_version():
    assert capabilities.download_urls(None) is None
    assert capabilities.download_urls("") is None


def test_a_bad_table_costs_the_overview_but_not_the_screen():
    raised, lines = _env_lines(None, b'{"reference_version":"4.8.117","versions":"oops"}')
    assert raised is None
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["capabilities"] is None, "should degrade to no reference, not half a one"
    assert "candidates" in payload, "and the wizard still gets its first screen"
