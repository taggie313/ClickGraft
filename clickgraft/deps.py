"""
clickgraft.deps — Dependency management.
Checks Xcode Command Line Tools, fetches/caches Electron runtime zip, and resolves/downloads LGPL dylibs from Homebrew CDN if not installed locally.
Target: Python 3.9+ (Standard Library only)
"""

from contextlib import contextmanager
import fcntl
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import ssl
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
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


# A download fails only when nothing at all arrives for this long, however
# long it takes while bytes keep coming. It is the socket timeout of every
# connect and every read, so no single wait outlasts it, and there is
# deliberately no limit on a whole download.
#
# Measured 22 Sep 2026 on the development Mac: Electron 39.8.4's archive
# (112,031,731 bytes) came from GitHub in 16.6 s and gettext's bottle
# (10,296,282 bytes) from ghcr.io in 4.3 s, and the longest pause between
# reads in either was 0.51 s, so a working connection never comes near it. It
# is minutes rather than seconds for a connection that drops out briefly (Wi-Fi
# changing access point, a hotspot losing signal): the server resends with a
# wait that doubles each time, so a transfer cut for half a minute can stay
# silent for most of a minute more before it carries on by itself.
#
# 1.5.9 had no limit at all, so a connection that went quiet held the build
# for ever. The first fix for that also capped a whole download at five
# minutes, which the 22 Sep 2026 review found stopped the 112 MB Electron
# download on any link slower than about 3 Mbit/s however steadily it was
# arriving, and every retry the same way, because nothing resumes.
STALL_SECONDS = 120

# How long a build waits for another one to finish with the shared cache
# before giving up. It gives up only once the other has shown no progress for
# this long: the holder touches the lock file whenever bytes arrive
# (_note_progress), so a slow download that is still arriving never makes the
# waiting build give up. A holder that stops receiving fails its own download
# within STALL_SECONDS, or a few of them while it connects and follows
# GitHub's redirect to its download host, so five of them means the holder is
# stuck in something other than a download. Until the review the wait reused
# the five-minute download cap.
LOCK_IDLE_SECONDS = 5 * STALL_SECONDS

_SHA256 = re.compile(r"[0-9a-f]{64}")

# What the build holding the cache lock in this thread writes its progress to.
_progress = threading.local()


class DownloadError(OSError):
    """A download that failed, in words the person reading can act on.

    Until the 22 Sep 2026 review a failed download reached the wizard and the
    CLI in urllib's own words ("HTTP Error 404: Not Found", "timed out",
    "<urlopen error ...>"), none of which said which file, from where, or
    what to do about it.
    """


class DownloadGone(ValueError):
    """A file this ClickGraft asks for is no longer there (HTTP 404 or 410).

    Not an OSError, on purpose. Whatever sees an OSError treats it as "the
    network is down, try later", and a missing pinned file never comes back
    by retrying. Measured 22 Sep 2026: with libidn2's pin pointed at a blob
    that does not exist, the release gate's network test
    (tests/test_clickgraft.py) reported "network unavailable: HTTP Error 404:
    Not Found" as a skip, and so passed with a dead pin.
    """


def _duration(seconds):
    if seconds >= 120 and seconds % 60 == 0:
        return f"{seconds // 60:g} minutes"
    return f"{seconds:g} seconds"


def _download_failure(exc, what, url):
    """The DownloadError or DownloadGone to raise for exc, or None.

    None when exc is not a network failure (a full disk, say), so that it is
    raised as it is rather than blamed on the connection.
    """
    host = urllib.parse.urlsplit(url).hostname or url
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (404, 410):
            return DownloadGone(
                f"{what[0].upper()}{what[1:]} is no longer on {host} (HTTP "
                f"{exc.code}). This ClickGraft asks for that exact file, so "
                f"trying again will not help: a newer ClickGraft is needed, "
                f"from clickgraft.elusive.net.")
        return DownloadError(
            f"{host} answered HTTP {exc.code} ({exc.reason}) when asked for "
            f"{what}. Try again later.")
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            reason = f"no answer in {_duration(STALL_SECONDS)}"
        return DownloadError(
            f"Could not reach {host} for {what}: {reason}. Check that this Mac "
            f"is online, then try again.")
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return DownloadError(
            f"The download of {what} stalled: nothing arrived from {host} for "
            f"{_duration(STALL_SECONDS)}. Check this Mac's internet connection, "
            f"then try again.")
    if isinstance(exc, (ConnectionError, ssl.SSLError, http.client.HTTPException)):
        return DownloadError(
            f"The connection to {host} broke during the download of {what} "
            f"({exc}). Try again.")
    return None


