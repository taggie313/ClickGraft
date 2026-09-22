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

# vtool reads the minimum macOS each Mach-O declares (clickgraft/macos_floor.py).
# It has shipped with the Command Line Tools since Xcode 11, so requiring it asks
# nothing new of anyone who can build at all.
REQUIRED_CLT_TOOLS = ["codesign", "install_name_tool", "lipo", "otool", "vtool", "ditto", "getconf"]


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


# Which macOS each Homebrew bottle tag is built on, and so the oldest macOS its
# files can be expected to run on.
#
# The bottle is chosen for the OLDEST macOS Homebrew publishes, not the newest
# and not the one this Mac runs. The first key used to win, and on 22 Sep 2026
# that was arm64_golden_gate, built on macOS 27. Matching this Mac would be no
# better: a copy is built on one Mac and handed to others (the wizard's route
# for a Mac managed by IT) and on Intel Macs for Apple Silicon ones, so it must
# not inherit the builder's macOS. That day Homebrew published arm64 bottles of
# libidn2 2.3.8 for golden_gate, tahoe, sequoia, sonoma and ventura, and of
# libunistring 1.4.2, gettext 1.0 and libnghttp2 1.70.0 for golden_gate, tahoe
# and sequoia only. The sequoia files all declare minos 15.0 and do not import
# _strchrnul; libidn2's sonoma and ventura files declare 14.0 and 13.0.
BOTTLE_MACOS = {
    "monterey": (12, 0, 0),
    "ventura": (13, 0, 0),
    "sonoma": (14, 0, 0),
    "sequoia": (15, 0, 0),
    "tahoe": (26, 0, 0),
    "golden_gate": (27, 0, 0),
}

# An arm64 macOS tag this table has not met is a macOS released after this
# ClickGraft, so it sorts after every known one.
UNKNOWN_NEWER_MACOS = (9999, 0, 0)


def bottle_macos(tag):
    """The macOS a bottle tag is built for, or None if it must never be used.

    "all" is a bottle Homebrew publishes once for every platform, so it
    carries no macOS of its own. x86_64 tags have no prefix ("sequoia",
    "x86_64_linux") and arm64_linux is not macOS at all.
    """
    if tag == "all":
        return (0, 0, 0)
    if not tag.startswith("arm64_"):
        return None
    name = tag[len("arm64_"):]
    if name.startswith("linux"):
        return None
    return BOTTLE_MACOS.get(name, UNKNOWN_NEWER_MACOS)


def choose_bottle(files, formula="?"):
    """(tag, macOS version) of the bottle for the oldest macOS on offer.

    files is the formula API's bottle.stable.files. Raises ValueError naming
    what was published when none of it is an arm64 macOS bottle.
    """
    usable = []
    for tag in files:
        v = bottle_macos(tag)
        if v is not None:
            usable.append((v, tag))
    if not usable:
        raise ValueError(
            f"Homebrew publishes no Apple Silicon macOS bottle of '{formula}' "
            f"(it has: {', '.join(sorted(files)) or 'none'}).")
    version, tag = min(usable)
    return tag, version


