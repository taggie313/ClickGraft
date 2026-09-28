"""The Python ClickGraft fetches, as a framework on disk.

Every other developer tool was removed from what a user needs. This one could
not be: the backend IS Python, and /usr/bin/python3 on macOS 27 is a 200,560-byte
xcrun shim with 78 hard links -- the same inode as clang -- with no system Python
behind it.

These hold the framework itself: pinned, relocatable, able to verify HTTPS, and
signed so it can be loaded. tests/test_python_payload.py holds the other half --
what the app does with it, and every way it refuses the wrong one -- since 1.8.0
stopped carrying the framework and started fetching it.
"""

import importlib.util
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIN = os.path.join(ROOT, "packaging", "python-pin.json")
sys.path.insert(0, os.path.join(ROOT, "packaging"))

import fetch_python                                        # noqa: E402


def _gate():
    spec = importlib.util.spec_from_file_location(
        "cr_bundled", os.path.join(ROOT, "packaging", "check_release.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- the pin ---------------------------------------------------------------

def test_the_pin_says_which_python_and_proves_it():
    pin = json.loads(open(PIN, encoding="utf-8").read())
    for key in ("version", "url", "pkg_sha256", "payload_sha256"):
        assert pin.get(key), f"python-pin.json has no {key}"
    assert pin["version"] in pin["url"], "the URL and the version disagree"
    assert len(pin["pkg_sha256"]) == 64 and len(pin["payload_sha256"]) == 64


# --- what the fingerprint deliberately does and does not cover --------------

def test_fingerprint_ignores_macho_because_signing_rewrites_it():
    """sign_and_notarize.sh re-signs every Mach-O with a Developer ID, which
    changes its bytes. Hashing them would only ever fail."""
    macho = b"\xcf\xfa\xed\xfe" + b"\x00" * 64
    other = b"\xcf\xfa\xed\xff" + b"\x00" * 64
    assert fetch_python.fingerprint([("a/x.dylib", macho)]) == \
        fetch_python.fingerprint([("a/x.dylib", b"\xca\xfe\xba\xbe" + b"\x11" * 32)])
    assert fetch_python.fingerprint([("a/x", other)]) != \
        fetch_python.fingerprint([("a/x", b"different")])


def test_fingerprint_ignores_versions_current():
    """It is a symlink to the real version directory, and whether an enumerator
    descended into it is a fact about the enumerator."""
    payload = [("Versions/3.13/lib/os.py", b"x")]
    doubled = payload + [("Versions/Current/lib/os.py", b"x")]
    assert fetch_python.fingerprint(payload) == fetch_python.fingerprint(doubled)


def test_fingerprint_is_order_independent():
    a = [("b", b"2"), ("a", b"1")]
    assert fetch_python.fingerprint(a) == fetch_python.fingerprint(sorted(a))


# --- the gate ----------------------------------------------------------------
# What the gate now says about the app -- that it ships the pin and carries no
# interpreter -- lives in tests/test_python_payload.py, with the rest of the
# fetch it governs. Nothing here duplicates it.


# --- the cached framework, when there is one --------------------------------

def _cached():
    pin = json.loads(open(PIN, encoding="utf-8").read())
    path = os.path.expanduser(f"~/.cache/clickgraft/python/{pin['version']}/Python.framework")
    return path if os.path.isdir(path) else None


def test_the_cached_framework_matches_its_pin():
    """And ensure() puts it back when it does not.

    The cache is writable, so running the interpreter out of it writes fresh
    .pyc files and the framework drifts from the pin. That is harmless -- the
    copy inside a signed app cannot drift, and ensure() rebuilds a cache that no
    longer hashes right -- but it means this has to ask ensure() first rather
    than trusting whatever is on disk.
    """
    if _cached() is None:
        pytest.skip("no framework in the cache; run packaging/fetch_python.py")
    framework = fetch_python.ensure(say=lambda _line: None)
    pin = json.loads(open(PIN, encoding="utf-8").read())
    assert fetch_python.fingerprint_dir(framework) == pin["payload_sha256"]


def test_the_cached_framework_has_no_absolute_references_left():
    """python.org's build hardcodes /Library/Frameworks/Python.framework/...
    in nine places. Any left behind means an interpreter that dies before it
    starts, anywhere but the path it was built for."""
    framework = _cached()
    if framework is None:
        pytest.skip("no framework in the cache; run packaging/fetch_python.py")
    sys.path.insert(0, ROOT)
    from clickgraft.macho import get_load_dylibs, is_macho

    offenders = []
    for root, _dirs, files in os.walk(framework):
        for name in files:
            path = os.path.join(root, name)
            if os.path.islink(path) or not is_macho(path):
                continue
            for dep in get_load_dylibs(path):
                if dep.startswith(fetch_python.ABSOLUTE_PREFIX):
                    offenders.append((os.path.relpath(path, framework), dep))
    assert not offenders, offenders[:5]


def test_the_cached_framework_actually_runs():
    framework = _cached()
    if framework is None:
        pytest.skip("no framework in the cache; run packaging/fetch_python.py")
    pin = json.loads(open(PIN, encoding="utf-8").read())
    exe = os.path.join(framework, "Versions", "Current", "bin", "python3")
    run = subprocess.run([exe, "-c", "import sys; print(sys.version.split()[0])"],
                         capture_output=True, text=True,
                         env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == pin["version"]


def test_the_cached_framework_can_verify_https():
    """It loads NO certificates of its own: ssl.create_default_context() comes
    back empty, and every download would fail CERTIFICATE_VERIFY_FAILED on a
    user's Mac and nowhere else. deps.https_context() points it at macOS's own
    PEM bundle."""
    framework = _cached()
    if framework is None:
        pytest.skip("no framework in the cache; run packaging/fetch_python.py")
    exe = os.path.join(framework, "Versions", "Current", "bin", "python3")
    run = subprocess.run(
        [exe, "-c",
         "import sys; sys.path.insert(0, %r)\n"
         "from clickgraft.deps import https_context\n"
         "print(https_context().cert_store_stats()['x509'])" % ROOT],
        capture_output=True, text=True,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert run.returncode == 0, run.stderr
    assert int(run.stdout.strip()) > 0, "the bundled Python would trust nothing"


# --- the bug this work surfaced --------------------------------------------

def test_preflight_survives_a_patch_only_manifest(capsys):
    """`clickgraft preflight` printed m['electron_version'] for every manifest.

    A patch_only manifest has none on purpose -- it grafts nothing -- so the
    command died with KeyError from the moment 4.11.31 shipped in 1.7.0. Nothing
    caught it because the wizard talks to the agent and never runs preflight.
    """
    sys.path.insert(0, ROOT)
    from clickgraft.cli import cmd_preflight
    from clickgraft.manifest import ManifestManager

    assert any("electron_version" not in m
               for m in ManifestManager().manifests.values()), \
        "no patch_only manifest present, so this proves nothing"
    cmd_preflight(None)
    out = capsys.readouterr().out
    assert "no graft (patch_only)" in out
    assert "ALL PREFLIGHT CHECKS PASSED" in out


def test_the_payload_build_signs_every_macho_and_the_framework():
    """Signing is what makes the fetched interpreter usable at all.

    Ad-hoc signing does not: python3 dies with a message that never mentions
    signing ("mapped file has no Team ID and is not a platform binary"), because
    under the hardened runtime the support files it loads must carry its own Team
    ID. That holds only because build_payload walks the framework and signs each
    Mach-O with the same Developer ID before sealing the bundle.

    It is also what the app's own check rests on: it runs codesign --verify on
    what it unpacked, and refuses an interpreter macOS will not vouch for
    (tests/test_python_payload.py proves that refusal binds).
    """
    source = open(os.path.join(ROOT, "packaging", "fetch_python.py"),
                  encoding="utf-8").read()
    body = source[source.index("def build_payload("):]
    body = body[:body.index("\ndef ")] if "\ndef " in body else body
    assert "is_macho(path)" in body, "it must find every Mach-O, not a fixed list"
    assert body.count('"--options", "runtime"') >= 2, \
        "each Mach-O and then the framework, both with the hardened runtime"
    assert '"--verify", "--strict"' in body, \
        "an unverifiable payload must fail the build, not a user's fetch"
