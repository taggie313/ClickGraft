"""R1-R6: every published runtime stays reachable at the URL apps embed.

Each released ClickGraft carries an immutable archive URL and SHA-256.
Publication exchanges the whole of html/, so before 1.8.2 shipping a new
payload took the old one off the site. A review reproduced both halves against
the real publish() on temporary directories:

    same Python re-pin: publication accepted; old pinned hash still available = False
    Python version bump: publication accepted; old runtime URL exists = False

An already-installed interpreter is unaffected; a first install, a cleared
cache or a repair fetch is not.
"""

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "site", "deploy"))

import runtime_store as rs                                        # noqa: E402

LEGACY = "ClickGraft-python-3.13.9.zip"
LEGACY_SHA = "3c9e8391ffd2fedf8f42709c5a092ab67dcfb8b10bc5a44ec19f556e5a56aa57"


def _entry(path, sha, version="3.13.9", releases=("1.8.2",)):
    return {"path": path, "python_version": version, "sha256": sha,
            "releases": list(releases)}


def _artifact(tmp_path, name, body):
    p = tmp_path / name
    p.write_bytes(body)
    import hashlib
    return p, hashlib.sha256(body).hexdigest()


# --- the shipped inventory -------------------------------------------------

def test_the_repository_inventory_names_the_live_pin():
    """The seed has to be the artifact 1.8.0 and 1.8.1 actually embed, or the
    first deploy after this change retires the runtime in use."""
    inv = rs.load(os.path.join(ROOT, "site", "deploy", "runtime-inventory.json"))
    assert LEGACY in inv, inv
    assert inv[LEGACY]["sha256"] == LEGACY_SHA
    with open(os.path.join(ROOT, "packaging", "python-pin.json"), encoding="utf-8") as h:
        pin = json.load(h)
    assert inv[LEGACY]["sha256"] == pin["payload_zip_sha256"]
    assert inv[LEGACY]["path"] == pin["payload_url"].rsplit("/", 1)[-1]
    assert {"1.8.0", "1.8.1"} <= set(inv[LEGACY]["releases"])


# --- what a path is allowed to be ------------------------------------------

@pytest.mark.parametrize("bad", [
    "../escape.zip", "/etc/passwd", "sub/dir.zip", "ClickGraft-python-3.13.9.zip?x=1",
    "https://elsewhere/ClickGraft-python-3.13.9.zip", ".hidden.zip",
    "ClickGraft-python-3.13.9.tar", "notaruntime.zip", "..",
])
def test_a_registered_path_must_be_a_bare_archive_name(bad):
    """It becomes a served URL and a filesystem path. Rejected, not normalised:
    'clean it up and carry on' is how one of those ends up unintended."""
    with pytest.raises(rs.InventoryError):
        rs.validate_entry(_entry(bad, LEGACY_SHA))


@pytest.mark.parametrize("bad", ["", "abc", LEGACY_SHA.upper(), LEGACY_SHA + "0", "z" * 64])
def test_a_hash_must_be_a_lowercase_sha256(bad):
    with pytest.raises(rs.InventoryError):
        rs.validate_entry(_entry(LEGACY, bad))


def test_the_legacy_name_is_accepted_exactly_as_it_is():
    """1.8.0 and 1.8.1 embed it; a stricter scheme cannot retire it."""
    assert rs.validate_entry(_entry(LEGACY, LEGACY_SHA))["path"] == LEGACY


def test_a_future_digest_name_is_accepted():
    name = f"ClickGraft-python-3.13.9-{LEGACY_SHA}.zip"
    assert rs.validate_entry(_entry(name, LEGACY_SHA))["path"] == name


# --- R1/R2/R3: the merge ---------------------------------------------------

def test_r1_publishing_a_second_runtime_keeps_the_first():
    """R1. Both original URLs must still resolve to exactly their pinned bytes."""
    durable = {LEGACY: _entry(LEGACY, LEGACY_SHA, releases=["1.8.1"])}
    newer = "ClickGraft-python-3.14.0-" + "b" * 64 + ".zip"
    merged = rs.merge(durable, {newer: _entry(newer, "b" * 64, "3.14.0", ["1.9.0"])})
    assert set(merged) == {LEGACY, newer}
    assert merged[LEGACY]["sha256"] == LEGACY_SHA


def test_r2_same_python_version_different_bytes_needs_a_different_name():
    """R2. A re-signed payload is different bytes; it may not reuse the name
    apps already trust, and under a digest name both coexist."""
    durable = {LEGACY: _entry(LEGACY, LEGACY_SHA)}
    revised = f"ClickGraft-python-3.13.9-{'c' * 64}.zip"
    merged = rs.merge(durable, {revised: _entry(revised, "c" * 64)})
    assert merged[LEGACY]["sha256"] == LEGACY_SHA
    assert merged[revised]["sha256"] == "c" * 64


def test_r3_a_conflicting_hash_for_an_existing_name_is_refused():
    """R3, the one that matters most: refused BEFORE anything served changes."""
    durable = {LEGACY: _entry(LEGACY, LEGACY_SHA)}
    with pytest.raises(rs.InventoryError, match="cannot change its bytes"):
        rs.merge(durable, {LEGACY: _entry(LEGACY, "d" * 64)})


