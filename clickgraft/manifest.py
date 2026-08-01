"""
clickgraft.manifest — Schema validation and manifest loading.
"""

import json
import os


class ManifestManager:
    def __init__(self, manifests_dir=None):
        if manifests_dir is None:
            # Default to manifests/ directory at root of repository/package
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            manifests_dir = os.path.join(base_dir, "manifests")

        self.manifests_dir = manifests_dir
        self.manifests = {}
        self.load_all_manifests()

    def load_all_manifests(self):
        self.manifests = {}
        if os.path.exists(self.manifests_dir):
            for f in os.listdir(self.manifests_dir):
                if f.endswith(".json"):
                    fp = os.path.join(self.manifests_dir, f)
                    try:
                        with open(fp, "r", encoding="utf-8") as file_obj:
                            data = json.load(file_obj)
                            if "app_version" in data:
                                self.manifests[data["app_version"]] = data
                    except Exception:
                        pass

    def find_manifest(self, app_version=None, asar_sha256=None):
        # Match by asar_sha256 first
        if asar_sha256:
            for m in self.manifests.values():
                if m.get("asar_sha256") == asar_sha256:
                    return m

        # Match by app_version
        if app_version and app_version in self.manifests:
            return self.manifests[app_version]

        return None

    def validate_manifest(self, manifest):
        required_keys = ["app_version", "asar_sha256", "electron_version", "asar_entries", "patches", "required_dylibs", "expected_x86_only"]
        for k in required_keys:
            if k not in manifest:
                raise ValueError(f"Invalid manifest: missing required key '{k}'")

        if "packed" not in manifest["asar_entries"] or "unpacked" not in manifest["asar_entries"]:
            raise ValueError("Invalid manifest: asar_entries must contain 'packed' and 'unpacked' integers")

        return True
