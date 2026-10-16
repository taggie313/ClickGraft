"""The public version-history table — generated, never hand-written.

HP publishes no changelog, so this page is the substitute. That makes two things
load-bearing: every version the tool knows about has to appear (a silently
missing release is worse than no page), and every number on it has to come from
the measured table rather than from prose someone typed.
Target: Python 3.9+
"""

import importlib.util
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEN = os.path.join(ROOT, "site", "deploy", "make-version-history.py")
NOTES = os.path.join(ROOT, "site", "version-notes.json")


def _module():
    spec = importlib.util.spec_from_file_location("mvh", GEN)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _measured():
    with open(os.path.join(ROOT, "clickgraft", "data", "capabilities.json"), encoding="utf-8") as f:
        return {v["version"]: v for v in json.load(f)["versions"]}


def test_every_measured_version_appears():
    """A release missing from the page reads as "HP never shipped one"."""
    out = subprocess.run([sys.executable, GEN], capture_output=True, text=True, check=True).stdout
    for v in _measured():
        assert f"<td>{v}</td>" in out, f"{v} is measured but absent from the page"


def test_the_numbers_come_from_the_measured_table():
    """Not from version-notes.json, which is prose a person wrote.

    Checked per row rather than in aggregate: a page that got one version's
    printer count from another version's row would still pass a totals check.

    What this canNOT prove: that capabilities.json itself is right. The test and
    the generator read the same file, so corrupting a value there changes both
    sides and this stays green -- verified, rather than assumed. Catching a wrong
    MEASUREMENT is measure_capabilities.py's job, re-run from the real bundles.
    What this catches is the generator transcribing the right table wrongly:
    reading the wrong field, or a row from the wrong version. Both were injected
    and both failed here.
    """
    measured = _measured()
    for r in _module().rows():
        m = measured[r["version"]]
        assert r["printers"] == m["printer_count"]
        assert r["electron"] == m["electron"]
        assert r["floor"] == m["declared_floor"]
        assert r["native"] == m["hp_native"]


def test_a_note_for_an_unmeasured_version_is_refused(tmp_path, monkeypatch):
    """The control. The generator must not invent a row it cannot measure.

    Without this the page would happily carry a version whose numbers nobody
    checked, which is exactly the thing it exists to be better than.
    """
    m = _module()
    notes = json.load(open(NOTES, encoding="utf-8"))
    notes["versions"]["9.9.99"] = {"published": "2099-01-01", "note": "does not exist"}
    fake = tmp_path / "version-notes.json"
    fake.write_text(json.dumps(notes), encoding="utf-8")
    monkeypatch.setattr(m, "NOTES", str(fake))
    try:
        m.rows()
    except SystemExit as e:
        assert "9.9.99" in str(e)
    else:
        raise AssertionError("it emitted a row for a version nothing has measured")


def test_notes_do_not_claim_a_version_that_was_never_measured():
    """The same rule against the real file, so the repo cannot drift into it."""
    notes = json.load(open(NOTES, encoding="utf-8"))["versions"]
    unmeasured = sorted(set(notes) - set(_measured()))
    assert not unmeasured, f"version-notes.json describes unmeasured {unmeasured}"