def _http_get(url, headers=None, timeout=60):
    """GET url and return the body. Everything that talks to Homebrew goes
    through here, so tests can stand in for Homebrew without the network."""
    req = urllib.request.Request(url, headers=dict({"User-Agent": "clickgraft/2.0"},
                                                   **(headers or {})))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def choose_bottles(manifest, cache_dir=None, timeout=30):
    """{formula: {"tag", "macos", "url", "sha256"}} for every required dylib.

    One query to Homebrew's formula API per formula, all at once (one after
    another they took 5.2s on 22 Sep 2026, and the wizard's Review screen waits
    for this); nothing is downloaded. If Homebrew cannot be reached, a formula
    whose dylib is already in the cache, intact, keeps the bottle it was
    fetched from, so a Mac that has built before can build offline. Raises
    OSError when Homebrew cannot be reached and something is not cached,
    ValueError when a formula has no usable bottle or only one for a macOS this
    ClickGraft does not know.
    """
    from concurrent.futures import ThreadPoolExecutor

    if cache_dir is None:
        cache_dir = get_cache_dir()
    infos = {}
    for info in manifest.get("required_dylibs", []):
        if info.get("brew_formula"):
            infos.setdefault(info["brew_formula"], info)

    def query(formula):
        try:
            return _http_get(f"https://formulae.brew.sh/api/formula/{formula}.json",
                             timeout=timeout), None
        except OSError as exc:
            return None, exc

    with ThreadPoolExecutor(max_workers=max(1, len(infos))) as pool:
        answers = dict(zip(infos, pool.map(query, infos)))

    chosen = {}
    for formula, info in infos.items():
        body, unreachable = answers[formula]
        if body is not None:
            files = json.loads(body.decode("utf-8")).get("bottle", {}).get(
                "stable", {}).get("files", {})
            tag, version = choose_bottle(files, formula)
            if version == UNKNOWN_NEWER_MACOS:
                raise ValueError(
                    f"Homebrew now publishes '{formula}' only for a macOS newer "
                    f"than this ClickGraft knows about ({tag}), so it cannot say "
                    f"which macOS the copy would need. A newer ClickGraft will.")
            chosen[formula] = {"tag": tag, "macos": version,
                               "url": files[tag]["url"], "sha256": files[tag]["sha256"]}
            continue
        meta = _cache_entry(info, cache_dir)
        tag = (meta or {}).get("bottle_tag", "")
        if meta is None or bottle_macos(tag) in (None, UNKNOWN_NEWER_MACOS):
            raise OSError(
                f"Could not reach Homebrew to choose the bottle of '{formula}' "
                f"({unreachable}), and ClickGraft's cache has no checked copy of "
                f"{info['name']} from an earlier build to use instead.")
        chosen[formula] = {"tag": tag, "macos": bottle_macos(tag),
                           "url": meta.get("bottle_url", ""),
                           "sha256": meta.get("bottle_sha256", "")}
    return chosen


def _sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _meta_path(cached_dylib):
    return cached_dylib + ".clickgraft.json"