def test_a_deploy_from_an_older_checkout_cannot_retire_an_artifact():
    """The other direction. Omission is not retirement."""
    durable = {LEGACY: _entry(LEGACY, LEGACY_SHA),
               "ClickGraft-python-3.14.0.zip": _entry("ClickGraft-python-3.14.0.zip",
                                                      "e" * 64, "3.14.0")}
    merged = rs.merge(durable, {LEGACY: _entry(LEGACY, LEGACY_SHA)})
    assert "ClickGraft-python-3.14.0.zip" in merged, "an older checkout retired an artifact"


def test_release_consumers_accumulate():
    durable = {LEGACY: _entry(LEGACY, LEGACY_SHA, releases=["1.8.0"])}
    merged = rs.merge(durable, {LEGACY: _entry(LEGACY, LEGACY_SHA, releases=["1.8.2"])})
    assert merged[LEGACY]["releases"] == ["1.8.0", "1.8.2"]


# --- R5: ingestion never accepts a partial ---------------------------------

def test_r5_ingest_verifies_before_it_stores(tmp_path):
    src, sha = _artifact(tmp_path, "a.zip", b"runtime bytes")
    site = tmp_path / "site"
    stored = rs.ingest(site, src, sha)
    assert stored.exists() and rs._sha256(stored) == sha


def test_r5_ingest_refuses_bytes_that_do_not_match(tmp_path):
    src, _sha = _artifact(tmp_path, "a.zip", b"runtime bytes")
    site = tmp_path / "site"
    with pytest.raises(rs.InventoryError, match="hashes to"):
        rs.ingest(site, src, "f" * 64)
    assert not rs.store_path(site, "f" * 64).exists(), "a refused ingest left a file"


def test_r5_a_corrupt_stored_copy_is_replaced_not_trusted(tmp_path):
    src, sha = _artifact(tmp_path, "a.zip", b"runtime bytes")
    site = tmp_path / "site"
    rs.ingest(site, src, sha)
    rs.store_path(site, sha).write_bytes(b"corrupted in place")
    rs.ingest(site, src, sha)
    assert rs._sha256(rs.store_path(site, sha)) == sha, "a bad stored copy was trusted"


def test_r5_no_part_files_survive_a_successful_ingest(tmp_path):
    src, sha = _artifact(tmp_path, "a.zip", b"runtime bytes")
    site = tmp_path / "site"
    rs.ingest(site, src, sha)
    leftovers = list((site / rs.STORE_DIR).glob("*.part"))
    assert not leftovers, leftovers


# --- R6: refuse, do not half-publish ---------------------------------------

def test_r6_a_missing_stored_artifact_stops_publication(tmp_path):
    site = tmp_path / "site"
    inv = {LEGACY: _entry(LEGACY, LEGACY_SHA)}
    with pytest.raises(rs.InventoryError, match="cannot serve every retained artifact"):
        rs.verify_store(site, inv)


def test_r6_a_corrupt_stored_artifact_stops_publication(tmp_path):
    src, sha = _artifact(tmp_path, "a.zip", b"runtime bytes")
    site = tmp_path / "site"
    rs.ingest(site, src, sha)
    rs.store_path(site, sha).write_bytes(b"not the artifact any more")
    with pytest.raises(rs.InventoryError):
        rs.verify_store(site, {LEGACY: _entry(LEGACY, sha)})


def test_verify_store_reports_what_is_retained(tmp_path):
    """So growth is visible at deploy time rather than discovered on a full disk."""
    src, sha = _artifact(tmp_path, "a.zip", b"x" * 5000)
    site = tmp_path / "site"
    rs.ingest(site, src, sha)
    assert rs.verify_store(site, {LEGACY: _entry(LEGACY, sha)}) == 5000


# --- populating a served tree ----------------------------------------------

def test_every_retained_artifact_reaches_the_served_tree(tmp_path):
    site = tmp_path / "site"
    html = tmp_path / "html"
    html.mkdir()
    bodies = {"ClickGraft-python-3.13.9.zip": b"one",
              "ClickGraft-python-3.14.0.zip": b"two"}
    inv = {}
    for name, body in bodies.items():
        src, sha = _artifact(tmp_path, name + ".src", body)
        rs.ingest(site, src, sha)
        inv[name] = _entry(name, sha, "3.14.0" if "3.14" in name else "3.13.9")
    rs.populate(html, site, inv)
    for name, body in bodies.items():
        assert (html / name).read_bytes() == body
        assert inv[name]["sha256"] in (html / f"{name}.sha256").read_text()
    published = rs.load(html / rs.INVENTORY)
    assert set(published) == set(bodies), "the published inventory is not the merged one"


def test_a_round_trip_through_json_is_stable():
    inv = rs.load(os.path.join(ROOT, "site", "deploy", "runtime-inventory.json"))
    import io
    again = json.loads(rs.dump(inv))
    assert again["schema"] == rs.SCHEMA
    assert [a["path"] for a in again["artifacts"]] == sorted(inv)
