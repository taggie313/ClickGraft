#!/usr/bin/env python3
"""Fetch, trim and relocate the Python that ships inside ClickGraft.app.

WHY CLICKGRAFT CARRIES A PYTHON AT ALL. Every other developer tool this project
needed has been removed -- lipo, otool, nm and vtool by clickgraft/macho_read.py,
install_name_tool by macho_write.py, clang by shipping the libpng shim prebuilt.
One is left and it cannot be removed the same way: the backend RUNS on
/usr/bin/python3, which on macOS 27 is not a Python but a 200,560-byte xcrun
shim with 78 hard links, the same inode as clang. There is no system Python
behind it -- /System/Library/Frameworks/Python.framework is gone. So a Mac
without Apple's Command Line Tools cannot start ClickGraft at all, whatever the
code does, and the only fix is to bring an interpreter.

WHICH PYTHON, AND WHY NOT THE SMALLER ONE. python.org's official macOS build:
one upstream, a PSF signature on the installer, a checksum to pin, and
universal2 out of the box -- which the Intel-host cross-build path needs, since
an Intel Mac must still be able to build an Apple Silicon copy.
python-build-standalone is smaller and genuinely relocatable, but it is a
third-party redistribution and ships separate per-architecture builds. For a
project whose reviewability is the point, one upstream with one signature is
worth the megabytes.

WHAT THIS COSTS, MEASURED. The installer is 67 MB and unpacks to 112 MB. Trimmed
to what the backend uses it is 51 MB, which adds about 18 MB to the download:
ClickGraft.zip goes from roughly 770 KB to roughly 18 MB. That is the price, and
it is the whole argument against doing this.

THE TWO THINGS THAT ARE NOT OBVIOUS, both measured on 28 September 2026:

  * python.org's build is NOT relocatable. bin/python3.13 loads
    /Library/Frameworks/Python.framework/.../Python by absolute path, and so do
    the extension modules and libssl -- nine references in all. Moved anywhere
    else it dies before it starts. They are rewritten @loader_path-relative
    here, using clickgraft.macho_write, which is to say using the tool this
    project built so it could stop needing install_name_tool.

  * It loads NO certificates. ssl.create_default_context() comes back with an
    empty store, so every HTTPS download fails CERTIFICATE_VERIFY_FAILED --
    a failure that would only ever appear on a machine that has to download,
    which is a user's Mac and not the one this was built on. deps.https_context()
    handles it by pointing at macOS's own PEM bundle, /etc/ssl/cert.pem.

    python.org's installer solves this by pip-installing certifi. Nothing is
    vendored here: macOS already ships a trust store.

HOW IT IS PINNED. packaging/python-pin.json carries the version, the URL, the
installer's sha256 and a hash over the finished framework. The release tag fixes
the pin, the pin fixes the framework, and packaging/check_release.py refuses a
release whose bundled Python is not the one the pin names -- which is how a
50 MB binary nobody can diff stays honest.

    python3 packaging/fetch_python.py            # ensure it is in the cache
    python3 packaging/fetch_python.py --print-pin  # after changing the version

Target: Python 3.9+ (Standard Library only)
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from clickgraft import macho_write                                 # noqa: E402
from clickgraft.macho import get_load_dylibs, is_macho             # noqa: E402

PIN = os.path.join(ROOT, "packaging", "python-pin.json")
CACHE = os.path.expanduser("~/.cache/clickgraft/python")
ABSOLUTE_PREFIX = "/Library/Frameworks/Python.framework/"

# Removed because the backend never touches it. Measured by running a full
# graft -- download, patch, sign, verify and a smoke launch -- under the
# trimmed interpreter, not by reading an import list: plistlib pulls xml,
# tarfile pulls lzma and bz2, and an import list does not show that.
TRIM_DIRS = [
    "lib/python{v}/test",          # the CPython test suite, 32 MB on its own
    "lib/python{v}/idlelib",
    "lib/python{v}/tkinter",
    "lib/python{v}/turtledemo",
    "lib/python{v}/ensurepip",
    "lib/python{v}/pydoc_data",
    "lib/python{v}/config-{v}-darwin",
    "lib/python{v}/site-packages",
    "Frameworks",                  # Tcl and Tk, 13 MB, for a tkinter that is gone
    "include",
    "share",
]
TRIM_GLOBS = [
    "bin/idle*", "bin/pydoc*", "bin/*-config", "bin/*-intel64",
    "lib/libncurses*", "lib/libform*", "lib/libpanel*", "lib/libmenu*",
    "lib/python{v}/lib-dynload/_tkinter*.so",
    "lib/python{v}/lib-dynload/_test*.so",
    "lib/python{v}/lib-dynload/_ctypes_test*.so",
    "lib/python{v}/lib-dynload/_xxtestfuzz*.so",
    "lib/python{v}/lib-dynload/xxlimited*.so",
    "lib/python{v}/lib-dynload/_curses*.so",
]


def load_pin():
    with open(PIN, encoding="utf-8") as f:
        return json.load(f)


def tree_sha256(folder):
    """One hash over a directory: every path and every byte, in sorted order.

    Symlinks are hashed as their target text rather than followed, because the
    framework's Versions/Current is a symlink and following it would count the
    whole thing twice.
    """
    digest = hashlib.sha256()
    entries = []
    for walk_root, dirs, files in os.walk(folder, followlinks=False):
        dirs.sort()
        for name in sorted(files) + sorted(d for d in dirs
                                           if os.path.islink(os.path.join(walk_root, d))):
            path = os.path.join(walk_root, name)
            entries.append((os.path.relpath(path, folder), path))
    for rel, path in sorted(entries):
        digest.update(rel.encode("utf-8") + b"\0")
        if os.path.islink(path):
            digest.update(b"L" + os.readlink(path).encode("utf-8"))
        else:
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


# Mach-O magics, as clickgraft/macho.py lists them.
_MACHO_MAGICS = (b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce", b"\xcf\xfa\xed\xfe",
                 b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
                 b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca")


def fingerprint(pairs):
    """One hash over (path, contents), for comparing a SHIPPED framework to the pin.

    Two kinds of path are dropped, and the reason matters because it bounds what
    this proves:

      * Anything under Versions/Current. It is a symlink to the real version
        directory, so whether an enumerator descended into it is a fact about
        the enumerator -- and the two sides here are enumerated differently, one
        walking a directory and one reading a ZIP.

      * Every Mach-O. sign_and_notarize.sh re-signs each one with a Developer ID,
        which rewrites its bytes, so a shipped binary legitimately differs from
        the ad-hoc-signed one in the cache. Hashing them would only ever fail.

    So this covers the stdlib -- about 30 MB of .py and .pyc -- and not the
    compiled binaries. Those are tied to the release a different way and a
    stronger one: pkg_sha256 fixes the upstream installer they came from, and
    both the pin and fetch_python.py are recorded sources, so the release tag
    fixes which framework was built and how it was trimmed.
    """
    digest = hashlib.sha256()
    for rel, data in sorted(pairs):
        if "/Versions/Current/" in "/" + rel.replace(os.sep, "/"):
            continue
        if data[:4] in _MACHO_MAGICS:
            continue
        digest.update(rel.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


def fingerprint_dir(folder):
    pairs = []
    for walk_root, _dirs, files in os.walk(folder):
        for name in files:
            path = os.path.join(walk_root, name)
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    pairs.append((os.path.relpath(path, folder), f.read()))
    return fingerprint(pairs)


def _download(url, destination, expected):
    print(f"  downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as response, \
            open(destination, "wb") as out:
        shutil.copyfileobj(response, out)
    got = hashlib.sha256(open(destination, "rb").read()).hexdigest()
    if expected and got != expected:
        os.remove(destination)
        raise SystemExit(f"the installer's sha256 is {got}, the pin says {expected}")
    return got


def _extract(pkg, workdir):
    """Python_Framework.pkg's payload, which is a gzipped cpio archive.

    Not PythonT_Framework.pkg, which is the free-threaded build: it is a second
    complete framework and nothing here wants it.
    """
    expanded = os.path.join(workdir, "expanded")
    shutil.rmtree(expanded, ignore_errors=True)
    subprocess.run(["pkgutil", "--expand", pkg, expanded], check=True, capture_output=True)
    payload = os.path.join(expanded, "Python_Framework.pkg", "Payload")
    if not os.path.exists(payload):
        raise SystemExit(f"no Python_Framework.pkg payload in {pkg}")
    unpacked = os.path.join(workdir, "unpacked")
    shutil.rmtree(unpacked, ignore_errors=True)
    os.makedirs(unpacked)
    with open(payload, "rb") as src:
        gunzip = subprocess.Popen(["gunzip", "-dc"], stdin=src, stdout=subprocess.PIPE)
        subprocess.run(["cpio", "-i", "--quiet"], stdin=gunzip.stdout,
                       cwd=unpacked, check=True, capture_output=True)
        gunzip.wait()
    return unpacked


def trim(framework, version):
    short = ".".join(version.split(".")[:2])
    base = os.path.join(framework, "Versions", short)
    for name in ("Headers", "Resources"):
        target = os.path.join(framework, name)
        if os.path.islink(target) or os.path.exists(target):
            (os.remove if os.path.islink(target) else shutil.rmtree)(target)
    for pattern in TRIM_DIRS:
        shutil.rmtree(os.path.join(base, pattern.format(v=short)), ignore_errors=True)
    import glob
    for pattern in TRIM_GLOBS:
        for path in glob.glob(os.path.join(base, pattern.format(v=short))):
            os.remove(path)
    os.makedirs(os.path.join(base, "lib", f"python{short}", "site-packages"), exist_ok=True)


def relocate(framework):
    """Rewrite every absolute reference to @loader_path-relative.

    @loader_path is the directory holding the REFERENCING binary, for an
    executable as much as a dylib, so one rule covers bin/python3.x,
    lib-dynload/*.so and libssl alike. Every replacement is shorter than the
    absolute path it replaces, so each fits where it already sits.
    """
    rewritten = 0
    for walk_root, _dirs, files in os.walk(framework):
        for name in files:
            path = os.path.join(walk_root, name)
            if os.path.islink(path) or not is_macho(path):
                continue
            changed = False
            deps = get_load_dylibs(path)
            # deps[0] of a dylib is its own LC_ID_DYLIB, which -change never
            # touches. Nothing resolves a library by its id at runtime, so a
            # stale absolute one is harmless today -- and it is exactly the kind
            # of thing that stops being harmless later, so it goes too.
            if deps and deps[0].startswith(ABSOLUTE_PREFIX) and path.endswith(
                    ("dylib", os.path.basename(deps[0]))):
                if macho_write.set_dylib_id(path, "@loader_path/" + os.path.basename(deps[0])):
                    rewritten += 1
                    changed = True
            for dep in deps:
                if not dep.startswith(ABSOLUTE_PREFIX):
                    continue
                target = os.path.join(framework, dep[len(ABSOLUTE_PREFIX):])
                if not os.path.exists(target):
                    raise SystemExit(f"{path} needs {dep}, which the trim removed")
                rel = os.path.relpath(target, os.path.dirname(path))
                if macho_write.change_dylib_path(path, dep, f"@loader_path/{rel}"):
                    rewritten += 1
                    changed = True
            if changed:
                # An edit invalidates the PSF signature, and on Apple Silicon an
                # invalid signature is fatal rather than a warning. The release
                # re-signs the whole app with a Developer ID afterwards.
                subprocess.run(["codesign", "--force", "-s", "-", path],
                               check=True, capture_output=True)
    return rewritten


def ensure(cache=CACHE, say=print):
    """The path to a ready framework, building it once and caching it.

    The hash check is not paranoia about tampering, it is about drift: the cache
    is writable, so anything that RUNS the cached interpreter writes fresh .pyc
    files into it and it stops matching the pin. A build must not then ship a
    framework the release gate will refuse, so a cache that no longer hashes
    right is rebuilt rather than trusted. The copy inside a signed app cannot
    drift, because it cannot be written to.
    """
    pin = load_pin()
    version = pin["version"]
    target = os.path.join(cache, version, "Python.framework")
    if os.path.isdir(target) and tree_sha256(target) == pin.get("framework_sha256"):
        return target

    os.makedirs(cache, exist_ok=True)
    workdir = os.path.join(cache, version)
    os.makedirs(workdir, exist_ok=True)
    pkg = os.path.join(workdir, os.path.basename(pin["url"]))
    if not os.path.exists(pkg) or \
            hashlib.sha256(open(pkg, "rb").read()).hexdigest() != pin["pkg_sha256"]:
        _download(pin["url"], pkg, pin["pkg_sha256"])
    say(f"  installer verified against the pin ({pin['pkg_sha256'][:16]}…)")

    unpacked = _extract(pkg, workdir)
    source = os.path.join(unpacked, "Versions")
    if not os.path.isdir(source):
        raise SystemExit(f"no Versions/ in the extracted payload at {unpacked}")
    shutil.rmtree(target, ignore_errors=True)
    os.makedirs(target)
    for name in os.listdir(unpacked):
        src = os.path.join(unpacked, name)
        dst = os.path.join(target, name)
        if os.path.islink(src):
            os.symlink(os.readlink(src), dst)
        elif os.path.isdir(src):
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst)
    trim(target, version)
    say(f"  trimmed to {subprocess.run(['du', '-sh', target], capture_output=True, text=True).stdout.split()[0]}")
    say(f"  rewrote {relocate(target)} absolute reference(s) to @loader_path")

    got = tree_sha256(target)
    expected = pin.get("framework_sha256")
    if expected and got != expected:
        raise SystemExit(
            f"the finished framework hashes to {got}, the pin says {expected}.\n"
            f"If the version or the trim changed on purpose, re-pin with:\n"
            f"  python3 packaging/fetch_python.py --print-pin")
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--print-pin", action="store_true",
                        help="rebuild ignoring framework_sha256 and print the new one")
    parser.add_argument("--path", action="store_true", help="print the path and nothing else")
    args = parser.parse_args(argv)

    if args.print_pin:
        pin = load_pin()
        pin.pop("framework_sha256", None)
        pin.pop("payload_sha256", None)
        with open(PIN, "w", encoding="utf-8") as f:
            json.dump(pin, f, indent=2)
            f.write("\n")
        framework = ensure()
        pin["framework_sha256"] = tree_sha256(framework)
        pin["payload_sha256"] = fingerprint_dir(framework)
        with open(PIN, "w", encoding="utf-8") as f:
            json.dump(pin, f, indent=2)
            f.write("\n")
        print(f"framework_sha256 {pin['framework_sha256']}")
        print(f"payload_sha256   {pin['payload_sha256']}")
        return 0

    framework = ensure(say=(lambda _line: None) if args.path else print)
    print(framework if args.path else f"ready: {framework}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