def _cache_entry(dylib_info, cache_dir):
    """The cached dylib's record if the file is intact and from this formula.

    Until 1.5.9 a cached file was trusted if its first bytes said arm64
    Mach-O: a libidn2 cut off at 4096 bytes was accepted and bundled. Now the
    SHA-256 of the extracted file is recorded beside it when it is fetched and
    must still match. A cache from before 1.5.9 has no record, so it is fetched
    again once, which is also what replaces the tahoe bottles (minos 26.0) that
    those caches hold.
    """
    cached = os.path.join(cache_dir, dylib_info["name"])
    try:
        with open(_meta_path(cached), "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or not os.path.isfile(cached):
        return None
    if meta.get("formula") != dylib_info.get("brew_formula"):
        return None
    try:
        if meta.get("sha256") != _sha256_of(cached):
            return None
    except OSError:
        return None
    return meta


def _evict(cached_dylib):
    for p in (cached_dylib, _meta_path(cached_dylib)):
        try:
            os.remove(p)
        except OSError:
            pass


def _fits(path, floor):
    """True when path's minimum macOS is known and no newer than floor."""
    if floor is None:
        return True
    from clickgraft.macos_floor import macho_minimum
    v = macho_minimum(path)
    return v is not None and v <= floor


def fetch_or_find_dylib(dylib_info, cache_dir=None, floor=None):
    """Path to an arm64 dylib for dylib_info; see resolve_dylib."""
    return resolve_dylib(dylib_info, cache_dir=cache_dir, floor=floor)["path"]


def resolve_dylib(dylib_info, cache_dir=None, floor=None, bottle=None):
    """
    Locates required dylib locally or fetches arm64 Homebrew bottle from Homebrew's CDN.
    dylib_info is a dict: { "name": "libidn2.0.dylib", "brew_formula": "libidn2", ... }

    floor, a version tuple: every source, local Homebrew, cache and download
    alike, is accepted only if the dylib's own minimum macOS is no newer. A
    local Homebrew library is built for the Mac it was installed on (minos
    26.0 on the development Mac, measured 22 Sep 2026), so it is skipped
    rather than let the copy inherit that Mac's macOS; a cached one is evicted
    and fetched again. None skips the check.

    bottle: this formula's entry from choose_bottles(), so a build uses the
    bottle its floor was worked out from; left out, it is chosen here.

    Returns {"path", "source": "homebrew"|"cache"|"download", "bottle_tag",
    "minos"}.
    """
    from clickgraft.macos_floor import format_version, macho_minimum

    if cache_dir is None:
        cache_dir = get_cache_dir()
    os.makedirs(cache_dir, exist_ok=True)

    dylib_name = dylib_info["name"]

    def found(path, source, tag=None):
        return {"path": path, "source": source, "bottle_tag": tag,
                "minos": macho_minimum(path)}

    # Every source below is checked for arm64 before it is accepted. This is
    # not belt-and-braces: on an Intel Mac, Homebrew lives at /usr/local and
    # ships x86_64 dylibs, so find_local_brew_dylib() would hand back an
    # x86_64 library to be bundled into an arm64 app. Nothing downstream looks,
    # so the build would "succeed" and produce something that cannot load.
    local_path = find_local_brew_dylib(dylib_name)
    if local_path and _is_arm64(local_path) and _fits(local_path, floor):
        return found(local_path, "homebrew")

    cached_dylib = os.path.join(cache_dir, dylib_name)
    meta = _cache_entry(dylib_info, cache_dir)
    if meta is not None and _is_arm64(cached_dylib) and _fits(cached_dylib, floor):
        return found(cached_dylib, "cache", meta.get("bottle_tag"))
    _evict(cached_dylib)          # altered, truncated, too new, or pre-1.5.9; re-fetch

    brew_formula = dylib_info.get("brew_formula")
    if not brew_formula:
        raise ValueError(f"Cannot download dylib '{dylib_name}': no brew_formula specified in manifest")

    if bottle is None or not bottle.get("url"):
        bottle = choose_bottles({"required_dylibs": [dylib_info]},
                                cache_dir=cache_dir)[brew_formula]
    bottle_key = bottle["tag"]
    bottle_url = bottle["url"]
    expected_sha256 = bottle["sha256"]

    # Download bottle tarball from ghcr.io using Homebrew anonymous bearer token
    tarball_path = os.path.join(cache_dir, f"{brew_formula}.tar.gz")
    with open(tarball_path, "wb") as f:
        f.write(_http_get(bottle_url, headers={"Authorization": "Bearer QQ=="}))

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

    if not _fits(cached_dylib, floor):
        got = format_version(macho_minimum(cached_dylib))
        os.remove(cached_dylib)
        raise ValueError(
            f"The {dylib_name} in Homebrew's {bottle_key} bottle of "
            f"'{brew_formula}' "
            + (f"needs macOS {got}, newer than" if got else "does not say which macOS it needs, so it may need newer than")
            + f" the macOS {format_version(floor)} this copy is being made for. "
            f"Refusing rather than make a copy that cannot start on the Macs it "
            f"says it supports.")

    os.chmod(cached_dylib, 0o755)
    with open(_meta_path(cached_dylib), "w", encoding="utf-8") as f:
        json.dump({"formula": brew_formula,
                   "bottle_tag": bottle_key,
                   "bottle_url": bottle_url,
                   "bottle_sha256": expected_sha256,
                   "sha256": _sha256_of(cached_dylib),
                   "minos": format_version(macho_minimum(cached_dylib))}, f, indent=1)
    return found(cached_dylib, "download", bottle_key)


def _archs_of(path):
    from clickgraft.macho import get_archs
    try:
        return get_archs(path)
    except Exception:                                              # noqa: BLE001
        return []


def _is_arm64(path):
    """A dylib bound for an arm64 bundle must actually contain arm64 code."""
    return "arm64" in _archs_of(path)
