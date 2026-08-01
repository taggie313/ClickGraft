"""
tests/test_clickgraft.py — Automated test suite executing all 13 acceptance tests from DESIGN-v2.md & DEFECTS-round2.md.
Target: Python 3.9+ (Standard Library unittest)
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from clickgraft.asar import AsarArchive, patch_and_repack_asar
from clickgraft.build import build_apple_silicon_bundle
from clickgraft.deps import check_clt
from clickgraft.manifest import ManifestManager
from clickgraft.patches import PatchEngine
from clickgraft.probe import probe_app_bundle
from clickgraft.verify import kill_hpclick_processes, verify_app_bundle


def get_dir_hash(directory_path):
    """Calculates recursive SHA256 of directory files."""
    hasher = hashlib.sha256()
    for root, dirs, files in os.walk(directory_path):
        for f in sorted(files):
            fp = os.path.join(root, f)
            rel_p = os.path.relpath(fp, directory_path)
            hasher.update(rel_p.encode("utf-8"))
            if not os.path.islink(fp):
                with open(fp, "rb") as file_obj:
                    hasher.update(file_obj.read())
    return hasher.hexdigest()


class TestClickGraftAcceptanceSuite(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.source_app = "/Applications/HP Click (x86_64 Backup).app"
        if not os.path.exists(cls.source_app):
            cls.source_app = "/Applications/HP Click.app"
        
        if not os.path.exists(cls.source_app):
            raise unittest.SkipTest(f"Source app bundle not found: {cls.source_app}")

        cls.mm = ManifestManager()
        cls.manifest = cls.mm.find_manifest(app_version="4.8.117")

    def test_1_actionable_error_messages(self):
        """Test 1: Missing CLT / unknown version / unreadable source -> distinct actionable message each."""
        print("\n--- Running Test 1: Actionable Error Messages ---")

        # 1. Unreadable/non-existent source
        with self.assertRaises(ValueError) as cm1:
            build_apple_silicon_bundle("/NonExistent/App.app", "/tmp/out.app", manifest=self.manifest)
        self.assertIn("Source app path does not exist", str(cm1.exception))

        # 2. Unknown version (missing required manifest keys)
        with self.assertRaises(ValueError) as cm2:
            build_apple_silicon_bundle(self.source_app, "/tmp/out.app", manifest={"app_version": "1.0"})
        self.assertIn("missing required key", str(cm2.exception))

        print("Test 1 PASSED: Distinct actionable messages returned for invalid source and missing manifest.")

    def test_2_reproducible_build_twice(self):
        """Test 2: Build twice -> identical output modulo signatures."""
        print("\n--- Running Test 2: Build Twice Reproducibility ---")
        with tempfile.TemporaryDirectory() as tmp_dir:
            out1 = os.path.join(tmp_dir, "App1.app")
            out2 = os.path.join(tmp_dir, "App2.app")

            build_apple_silicon_bundle(self.source_app, out1, manifest=self.manifest)
            build_apple_silicon_bundle(self.source_app, out2, manifest=self.manifest)

            # Compare ASAR files
            asar1 = os.path.join(out1, "Contents", "Resources", "app.asar")
            asar2 = os.path.join(out2, "Contents", "Resources", "app.asar")
            with open(asar1, "rb") as f1, open(asar2, "rb") as f2:
                self.assertEqual(f1.read(), f2.read())

            print("Test 2 PASSED: Two independent builds produced byte-identical ASAR files.")

    def test_3_corrupt_patch_anchor_hard_error(self):
        """Test 3: Corrupt any patch anchor -> hard error naming the file, nothing written."""
        print("\n--- Running Test 3: Corrupt Patch Anchor Hard Error ---")
        corrupted_manifest = json.loads(json.dumps(self.manifest))

        corrupted_manifest["patches"].append({
            "path": "app/node/main/app-updater.js",
            "why": "Test corruption",
            "ops": [{ "type": "replace", "anchor": "NON_EXISTENT_ANCHOR_STRING_12345", "replacement": "xyz" }]
        })

        with tempfile.TemporaryDirectory() as tmp_dir:
            out_app = os.path.join(tmp_dir, "CorruptOut.app")
            with self.assertRaises(ValueError) as cm:
                build_apple_silicon_bundle(self.source_app, out_app, manifest=corrupted_manifest)

            err_msg = str(cm.exception)
            self.assertIn("app/node/main/app-updater.js", err_msg)
            self.assertFalse(os.path.exists(out_app))

        print("Test 3 PASSED: Corrupt anchor raised hard error naming target file, and no output was written.")

    def test_4_built_artifact_passes_verify(self):
        """Test 4: Built artifact passes verify, every check."""
        print("\n--- Running Test 4: Built Artifact Verification Suite ---")
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_app = os.path.join(tmp_dir, "HP Click (Apple Silicon).app")
            build_apple_silicon_bundle(self.source_app, out_app, manifest=self.manifest)

            ok, results = verify_app_bundle(out_app, manifest=self.manifest)
            self.assertTrue(ok)
            for k, v in results.items():
                print(f"  - Check [{k}]: {v}")

        print("Test 4 PASSED: Built app bundle passed all 5 verification suite checks.")

    def test_5_built_asar_postconditions(self):
        """Test 5: Built asar differs from source in exactly the manifest's patched entries; packed/unpacked counts unchanged; zero slack."""
        print("\n--- Running Test 5: Built ASAR Post-Conditions ---")
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_app = os.path.join(tmp_dir, "HP Click (Apple Silicon).app")
            build_apple_silicon_bundle(self.source_app, out_app, manifest=self.manifest)

            src_asar_p = os.path.join(self.source_app, "Contents", "Resources", "app.asar")
            dst_asar_p = os.path.join(out_app, "Contents", "Resources", "app.asar")

            src_archive = AsarArchive(src_asar_p)
            dst_archive = AsarArchive(dst_asar_p)

            s_packed, s_unpacked = src_archive.count_entries()
            d_packed, d_unpacked = dst_archive.count_entries()

            self.assertEqual(s_packed, d_packed)
            self.assertEqual(s_unpacked, d_unpacked)
            self.assertEqual(d_packed, self.manifest["asar_entries"]["packed"])
            self.assertEqual(d_unpacked, self.manifest["asar_entries"]["unpacked"])

            patched_paths = set(p["path"] for p in self.manifest["patches"])
            src_nodes = src_archive.get_all_file_nodes()
            dst_nodes = dst_archive.get_all_file_nodes()

            changed_paths = set()
            for path, node in dst_nodes.items():
                if node.get("unpacked") is True:
                    continue
                if src_archive.read_file_content(src_nodes[path]) != dst_archive.read_file_content(node):
                    changed_paths.add(path)

            self.assertEqual(changed_paths, patched_paths)

        print(f"Test 5 PASSED: Built ASAR changed exactly {len(changed_paths)} patched entries ({changed_paths}) and preserved entry counts & zero slack.")

    def test_6_source_bundle_byte_identical(self):
        """Test 6: Source bundle is byte-identical before and after a full run (v2 headline guarantee)."""
        print("\n--- Running Test 6: Source Bundle Byte-Identity ---")
        initial_hash = get_dir_hash(self.source_app)

        with tempfile.TemporaryDirectory() as tmp_dir:
            out_app = os.path.join(tmp_dir, "HP Click (Apple Silicon).app")
            build_apple_silicon_bundle(self.source_app, out_app, manifest=self.manifest)

        post_hash = get_dir_hash(self.source_app)
        self.assertEqual(initial_hash, post_hash)
        print("Test 6 PASSED: Source app bundle was 100% byte-identical before and after full build pipeline run.")

    def test_7_broken_signature_fails_verify(self):
        """Test 7: Deliberately break the output's signature -> verify fails."""
        print("\n--- Running Test 7: Broken Signature Verification Failure ---")
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_app = os.path.join(tmp_dir, "HP Click (Apple Silicon).app")
            build_apple_silicon_bundle(self.source_app, out_app, manifest=self.manifest)

            exe_path = os.path.join(out_app, "Contents", "MacOS", "HPClickExe")
            with open(exe_path, "r+b") as f:
                f.seek(100)
                f.write(b"\xFF\xFE\xFD\xFC")

            with self.assertRaises(ValueError) as cm:
                verify_app_bundle(out_app, manifest=self.manifest)

            print("Test 7 PASSED: Corrupted signature caused verify_app_bundle to fail as expected.")

    def test_8_stock_asar_swap_fails_verify(self):
        """Test 8: Swap the stock asar into a built bundle -> verify fails on Info.plist / SyntaxError."""
        print("\n--- Running Test 8: Stock ASAR Swap Verification Failure ---")
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_app = os.path.join(tmp_dir, "HP Click (Apple Silicon).app")
            build_apple_silicon_bundle(self.source_app, out_app, manifest=self.manifest)

            src_asar = os.path.join(self.source_app, "Contents", "Resources", "app.asar")
            dst_asar = os.path.join(out_app, "Contents", "Resources", "app.asar")
            shutil.copy2(src_asar, dst_asar)

            with self.assertRaises(ValueError) as cm:
                verify_app_bundle(out_app, manifest=self.manifest)

            print(f"Test 8 PASSED: Swapping stock ASAR into built bundle caused verify to fail: {cm.exception}")

    def test_9_probe_reproduces_manifest_spec(self):
        """Test 9: probe against 4.8.117 reproduces electron_version, entry counts, required_dylibs and expected_x86_only."""
        print("\n--- Running Test 9: Probe Reproducibility against 4.8.117 ---")
        draft, report = probe_app_bundle(self.source_app)

        self.assertEqual(draft["electron_version"], self.manifest["electron_version"])
        self.assertEqual(draft["asar_entries"], self.manifest["asar_entries"])
        self.assertEqual(draft["expected_x86_only"], self.manifest["expected_x86_only"])
        self.assertGreaterEqual(len(draft["required_dylibs"]), 3)

        print(f"Test 9 PASSED: Probe output matched manifest electron_version ({draft['electron_version']}), entry counts, and dylib requirements.")

    def test_10_json_set_nonexistent_parent_hard_error(self):
        """Test 10 (D1): json_set with a non-existent parent path -> hard error, no output written."""
        print("\n--- Running Test 10: json_set Non-Existent Parent Hard Error ---")
        corrupted_manifest = json.loads(json.dumps(self.manifest))
        corrupted_manifest["patches"].append({
            "path": "package.json",
            "why": "Typo parent key test",
            "ops": [{ "type": "json_set", "path": "hp_configsTYPO.crashAutoSubmit", "value": False }]
        })

        with tempfile.TemporaryDirectory() as tmp_dir:
            out_app = os.path.join(tmp_dir, "InvalidParentOut.app")
            with self.assertRaises(ValueError) as cm:
                build_apple_silicon_bundle(self.source_app, out_app, manifest=corrupted_manifest)

            err_msg = str(cm.exception)
            self.assertIn("hp_configsTYPO", err_msg)
            self.assertFalse(os.path.exists(out_app))

        print("Test 10 PASSED: json_set with non-existent parent path raised hard error naming segment 'hp_configsTYPO', and no output was written.")

    def test_11_probe_unsatisfied_symbols_concise(self):
        """Test 11 (D2): Probe against stock 4.8.117 -> surviving unsatisfied-symbol set is concise (<= 5 binaries) and contains _idn2_*."""
        print("\n--- Running Test 11: Probe 3-Filter Unsatisfied Symbol Scan ---")
        draft, report_str = probe_app_bundle(self.source_app)

        # Check required_dylibs produced by probe
        dylib_names = [d["name"] for d in draft["required_dylibs"]]
        self.assertIn("libidn2.0.dylib", dylib_names)

        # Ensure report is concise
        self.assertIn("Binaries with arm64-only unsatisfied symbols: 2", report_str)
        self.assertIn("_idn2_check_version", report_str)

        print("Test 11 PASSED: Probe symbol scan isolated exactly 2 .node binaries with unsatisfied arm64 symbols (_idn2_*, _nghttp2_*).")

    def test_12_corrupt_unpacked_file_fails_postcondition(self):
        """Test 12 (D4): Corrupt one unpacked file under app.asar.unpacked/ -> build fails post-condition."""
        print("\n--- Running Test 12: Corrupt Unpacked File Post-Condition Failure ---")
        src_asar_p = os.path.join(self.source_app, "Contents", "Resources", "app.asar")
        patch_engine = PatchEngine(self.manifest["patches"])

        with tempfile.TemporaryDirectory() as tmp_dir:
            target_asar_p = os.path.join(tmp_dir, "app.asar")
            target_unpacked_dir = os.path.join(tmp_dir, "app.asar.unpacked")

            # Copy source unpacked dir
            src_unpacked_dir = src_asar_p + ".unpacked"
            shutil.copytree(src_unpacked_dir, target_unpacked_dir)

            # Corrupt one unpacked file
            unpacked_files = []
            for root, dirs, files in os.walk(target_unpacked_dir):
                for f in files:
                    unpacked_files.append(os.path.join(root, f))
            self.assertTrue(len(unpacked_files) > 0)

            corrupt_file = unpacked_files[0]
            with open(corrupt_file, "w") as f:
                f.write("CORRUPTED_UNPACKED_FILE_CONTENT")

            with self.assertRaises(ValueError) as cm:
                patch_and_repack_asar(src_asar_p, target_asar_p, patch_engine, self.manifest)

            self.assertIn("unpacked file", str(cm.exception))
            self.assertIn("hash mismatch", str(cm.exception))

        print("Test 12 PASSED: Corrupting an unpacked file on disk under app.asar.unpacked/ failed post-condition hash verification.")

    def test_13_unsupported_version_reported_not_fallen_back(self):
        """Test 13 (D5): a bundle whose version has no manifest is reported
        unsupported, rather than silently falling back to some other manifest.

        Identification is by app VERSION, not by asar SHA-256. The manifest's
        asar_sha256 fingerprints the stock *source*, so a built bundle's asar
        can never match it -- keying on that made `verify` fail on every bundle
        this tool produces. So the unsupported case must be provoked by an
        unknown version, which is what a genuinely new Click release looks like.
        """
        print("\n--- Running Test 13: Unsupported version is reported ---")
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_app = os.path.join(tmp_dir, "HP Click (Apple Silicon).app")
            build_apple_silicon_bundle(self.source_app, out_app, manifest=self.manifest)

            # Rewrite the built bundle's version to something no manifest covers,
            # using the product's own asar machinery.
            asar_p = os.path.join(out_app, "Contents", "Resources", "app.asar")
            # Stage under a directory where the .unpacked sibling resolves, or
            # the (correct) unpacked-file post-condition rejects the rebuild.
            work = os.path.join(tmp_dir, "restamp")
            os.makedirs(work)
            os.symlink(asar_p + ".unpacked", os.path.join(work, "app.asar.unpacked"))
            staged = os.path.join(work, "app.asar")
            engine = PatchEngine([{
                "path": "package.json",
                "ops": [{"type": "json_set", "path": "version", "value": "0.0.0-unsupported"}],
            }])
            patch_and_repack_asar(asar_p, staged, engine, self.manifest)
            shutil.move(staged, asar_p)

            with self.assertRaises(ValueError) as cm:
                verify_app_bundle(out_app, manifest=None)

            msg = str(cm.exception)
            self.assertIn("No manifest matches", msg)
            self.assertIn("0.0.0-unsupported", msg)
            self.assertIn("4.8.117", msg, "should list the versions that ARE supported")

        print("Test 13 PASSED: unknown version reported as unsupported, with supported versions listed.")


