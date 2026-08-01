"""
clickgraft.deps — Dependency management.
Checks Xcode Command Line Tools, fetches/caches Electron runtime zip, and resolves/downloads LGPL dylibs from Homebrew CDN if not installed locally.
Target: Python 3.9+ (Standard Library only)
"""

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import urllib.request

REQUIRED_CLT_TOOLS = ["codesign", "install_name_tool", "lipo", "otool", "ditto", "getconf"]


def check_clt():
    """Checks if all required Xcode Command Line Tools are installed."""
    for tool in REQUIRED_CLT_TOOLS:
        if shutil.which(tool) is None:
            return False
    return True


def install_clt_interactive():
    """Triggers xcode-select --install and blocks/polls until Xcode CLT installation completes."""
    try:
        subprocess.run(["xcode-select", "--install"], check=True)
    except subprocess.CalledProcessError:
        pass  # May already be downloading or installed

    # Poll until check_clt returns True
    import time
    while not check_clt():
        time.sleep(2)


def get_cache_dir():
    cache_dir = os.path.expanduser("~/.cache/clickgraft")
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def fetch_electron(electron_version, cache_dir=None):
    """
    Downloads electron-v{version}-darwin-arm64.zip and verifies against SHASUMS256.txt.
    Returns absolute path to cached zip.
    """
    if cache_dir is None:
        cache_dir = get_cache_dir()

    zip_filename = f"electron-v{electron_version}-darwin-arm64.zip"
    cached_zip_path = os.path.join(cache_dir, zip_filename)
    shasums_filename = f"SHASUMS256-{electron_version}.txt"
    cached_shasums_path = os.path.join(cache_dir, shasums_filename)

    base_url = f"https://github.com/electron/electron/releases/download/v{electron_version}"
    zip_url = f"{base_url}/{zip_filename}"
    shasums_url = f"{base_url}/SHASUMS256.txt"

    # Fetch SHASUMS256.txt if not cached
    if not os.path.exists(cached_shasums_path):
        req = urllib.request.Request(shasums_url, headers={"User-Agent": "clickgraft/2.0"})
        with urllib.request.urlopen(req) as resp, open(cached_shasums_path, "wb") as f:
            f.write(resp.read())

    # Find expected hash
    expected_sha256 = None
    with open(cached_shasums_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if zip_filename in line:
                expected_sha256 = line.strip().split()[0]
                break

    if not expected_sha256:
        raise ValueError(f"Could not find SHA-256 for {zip_filename} in downloaded SHASUMS256.txt")

    # Download zip if not cached or hash mismatch
    if os.path.exists(cached_zip_path):
        with open(cached_zip_path, "rb") as f:
            calc_hash = hashlib.sha256(f.read()).hexdigest()
        if calc_hash == expected_sha256:
            return cached_zip_path

    req = urllib.request.Request(zip_url, headers={"User-Agent": "clickgraft/2.0"})
    with urllib.request.urlopen(req) as resp, open(cached_zip_path, "wb") as f:
        f.write(resp.read())

    with open(cached_zip_path, "rb") as f:
        calc_hash = hashlib.sha256(f.read()).hexdigest()

    if calc_hash != expected_sha256:
        os.remove(cached_zip_path)
        raise ValueError(f"Downloaded Electron zip SHA-256 mismatch! {calc_hash} != expected {expected_sha256}")

    return cached_zip_path


def find_local_brew_dylib(dylib_name):
    """Checks local Homebrew installations for dylib."""
    search_paths = [
        f"/opt/homebrew/lib/{dylib_name}",
        f"/usr/local/lib/{dylib_name}"
    ]
    # Check brew --prefix if brew is available
    if shutil.which("brew"):
        try:
            prefix = subprocess.run(["brew", "--prefix"], capture_output=True, text=True).stdout.strip()
            if prefix:
                search_paths.append(os.path.join(prefix, "lib", dylib_name))
                search_paths.append(os.path.join(prefix, "opt", dylib_name.split(".")[0], "lib", dylib_name))
        except Exception:
            pass

    for p in search_paths:
        if os.path.exists(p) and not os.path.isdir(p):
            return p
    return None


def fetch_or_find_dylib(dylib_info, cache_dir=None):
    """
    Locates required dylib locally or fetches arm64 Homebrew bottle from Homebrew's CDN.
    dylib_info is a dict: { "name": "libidn2.0.dylib", "brew_formula": "libidn2", ... }
    """
    if cache_dir is None:
        cache_dir = get_cache_dir()

    dylib_name = dylib_info["name"]

    # Every source below is checked for arm64 before it is accepted. This is
    # not belt-and-braces: on an Intel Mac, Homebrew lives at /usr/local and
    # ships x86_64 dylibs, so find_local_brew_dylib() would hand back an
    # x86_64 library to be bundled into an arm64 app. Nothing downstream looks,
    # so the build would "succeed" and produce something that cannot load.
    local_path = find_local_brew_dylib(dylib_name)
    if local_path and _is_arm64(local_path):
        return local_path

    cached_dylib = os.path.join(cache_dir, dylib_name)
    if os.path.exists(cached_dylib) and _is_arm64(cached_dylib):
        return cached_dylib
    if os.path.exists(cached_dylib):
        os.remove(cached_dylib)        # wrong-arch leftover; re-fetch below

    brew_formula = dylib_info.get("brew_formula")
    if not brew_formula:
        raise ValueError(f"Cannot download dylib '{dylib_name}': no brew_formula specified in manifest")

    # Query Homebrew formula API
    api_url = f"https://formulae.brew.sh/api/formula/{brew_formula}.json"
    req = urllib.request.Request(api_url, headers={"User-Agent": "clickgraft/2.0"})
    with urllib.request.urlopen(req) as resp:
        formula_data = json.loads(resp.read().decode("utf-8"))

    bottles = formula_data.get("bottle", {}).get("stable", {}).get("files", {})
    # Look for arm64 bottle keys
    bottle_key = None
    for k in bottles.keys():
        if k.startswith("arm64_") or k == "all":
            bottle_key = k
            break

    if not bottle_key and bottles:
        bottle_key = list(bottles.keys())[0]

    if not bottle_key:
        raise ValueError(f"No arm64 Homebrew bottle found for formula '{brew_formula}'")

    bottle_info = bottles[bottle_key]
    bottle_url = bottle_info["url"]
    expected_sha256 = bottle_info["sha256"]

    # Download bottle tarball from ghcr.io using Homebrew anonymous bearer token
    req_bottle = urllib.request.Request(
        bottle_url,
        headers={
            "User-Agent": "clickgraft/2.0",
            "Authorization": "Bearer QQ=="
        }
    )
    tarball_path = os.path.join(cache_dir, f"{brew_formula}.tar.gz")
    with urllib.request.urlopen(req_bottle) as resp, open(tarball_path, "wb") as f:
        f.write(resp.read())

    # Verify sha256
    with open(tarball_path, "rb") as f:
        calc_hash = hashlib.sha256(f.read()).hexdigest()

    if calc_hash != expected_sha256:
        os.remove(tarball_path)
        raise ValueError(f"Downloaded bottle tarball SHA-256 mismatch for {brew_formula}: {calc_hash} != {expected_sha256}")

    # Extract the target dylib. Bottles carry both the real file and an
    # unversioned symlink beside it (libnghttp2.dylib -> libnghttp2.14.dylib),
    # so prefer a regular file and resolve a symlink to its target rather than
    # writing out a dangling link.
    found_extracted = False
    dylibs_present = []
    with tarfile.open(tarball_path, "r:gz") as tar:
        members = tar.getmembers()
        dylibs_present = [m.name for m in members if m.name.endswith(".dylib")]

        def _match(m):
            return m.name.endswith(f"/{dylib_name}") or m.name == dylib_name

        target = next((m for m in members if _match(m) and m.isfile()), None)
        if target is None:
            link = next((m for m in members if _match(m) and m.issym()), None)
            if link is not None:
                want = os.path.basename(link.linkname)
                target = next((m for m in members
                               if m.isfile() and os.path.basename(m.name) == want), None)

        if target is not None:
            src = tar.extractfile(target)
            if src:
                with open(cached_dylib, "wb") as out_f:
                    out_f.write(src.read())
                found_extracted = True

    if os.path.exists(tarball_path):
        os.remove(tarball_path)

    if not found_extracted or not os.path.exists(cached_dylib):
        # Name what WAS in there. The failure this replaces said only that
        # extraction failed, which sent everyone looking at their network when
        # the real answer was that Homebrew had moved the library to a
        # different formula and the bottle legitimately no longer contained it.
        inventory = ", ".join(sorted(dylibs_present)) or "no .dylib files at all"
        raise ValueError(
            f"The Homebrew bottle for '{brew_formula}' does not contain "
            f"{dylib_name}. It downloaded and its checksum matched, so this is "
            f"not a network problem: the bottle contains {inventory}. Homebrew "
            f"has probably renamed the formula or bumped the library version, "
            f"and the manifest needs updating.")

    if not _is_arm64(cached_dylib):
        got = ", ".join(_archs_of(cached_dylib)) or "nothing readable"
        os.remove(cached_dylib)
        raise ValueError(
            f"The {dylib_name} extracted from '{brew_formula}' is {got}, not "
            f"arm64. ClickGraft picks the arm64 bottle deliberately; refusing "
            f"rather than building an app that cannot load it.")

    os.chmod(cached_dylib, 0o755)
    return cached_dylib


def _archs_of(path):
    from clickgraft.macho import get_archs
    try:
        return get_archs(path)
    except Exception:                                              # noqa: BLE001
        return []


def _is_arm64(path):
    """A dylib bound for an arm64 bundle must actually contain arm64 code."""
    return "arm64" in _archs_of(path)
