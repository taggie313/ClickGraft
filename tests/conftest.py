"""Which ClickGraft the tests are testing, said explicitly.

Until 1.8.2 the behavioural tests froze `dist/ClickGraft.app` into module-level
constants and a skip decorator. The release gate runs pytest BEFORE it compiles
its candidate, so those tests ran against whatever happened to be in `dist/` --
usually the previous release. A Swift change could be checked against an older
executable while the structural tests read newer source, and with no `dist/` at
all the behavioural tests skipped and the gate accepted the run.

So the candidate is now an input:

    --clickgraft-app PATH   the .app to exercise (default: dist/ClickGraft.app)
    --clickgraft-payload P  the pinned archive (default: the cache)
    --release-check         missing prerequisites FAIL instead of skipping

`--release-check` is what the source gate passes. Development runs keep the
skips, because a contributor without a Developer ID or a 17 MB archive should
still be able to run most of the suite -- but a release must never approve
itself by skipping the cases that matter.
"""

import hashlib
import json
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_PIN = os.path.join(ROOT, "packaging", "python-pin.json")


def pytest_addoption(parser):
    group = parser.getgroup("clickgraft")
    group.addoption("--clickgraft-app", action="store", default=None,
                    metavar="PATH",
                    help="the ClickGraft.app to exercise (default dist/ClickGraft.app)")
    group.addoption("--clickgraft-payload", action="store", default=None,
                    metavar="PATH",
                    help="the pinned Python archive (default: the fetch_python cache)")
    group.addoption("--release-check", action="store_true", default=False,
                    help="a missing prerequisite is a failure, not a skip")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "needs_app: exercises a built ClickGraft.app")
    config.addinivalue_line(
        "markers", "needs_payload: exercises the real pinned Python archive")


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pin():
    with open(REPO_PIN, encoding="utf-8") as handle:
        return json.load(handle)


def _missing(request, why):
    """Skip, or fail when this is a release run.

    The whole point of --release-check: 'no candidate, so nothing was tested'
    must not read the same as 'everything passed'.
    """
    if request.config.getoption("--release-check"):
        pytest.fail(f"release check: {why}", pytrace=False)
    pytest.skip(why)


@pytest.fixture(scope="session")
def release_mode(request):
    return request.config.getoption("--release-check")


@pytest.fixture(scope="session")
def clickgraft_app(request):
    """The .app under test, validated at fixture time rather than import time.

    Import-time validation is what let a stale dist/ through: the decorator ran
    once, before anything had decided what the candidate was.
    """
    chosen = request.config.getoption("--clickgraft-app")
    app = os.path.abspath(chosen) if chosen else os.path.join(ROOT, "dist", "ClickGraft.app")
    exe = os.path.join(app, "Contents", "MacOS", "ClickGraft")
    if not os.path.exists(exe):
        _missing(request, f"no ClickGraft.app at {app} "
                          f"(build one, or pass --clickgraft-app)")
    return app


@pytest.fixture(scope="session")
def clickgraft_exe(clickgraft_app):
    return os.path.join(clickgraft_app, "Contents", "MacOS", "ClickGraft")


@pytest.fixture(scope="session")
def exe(clickgraft_exe):
    """Short alias: the call sites read better as _fetch(url, home, exe)."""
    return clickgraft_exe


@pytest.fixture(scope="session")
def shipped_pin(clickgraft_app):
    return os.path.join(clickgraft_app, "Contents", "Resources", "python-pin.json")


@pytest.fixture(scope="session")
def payload(request):
    """The archive the pin names -- that exact one, never a rebuild.

    It cannot be rebuilt to match: signing writes a fresh signature every time,
    so two runs of `fetch_python.py --payload` give two sha256s and the pin
    names one. A synthetic stand-in would be worse than nothing here, because it
    could not exercise the signature check at all.
    """
    import sys
    if sys.platform != "darwin":
        _missing(request, "the payload is signed with a Developer ID, which needs macOS")
    chosen = request.config.getoption("--clickgraft-payload")
    if chosen:
        built = os.path.abspath(chosen)
    else:
        sys.path.insert(0, os.path.join(ROOT, "packaging"))
        import fetch_python
        built = fetch_python.payload_path()
    if not os.path.exists(built):
        _missing(request, f"no built payload at {built}; "
                          f"run python3 packaging/fetch_python.py --payload")
    if _sha256(built) != _pin()["payload_zip_sha256"]:
        _missing(request, f"{built} is not the archive the pin names; rebuild and "
                          f"re-pin with python3 packaging/fetch_python.py --payload")
    return built


@pytest.fixture(scope="session")
def developer_id(request):
    """The signing identity, for the provenance cases.

    Required in a release run: those tests are the only behavioural cover for
    the Team ID check, and skipping them in the maintainer's own run is exactly
    the hole --release-check exists to close.
    """
    import subprocess
    run = subprocess.run(["security", "find-identity", "-v", "-p", "codesigning"],
                         capture_output=True, text=True)
    for line in run.stdout.splitlines():
        if "Developer ID Application" in line:
            return line.split('"')[1]
    _missing(request, "no Developer ID Application identity in the keychain")


@pytest.fixture(autouse=True)
def _no_gui_launch_as_interpreter(request, monkeypatch):
    """Catch a test running ClickGraft.app as if it were python3.

    `exe` is the app; `installed` is the fetched interpreter. Renaming a local
    that now shares a fixture's name does not error -- the name still resolves,
    to the wrong object -- so a test asked the GUI binary to run
    `-c "import sys; ..."`. It opened an NSApplication and hung for ever, and the
    suite went from 8 seconds to a wedged process with no failure to read.

    Anything invoking the app with a python-style flag is a mistake, not a test.
    """
    real = __import__("subprocess").run

    def guarded(args, *a, **kw):
        if isinstance(args, (list, tuple)) and len(args) >= 2:
            first, second = str(args[0]), str(args[1])
            if first.endswith("/MacOS/ClickGraft") and second in ("-c", "-m"):
                raise AssertionError(
                    f"test ran the ClickGraft app as an interpreter: {first} {second} ...\n"
                    f"Use the `installed` interpreter path, not the `exe` fixture.")
        return real(args, *a, **kw)

    monkeypatch.setattr(__import__("subprocess"), "run", guarded)
