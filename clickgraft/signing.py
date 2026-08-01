"""
clickgraft.signing — Inner-to-outer ad-hoc code signing orchestration.
Ported verbatim from verified reference implementation repack.py.
Includes APPE recursive framework/binary signing pass and hardened runtime entitlements.
Target: Python 3.9+ (Standard Library only)
"""

import os
import tempfile
from clickgraft.macho import get_rpaths, is_macho, run_cmd

ENTITLEMENTS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>com.apple.security.cs.allow-jit</key>
    <true/>
    <key>com.apple.security.cs.allow-unsigned-executable-memory</key>
    <true/>
    <key>com.apple.security.cs.allow-dyld-environment-variables</key>
    <true/>
    <key>com.apple.security.cs.disable-library-validation</key>
    <true/>
</dict>
</plist>
"""


def sign_bundle(app_bundle_path):
    """
    Performs inner-to-outer code signing across all components of the bundle.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        entitlements_path = os.path.join(tmp_dir, "entitlements.plist")
        with open(entitlements_path, "w", encoding="utf-8") as f:
            f.write(ENTITLEMENTS_XML)

        # 1. Sign inner dylibs and .node modules
        inner_dirs = [
            os.path.join(app_bundle_path, "Contents", "Resources", "app", "appData", "macx", "lib"),
            os.path.join(app_bundle_path, "Contents", "Resources", "app", "appData", "macx", "Frameworks"),
            os.path.join(app_bundle_path, "Contents", "Resources", "app.asar.unpacked")
        ]

        for d in inner_dirs:
            if os.path.exists(d):
                for root, dirs, files in os.walk(d):
                    for file_name in files:
                        fp = os.path.join(root, file_name)
                        if file_name.endswith(".dylib") or file_name.endswith(".node") or is_macho(fp):
                            run_cmd(["codesign", "--force", "-s", "-", fp], check=False)

        # 2. APPE recursive-signing pass
        appe_dir = os.path.join(
            app_bundle_path,
            "Contents",
            "Resources",
            "app",
            "appData",
            "macx",
            "bin",
            "APPE",
            "JDFPrintProcessor"
        )
        if os.path.exists(appe_dir):
            for root, dirs, files in os.walk(appe_dir):
                for file_name in files:
                    fp = os.path.join(root, file_name)
                    if is_macho(fp):
                        if ".framework" in fp:
                            rpaths = get_rpaths(fp)
                            if "@loader_path/.." not in rpaths:
                                run_cmd(["install_name_tool", "-add_rpath", "@loader_path/..", fp], check=False)
                        run_cmd(["codesign", "--force", "--deep", "-s", "-", fp], check=False)

        # 3. Top-level frameworks in Contents/Frameworks/
        fw_dir = os.path.join(app_bundle_path, "Contents", "Frameworks")
        if os.path.exists(fw_dir):
            for item in os.listdir(fw_dir):
                item_path = os.path.join(fw_dir, item)
                if item.endswith(".framework"):
                    run_cmd(["codesign", "--force", "--timestamp=none", "-s", "-", item_path], check=False)

        # 4. Helper apps
        if os.path.exists(fw_dir):
            for item in os.listdir(fw_dir):
                item_path = os.path.join(fw_dir, item)
                if item.endswith(".app"):
                    run_cmd(["codesign", "--force", "--options", "runtime", "--entitlements", entitlements_path, "-s", "-", item_path], check=False)

        # 5. Main executables
        exe_path = os.path.join(app_bundle_path, "Contents", "MacOS", "HPClickExe")
        if os.path.exists(exe_path):
            run_cmd(["codesign", "--force", "--options", "runtime", "--entitlements", entitlements_path, "-s", "-", exe_path], check=False)

        main_launcher = os.path.join(app_bundle_path, "Contents", "MacOS", "HP Click")
        if os.path.exists(main_launcher) and is_macho(main_launcher):
            run_cmd(["codesign", "--force", "--options", "runtime", "--entitlements", entitlements_path, "-s", "-", main_launcher], check=False)

        # 6. Outer application bundle
        run_cmd(["codesign", "--force", "--options", "runtime", "--entitlements", entitlements_path, "-s", "-", app_bundle_path], check=False)

    # 7. Clear quarantine attributes
    run_cmd(["xattr", "-dr", "com.apple.quarantine", app_bundle_path], check=False)
