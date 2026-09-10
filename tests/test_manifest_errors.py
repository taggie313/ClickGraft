"""
tests/test_manifest_errors.py — Tests for ManifestManager error handling and version diagnosis.
"""

import json
import os
import tempfile

from clickgraft.manifest import ManifestManager


def test_valid_manifest_still_loads():
    with tempfile.TemporaryDirectory() as tmp_dir:
        manifest_path = os.path.join(tmp_dir, "9.9.9.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump({"app_version": "9.9.9"}, f)

        mm = ManifestManager(manifests_dir=tmp_dir)
        assert "9.9.9" in mm.manifests
        assert mm.load_errors == {}


def test_malformed_json_is_recorded_not_swallowed():
    with tempfile.TemporaryDirectory() as tmp_dir:
        manifest_path = os.path.join(tmp_dir, "4.8.117.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            f.write('{"app_version": "4.8.117", TRUNCATED')

        mm = ManifestManager(manifests_dir=tmp_dir)
        assert "4.8.117.json" in mm.load_errors
        err_msg = mm.load_errors["4.8.117.json"]
        assert "invalid JSON" in err_msg
        assert "1" in err_msg


def test_one_broken_file_does_not_block_the_others():
    with tempfile.TemporaryDirectory() as tmp_dir:
        valid_path = os.path.join(tmp_dir, "9.9.9.json")
        with open(valid_path, "w", encoding="utf-8") as f:
            json.dump({"app_version": "9.9.9"}, f)

        broken_path = os.path.join(tmp_dir, "4.8.117.json")
        with open(broken_path, "w", encoding="utf-8") as f:
            f.write('{"app_version": "4.8.117", TRUNCATED')

        mm = ManifestManager(manifests_dir=tmp_dir)
        assert "9.9.9" in mm.manifests
        assert "4.8.117.json" in mm.load_errors


def test_missing_app_version_key_is_recorded():
    with tempfile.TemporaryDirectory() as tmp_dir:
        manifest_path = os.path.join(tmp_dir, "no_version.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump({"foo": 1}, f)

        mm = ManifestManager(manifests_dir=tmp_dir)
        assert "no_version.json" in mm.load_errors
        assert "app_version" in mm.load_errors["no_version.json"]


def test_describe_version_returns_none_when_available():
    with tempfile.TemporaryDirectory() as tmp_dir:
        manifest_path = os.path.join(tmp_dir, "9.9.9.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump({"app_version": "9.9.9"}, f)

        mm = ManifestManager(manifests_dir=tmp_dir)
        assert mm.describe_version("9.9.9") is None


def test_describe_version_distinguishes_broken_from_absent():
    with tempfile.TemporaryDirectory() as tmp_dir:
        broken_path = os.path.join(tmp_dir, "4.8.117.json")
        with open(broken_path, "w", encoding="utf-8") as f:
            f.write('{"app_version": "4.8.117", TRUNCATED')

        mm = ManifestManager(manifests_dir=tmp_dir)
        desc_broken = mm.describe_version("4.8.117")
        assert desc_broken.startswith("broken manifest: ")

        desc_absent = mm.describe_version("1.2.3")
        assert desc_absent.startswith("no manifest: ")


def test_load_errors_cleared_on_reload():
    with tempfile.TemporaryDirectory() as tmp_dir:
        path = os.path.join(tmp_dir, "4.8.117.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"app_version": "4.8.117", TRUNCATED')

        mm = ManifestManager(manifests_dir=tmp_dir)
        assert "4.8.117.json" in mm.load_errors

        with open(path, "w", encoding="utf-8") as f:
            json.dump({"app_version": "4.8.117"}, f)

        mm.load_all_manifests()
        assert mm.load_errors == {}
        assert "4.8.117" in mm.manifests


def test_manifests_dir_not_exist():
    with tempfile.TemporaryDirectory() as tmp_dir:
        non_existent = os.path.join(tmp_dir, "does_not_exist")
        mm = ManifestManager(manifests_dir=non_existent)
        assert mm.manifests == {}
        assert mm.load_errors == {}


def test_manifests_dir_empty():
    with tempfile.TemporaryDirectory() as tmp_dir:
        mm = ManifestManager(manifests_dir=tmp_dir)
        assert mm.manifests == {}
        assert mm.load_errors == {}


def test_zero_byte_json_recorded():
    with tempfile.TemporaryDirectory() as tmp_dir:
        path = os.path.join(tmp_dir, "empty.json")
        with open(path, "w", encoding="utf-8"):
            pass
        mm = ManifestManager(manifests_dir=tmp_dir)
        assert "empty.json" in mm.load_errors
        assert "invalid JSON" in mm.load_errors["empty.json"]
        assert "1" in mm.load_errors["empty.json"]


def test_whitespace_json_recorded():
    with tempfile.TemporaryDirectory() as tmp_dir:
        path = os.path.join(tmp_dir, "whitespace.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("   \n\t  ")
        mm = ManifestManager(manifests_dir=tmp_dir)
        assert "whitespace.json" in mm.load_errors
        assert "invalid JSON" in mm.load_errors["whitespace.json"]
        assert "2" in mm.load_errors["whitespace.json"]


def test_json_array_not_object():
    with tempfile.TemporaryDirectory() as tmp_dir:
        path = os.path.join(tmp_dir, "array.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("[]")
        mm = ManifestManager(manifests_dir=tmp_dir)
        assert "array.json" in mm.load_errors
        assert "app_version" in mm.load_errors["array.json"]


def test_directory_named_json():
    with tempfile.TemporaryDirectory() as tmp_dir:
        dir_path = os.path.join(tmp_dir, "weird.json")
        os.mkdir(dir_path)
        mm = ManifestManager(manifests_dir=tmp_dir)
        assert "weird.json" in mm.load_errors
        assert "unreadable" in mm.load_errors["weird.json"]


def test_non_json_files_ignored():
    with tempfile.TemporaryDirectory() as tmp_dir:
        txt_path = os.path.join(tmp_dir, "README.txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("This is not JSON")
        mm = ManifestManager(manifests_dir=tmp_dir)
        assert mm.manifests == {}
        assert mm.load_errors == {}


def test_describe_version_none_and_empty_do_not_raise():
    with tempfile.TemporaryDirectory() as tmp_dir:
        mm = ManifestManager(manifests_dir=tmp_dir)
        desc_none = mm.describe_version(None)
        assert desc_none.startswith("no manifest: ")
        desc_empty = mm.describe_version("")
        assert desc_empty.startswith("no manifest: ")


def test_describe_version_matches_version_in_error_text():
    with tempfile.TemporaryDirectory() as tmp_dir:
        mm = ManifestManager(manifests_dir=tmp_dir)
        mm.load_errors["corrupt.json"] = "unreadable: corrupt version 5.0.0 payload"
        desc = mm.describe_version("5.0.0")
        assert desc.startswith("broken manifest: ")