@contextmanager
def _explaining_failures(what, url):
    """Turn a failed download inside into a DownloadError or DownloadGone."""
    try:
        yield
    except (OSError, http.client.HTTPException) as exc:
        failure = _download_failure(exc, what, url)
        if failure is None:
            raise
        raise failure from exc


def _note_progress():
    """Tell builds waiting for the cache that this one is still receiving.

    At most once a second (a tenth of LOCK_IDLE_SECONDS when a test shrinks
    it), not once per 64 KB read: Electron's archive took 6,885 reads on 22
    Sep 2026.
    """
    path = getattr(_progress, "lock_path", None)
    now = time.time()
    if path and now - getattr(_progress, "touched", 0.0) >= min(1.0, LOCK_IDLE_SECONDS / 10):
        _progress.touched = now
        try:
            os.utime(path)
        except OSError:
            pass


def _sweep_stale_temporaries(cache_dir):
    """Remove what a build killed mid-download left in the cache.

    Measured 22 Sep 2026: a build SIGKILLed during Electron's download left
    .download-q7yveawj in the cache (as much as 112 MB for Electron), and
    nothing ever removed it. This runs with the cache lock held, and every
    writer takes that lock, so nothing found here is being written by a live
    build. The age rule, untouched for LOCK_IDLE_SECONDS, is a second guard
    for a cache on a network home folder, where flock may not reach another
    Mac: a live Electron download writes to its file every second or so, and
    a bottle (10 MB at most) is held in memory and written to its folder once
    it has all arrived, which at 17 KB/s or faster is inside that limit.
    """
    cutoff = time.time() - LOCK_IDLE_SECONDS
    try:
        names = os.listdir(cache_dir)
    except OSError:
        return
    for name in names:
        if not name.startswith((".download-", ".bottle-")):
            continue
        path = os.path.join(cache_dir, name)
        try:
            st = os.lstat(path)
            if st.st_mtime > cutoff:
                continue
            if stat.S_ISDIR(st.st_mode):
                shutil.rmtree(path)
            else:
                os.unlink(path)
        except OSError:
            pass


@contextmanager
def _cache_lock(cache_dir):
    """Hold the download cache for one fetch.

    Builds into different folders, or the wizard and the CLI at once, share
    ~/.cache/clickgraft. Until the 22 Sep 2026 review each wrote its
    downloads straight into place, so two at once could truncate each
    other's Electron archive. flock is released by the kernel when its holder
    dies, so a killed build never leaves the cache locked, only its temporary
    files, which are swept here.
    """
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, ".download.lock")
    with open(path, "a") as lock:
        started = time.time()
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                try:
                    touched = os.fstat(lock.fileno()).st_mtime
                except OSError:
                    touched = started
                if time.time() - max(started, touched) >= LOCK_IDLE_SECONDS:
                    raise TimeoutError(
                        f"Another ClickGraft build on this Mac is using "
                        f"ClickGraft's download cache and has received nothing "
                        f"for {_duration(LOCK_IDLE_SECONDS)}. Quit it or let it "
                        f"finish, then try again.")
                time.sleep(0.2)
        _progress.lock_path, _progress.touched = path, 0.0
        try:
            _sweep_stale_temporaries(cache_dir)
            yield
        finally:
            _progress.lock_path = None
            fcntl.flock(lock, fcntl.LOCK_UN)


def _response_chunks(response):
    """The body of response as it arrives, however long that takes.

    Each read waits at most the socket timeout urlopen was given
    (STALL_SECONDS has why there is no limit on the whole). A body that ends
    before its Content-Length is a broken connection, and is raised as one
    rather than left to fail its checksum as though the file were wrong.
    """
    read = getattr(response, "read1", response.read)
    while True:
        chunk = read(64 * 1024)
        if not chunk:
            break
        _note_progress()
        yield chunk
    remaining = getattr(response, "length", None)
    if isinstance(remaining, int) and remaining > 0:
        raise ConnectionError(f"it closed with {remaining:,} bytes still to come")


def _atomic_download(url, destination, what, expected=None, validate=None):
    """Download url to destination, which only ever holds complete, checked bytes.

    Until the 22 Sep 2026 review a download was written straight into the
    cache, so an interrupted one left a partial file under the real name. Now
    the bytes go to a temporary file beside it (.download-*) and are renamed
    into place only once they match expected, a SHA-256, and pass validate.
    what names the file in the messages a person reads.
    """
    fd, pending = tempfile.mkstemp(prefix=".download-", dir=os.path.dirname(destination))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "clickgraft/2.0"})
        _note_progress()
        with os.fdopen(fd, "wb") as out, _explaining_failures(what, url):
            with urllib.request.urlopen(req, timeout=STALL_SECONDS) as response:
                for chunk in _response_chunks(response):
                    out.write(chunk)
        if expected:
            got = sha256_of(pending)
            if got != expected:
                raise ValueError(
                    f"The downloaded {what} does not match the SHA-256 this "
                    f"ClickGraft expects ({expected}); it has {got}. Nothing "
                    f"was kept. Try again.")
        if validate:
            validate(pending)
        os.replace(pending, destination)
    finally:
        if os.path.exists(pending):
            os.unlink(pending)


