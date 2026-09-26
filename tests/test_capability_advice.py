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
        got = agent.capability_advice("/Applications/whatever.app", this_mac="15.0", config=cfg)
        assert got["recommend"] == "4.8.117"
        assert got["needs_graft"] is True
        assert got["unlisted"] == []
        assert "T750" in got["why"]


def test_a_t1600_shop_is_left_on_hps_own_build():
    with tempfile.TemporaryDirectory() as d:
        cfg = _config(d, "HP DesignJet T1600dr PostScript Printer")
        got = agent.capability_advice("/Applications/whatever.app", this_mac="15.0", config=cfg)
        assert got["recommend"] == "4.11.31"
        assert got["needs_graft"] is False


def test_a_mixed_shop_gets_the_version_that_covers_both():
    with tempfile.TemporaryDirectory() as d:
        cfg = _config(d, "HP DesignJet T1600dr Printer", "HP DesignJet T750 24-in")
        got = agent.capability_advice("/Applications/whatever.app", this_mac="15.0", config=cfg)
        assert got["recommend"] == "4.8.117"
        assert got["unlisted"] == []


def test_a_printer_no_release_lists_is_named_rather_than_hidden():
    with tempfile.TemporaryDirectory() as d:
        cfg = _config(d, "HP DesignJet T1600dr Printer", "Epson SureColor P900")
        got = agent.capability_advice("/Applications/whatever.app", this_mac="15.0", config=cfg)
        assert got["recommend"]
        assert "Epson SureColor P900" in got["unlisted"], \
            "a printer the answer cannot drive must be said, not dropped"


def test_no_printers_configured_still_advises():
    with tempfile.TemporaryDirectory() as d:
        cfg = _config(d)
        got = agent.capability_advice("/Applications/whatever.app", this_mac="15.0", config=cfg)
        assert got["recommend"], "with no printer to satisfy, the newest fitting release"
        assert got["printers"] == []


def test_advice_never_claims_a_floor_was_tested():
    with tempfile.TemporaryDirectory() as d:
        cfg = _config(d, "HP DesignJet T730 24-in")
        got = agent.capability_advice("/Applications/whatever.app", this_mac="15.0", config=cfg)
        assert got["floor_tested"] is False


def test_advice_is_none_without_a_table(monkeypatch):
    """No table means the panel is left exactly as it was."""
    monkeypatch.setattr(capabilities, "recorded", lambda version=None: [])
    assert agent.capability_advice("/Applications/whatever.app") is None