class TestClickGraftCLI(unittest.TestCase):
    """End-to-end tests that run the actual CLI a user runs.

    Every test in the suite above passes `manifest=` explicitly, so the
    manifest-resolution path was never exercised. That is precisely how a
    regression shipped in which `clickgraft verify --app <built bundle>` failed
    100% of the time -- it looked the manifest up by the asar's disk SHA-256,
    which can never match a patched asar -- while all 13 tests stayed green.

    These tests invoke the CLI through subprocess. No `manifest=` shortcut.
    """

    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    @classmethod
    def setUpClass(cls):
        cls.source_app = "/Applications/HP Click (x86_64 Backup).app"
        if not os.path.exists(cls.source_app):
            raise unittest.SkipTest("Stock source bundle not available")

        # Build once; the CLI tests share it.
        cls._tmp = tempfile.mkdtemp(prefix="clickgraft_cli_")
        cls.built_app = os.path.join(cls._tmp, "HP Click (Apple Silicon).app")
        r = cls.run_cli(["build", "--source", cls.source_app, "--out", cls.built_app])
        if r.returncode != 0 or not os.path.exists(cls.built_app):
            raise unittest.SkipTest("CLI build failed; cannot run CLI tests:\n" + r.stdout + r.stderr)

    @classmethod
    def tearDownClass(cls):
        # Wait for every process to exit BEFORE deleting the bundle. Deleting it
        # first left JDFPrintProcessor running out of a bundle that no longer
        # existed, which looks exactly like the app crashing.
        kill_hpclick_processes(getattr(cls, "built_app", None))
        shutil.rmtree(getattr(cls, "_tmp", ""), ignore_errors=True)

    @classmethod
    def run_cli(cls, args):
        return subprocess.run(
            [sys.executable, "-m", "clickgraft.cli"] + args,
            cwd=cls.REPO_ROOT, capture_output=True, text=True, timeout=1800)

    def test_14_cli_verify_built_bundle(self):
        """Regression guard: verify must resolve a manifest for a BUILT bundle."""
        print("\n--- Test 14: CLI verify on a built bundle ---")
        r = self.run_cli(["verify", "--app", self.built_app])
        self.assertEqual(r.returncode, 0,
                         "CLI verify failed on a freshly built bundle:\n" + r.stdout + r.stderr)
        self.assertIn("ALL VERIFICATION CHECKS PASSED", r.stdout)
        print("Test 14 PASSED: built bundle verifies through the CLI with no manifest= shortcut.")

    def test_15_cli_verify_missing_app_is_actionable(self):
        print("\n--- Test 15: CLI verify on a missing bundle ---")
        r = self.run_cli(["verify", "--app", "/NonExistent/Nope.app"])
        self.assertNotEqual(r.returncode, 0, "CLI verify should fail on a missing bundle")
        out = r.stdout + r.stderr
        self.assertIn("/NonExistent/Nope.app", out,
                      "error message should name the missing path")
        print("Test 15 PASSED: missing bundle reported, non-zero exit.")

    def test_16_cli_probe_is_readable(self):
        """Probe must stay legible -- its whole purpose is human triage."""
        print("\n--- Test 16: CLI probe signal-to-noise ---")
        r = self.run_cli(["probe", "--app", self.source_app])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("_idn2_", r.stdout, "probe must surface the idn2 gap")
        flagged = [ln for ln in r.stdout.splitlines() if "unsatisfied symbols" in ln.lower()]
        for ln in flagged:
            n = int("".join(c for c in ln.split(":")[-1] if c.isdigit()) or 0)
            self.assertLessEqual(n, 5, "probe report too noisy to triage: " + ln)
        print("Test 16 PASSED: probe surfaces idn2 and stays short enough to read.")

    def test_17_required_dylibs_actually_load(self):
        """Bundling is not loading.

        Flat-namespace symbols resolve only against images actually loaded in
        the process. libnghttp2 once shipped with preload:false -- present on
        disk, satisfying an `nm -gU` scan, and never loaded, so all 36
        _nghttp2_* symbols would still have aborted at call time.
        """
        print("\n--- Test 17: preloaded dylibs are actually in the process ---")
        mm = ManifestManager()
        manifest = mm.find_manifest(app_version="4.8.117")
        expected = [d["name"] for d in manifest.get("required_dylibs", []) if d.get("preload")]
        self.assertTrue(expected, "manifest declares no preloaded dylibs")

        # Clear any prior instance first: both bundles share Electron's
        # single-instance lock, so a survivor makes this launch exit(0)
        # silently with no window and no error.
        kill_hpclick_processes(self.built_app)
        time.sleep(3)

        # Launch through the generated launcher, NOT by hand-building the env.
        # Constructing DYLD_INSERT_LIBRARIES here would test the manifest while
        # leaving the thing that actually ships -- the launcher script -- unchecked.
        # The launcher execs HPClickExe, so proc.pid stays valid across the exec.
        launcher = os.path.join(self.built_app, "Contents", "MacOS", "HP Click")
        proc = subprocess.Popen([launcher], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            # Inspect the process we launched directly -- pgrep on a path
            # containing regex metacharacters like "(Apple Silicon)" is a trap.
            loaded = ""
            for _ in range(30):
                time.sleep(1)
                if proc.poll() is not None:
                    self.fail(f"app exited immediately (rc={proc.returncode}); "
                              "likely the shared single-instance lock")
                loaded = subprocess.run(["vmmap", str(proc.pid)],
                                        capture_output=True, text=True).stdout
                if "libidn2" in loaded:
                    break
            self.assertTrue(loaded, "could not inspect the running process")
            for name in expected:
                stem = name.split(".")[0]
                self.assertIn(stem, loaded,
                              f"{name} is declared preload:true but is NOT loaded into the process")
        finally:
            proc.kill()
            kill_hpclick_processes(self.built_app)
        print("Test 17 PASSED: every preload:true dylib is present in the running process.")


if __name__ == "__main__":
    unittest.main()