def _electron_digest(path, filename):
    """The SHA-256 that the SHASUMS256.txt at path gives for filename.

    Exactly one well-formed line must name it, or ValueError. 1.5.9 took the
    first line that merely contained the name, and never fetched the file
    again once it was cached, so a truncated copy failed every build until
    someone deleted it by hand.
    """
    with open(path, encoding="utf-8") as source:
        matches = [parts[0].lower() for line in source
                   for parts in [line.split()] if len(parts) == 2
                   and parts[1].lstrip("*") == filename
                   and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0])]
    if len(matches) != 1:
        raise ValueError(f"Missing or invalid SHA-256 for {filename}")
    return matches[0]


def fetch_electron(electron_version, cache_dir=None, sha256=None):
    """
    Downloads electron-v{version}-darwin-arm64.zip and verifies its SHA-256.
    Returns absolute path to cached zip.

    sha256 is the archive's hash pinned in the manifest (electron_sha256).
    Given one, the release's SHASUMS256.txt is neither fetched nor read.
    Without one, as for a development manifest, the archive is checked
    against that file, which is fetched again whenever the cached copy has no
    valid line for this archive. A cached archive that matches is used with
    no network at all.
    """
    if sha256 is not None and not _SHA256.fullmatch(str(sha256)):
        raise ValueError(
            f"The pinned SHA-256 of Electron {electron_version} ({sha256!r}) "
            f"is not a SHA-256: it must be 64 lowercase hex digits.")
    cache_dir = cache_dir or get_cache_dir()
    filename = f"electron-v{electron_version}-darwin-arm64.zip"
    archive = os.path.join(cache_dir, filename)
    sums = os.path.join(cache_dir, f"SHASUMS256-{electron_version}.txt")
    base = f"https://github.com/electron/electron/releases/download/v{electron_version}"
    with _cache_lock(cache_dir):
        try:
            digest = sha256 or _electron_digest(sums, filename)
        except (OSError, ValueError, UnicodeError):
            _atomic_download(base + "/SHASUMS256.txt", sums,
                             f"the published fingerprints of Electron {electron_version} (SHASUMS256.txt)",
                             validate=lambda path: _electron_digest(path, filename))
            digest = _electron_digest(sums, filename)
        if not os.path.isfile(archive) or sha256_of(archive) != digest:
            _atomic_download(base + "/" + filename, archive,
                             f"the Apple Silicon engine (Electron {electron_version})",
                             expected=digest)
        return archive


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
#
# Shipped manifests pin the bottles this rule chose that day (each manifest's
# $bottles_pinned), so their builds never ask Homebrew which to use. The rule
# runs at build time only for a manifest without pins, such as a draft from
# `clickgraft probe`.
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
    through here, so tests can stand in for Homebrew without the network.

    timeout is the socket timeout of each connect and read, not of the whole
    request: a slow body that keeps arriving is read to the end.
    """
    req = urllib.request.Request(url, headers=dict({"User-Agent": "clickgraft/2.0"},
                                                   **(headers or {})))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return b"".join(_response_chunks(resp))


def check_pins(manifest):
    """Raise ValueError, naming the formula and field, for any malformed pin.

    Run before anything is downloaded (choose_bottles is the build's first
    use of a manifest's dependencies), because a malformed pin otherwise
    shows itself only as a checksum "mismatch" on every build, blamed on the
    download. Measured 22 Sep 2026: a bottle url whose blob digest differed
    from its sha256, and an electron_sha256 of "XYZ", were both accepted.
    """
    electron = manifest.get("electron_sha256")
    if electron is not None and not _SHA256.fullmatch(str(electron)):
        raise ValueError(
            f"The manifest's electron_sha256 ({electron!r}) is not a SHA-256: it "
            f"must be 64 lowercase hex digits.")
    infos = manifest.get("required_dylibs", [])
    unpinned = [info.get("name", "?") for info in infos if "bottle" not in info]
    if unpinned and len(unpinned) < len(infos):
        raise ValueError(
            f"The manifest pins some of its libraries but not {', '.join(unpinned)}. "
            f"A pinned manifest must pin every one.")
    for info in infos:
        if "bottle" not in info:
            continue
        formula = info.get("brew_formula") or info.get("name", "?")
        bottle = info["bottle"]
        if not isinstance(bottle, dict):
            raise ValueError(f"The pinned bottle of '{formula}' is not an object.")
        tag = str(bottle.get("tag", ""))
        if bottle_macos(tag) in (None, UNKNOWN_NEWER_MACOS):
            raise ValueError(
                f"The pinned bottle of '{formula}' is for {tag!r}, which is not an "
                f"Apple Silicon macOS this ClickGraft knows.")
        for key in ("sha256", "dylib_sha256"):
            if not _SHA256.fullmatch(str(bottle.get(key, ""))):
                raise ValueError(
                    f"The pinned bottle of '{formula}' has an invalid {key} "
                    f"({bottle.get(key)!r}): it must be 64 lowercase hex digits.")
        url = str(bottle.get("url", ""))
        if not url.startswith("https://ghcr.io/"):
            raise ValueError(
                f"The pinned bottle of '{formula}' must come from Homebrew's "
                f"registry, https://ghcr.io/, not {url!r}.")
        digest = url.rpartition("/blobs/sha256:")[2]
        if "/blobs/sha256:" not in url or digest != bottle["sha256"]:
            raise ValueError(
                f"The pinned bottle of '{formula}' names blob {digest!r} in its url "
                f"but {bottle['sha256']} as its sha256. A registry blob is named "
                f"by its SHA-256, so the two must be the same.")


def choose_bottles(manifest, cache_dir=None, timeout=30):
    """{formula: {"tag", "macos", "url", "sha256"}} for every required dylib.

    Shipped manifests pin bottle identity and need no discovery request. For
    unpinned development manifests, one query per formula runs concurrently (one after
    another they took 5.2s on 22 Sep 2026, and the wizard's Review screen waits
    for this); nothing is downloaded. If Homebrew cannot be reached, a formula
    whose dylib is already in the cache, intact, keeps the bottle it was
    fetched from, so a Mac that has built before can build offline. Raises
    OSError when Homebrew cannot be reached and something is not cached,
    ValueError when a formula has no usable bottle or only one for a macOS this
    ClickGraft does not know, or a pin is malformed (check_pins).
    """
    check_pins(manifest)
    infos = manifest.get("required_dylibs", [])
    if any("bottle" in info for info in infos):
        chosen = {}
        for info in infos:
            bottle = dict(info["bottle"])
            bottle["macos"] = bottle_macos(bottle["tag"])
            chosen[info["brew_formula"]] = bottle
        return chosen

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


def sha256_of(path):
    """SHA-256 of the file at path, in hex. Read a megabyte at a time, so
    Electron's 112 MB archive is never held in memory whole."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# The name build.py imports it by, until it uses sha256_of.
_sha256_of = sha256_of


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
        if meta.get("sha256") != sha256_of(cached):
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

    When the bottle is pinned (dylib_sha256), a local Homebrew or cached
    file is used only if it is byte for byte the pinned library.

    Returns {"path", "source": "homebrew"|"cache"|"download", "bottle_tag",
    "minos"}.
    """
    cache_dir = cache_dir or get_cache_dir()
    with _cache_lock(cache_dir):
        return _resolve_dylib(dylib_info, cache_dir, floor, bottle)


def _resolve_dylib(dylib_info, cache_dir, floor, bottle):
    """resolve_dylib's work, with the cache lock held for all of it, so that
    another build cannot evict or replace the file between its checks."""
    from clickgraft.macos_floor import macho_minimum

    dylib_name = dylib_info["name"]

    def found(path, source, tag=None):
        return {"path": path, "source": source, "bottle_tag": tag,
                "minos": macho_minimum(path)}

    # Every source below is checked for arm64 before it is accepted. This is
    # not belt-and-braces: on an Intel Mac, Homebrew lives at /usr/local and
    # ships x86_64 dylibs, so find_local_brew_dylib() would hand back an
    # x86_64 library to be bundled into an arm64 app. Nothing downstream looks,
    # so the build would "succeed" and produce something that cannot load.
    expected_file = (bottle or dylib_info.get("bottle") or {}).get("dylib_sha256")
    def matches_pin(path):
        return not expected_file or sha256_of(path) == expected_file

    local_path = find_local_brew_dylib(dylib_name)
    if local_path and matches_pin(local_path) and _is_arm64(local_path) and _fits(local_path, floor):
        return found(local_path, "homebrew")

    cached_dylib = os.path.join(cache_dir, dylib_name)
    meta = _cache_entry(dylib_info, cache_dir)
    if meta is not None and matches_pin(cached_dylib) and _is_arm64(cached_dylib) and _fits(cached_dylib, floor):
        return found(cached_dylib, "cache", meta.get("bottle_tag"))
    _evict(cached_dylib)          # altered, truncated, too new, or pre-1.5.9; re-fetch

    brew_formula = dylib_info.get("brew_formula")
    if not brew_formula:
        raise ValueError(f"Cannot download dylib '{dylib_name}': no brew_formula specified in manifest")

    if bottle is None or not bottle.get("url"):
        bottle = choose_bottles({"required_dylibs": [dylib_info]},
                                cache_dir=cache_dir)[brew_formula]
    published_dylib = cached_dylib
    with tempfile.TemporaryDirectory(prefix=".bottle-", dir=cache_dir) as pending:
        return _extract_bottle(dylib_info, pending, published_dylib, floor, bottle)


def _extract_bottle(dylib_info, pending_dir, published_dylib, floor, bottle):
    """Download the bottle into pending_dir, check it, and publish its dylib.

    pending_dir is a temporary folder in the cache (.bottle-*). Until the 22
    Sep 2026 review the tarball and the dylib were written straight into the
    cache, so a download or check that failed part way left them under the
    real names. Now the dylib and its record are renamed to published_dylib
    only once every check has passed.
    """
    from clickgraft.macos_floor import format_version, macho_minimum
    dylib_name = dylib_info["name"]
    brew_formula = dylib_info["brew_formula"]
    pending_dylib = os.path.join(pending_dir, dylib_name)
    bottle_key, bottle_url, expected_sha256 = bottle["tag"], bottle["url"], bottle["sha256"]
    # Download bottle tarball from ghcr.io using Homebrew anonymous bearer token
    tarball_path = os.path.join(pending_dir, f"{brew_formula}.tar.gz")
    what = f"the support file {dylib_name} (Homebrew's {brew_formula} {bottle_key} bottle)"
    with _explaining_failures(what, bottle_url):
        body = _http_get(bottle_url, headers={"Authorization": "Bearer QQ=="},
                         timeout=STALL_SECONDS)
    with open(tarball_path, "wb") as f:
        f.write(body)

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
                with open(pending_dylib, "wb") as out_f:
                    out_f.write(src.read())
                found_extracted = True

    if os.path.exists(tarball_path):
        os.remove(tarball_path)

    if not found_extracted or not os.path.exists(pending_dylib):
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

    if not _is_arm64(pending_dylib):
        got = ", ".join(_archs_of(pending_dylib)) or "nothing readable"
        os.remove(pending_dylib)
        raise ValueError(
            f"The {dylib_name} extracted from '{brew_formula}' is {got}, not "
            f"arm64. ClickGraft picks the arm64 bottle deliberately; refusing "
            f"rather than building an app that cannot load it.")

    if not _fits(pending_dylib, floor):
        got = format_version(macho_minimum(pending_dylib))
        os.remove(pending_dylib)
        raise ValueError(
            f"The {dylib_name} in Homebrew's {bottle_key} bottle of "
            f"'{brew_formula}' "
            + (f"needs macOS {got}, newer than" if got else "does not say which macOS it needs, so it may need newer than")
            + f" the macOS {format_version(floor)} this copy is being made for. "
            f"Refusing rather than make a copy that cannot start on the Macs it "
            f"says it supports.")

    if bottle.get("dylib_sha256") and sha256_of(pending_dylib) != bottle["dylib_sha256"]:
        raise ValueError(f"Extracted {dylib_name} does not match the manifest's pinned hash")
    os.chmod(pending_dylib, 0o755)
    with open(_meta_path(pending_dylib), "w", encoding="utf-8") as f:
        json.dump({"formula": brew_formula,
                   "bottle_tag": bottle_key,
                   "bottle_url": bottle_url,
                   "bottle_sha256": expected_sha256,
                   "sha256": sha256_of(pending_dylib),
                   "minos": format_version(macho_minimum(pending_dylib))}, f, indent=1)
    os.replace(pending_dylib, published_dylib)
    os.replace(_meta_path(pending_dylib), _meta_path(published_dylib))
    return {"path": published_dylib, "source": "download", "bottle_tag": bottle_key,
            "minos": macho_minimum(published_dylib)}


def _archs_of(path):
    from clickgraft.macho import get_archs
    try:
        return get_archs(path)
    except Exception:                                              # noqa: BLE001
        return []


def _is_arm64(path):
    """A dylib bound for an arm64 bundle must actually contain arm64 code."""
    return "arm64" in _archs_of(path)
