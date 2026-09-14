"""
tests/test_report_format.py — The contract between the app's reports and the collector.

The collector files a report by how its body STARTS: "kind: result" is the
denominator, "kind: unsupported-version" is a contribution, any other "kind: "
is the problem queue, and anything else is "unknown". Nothing but that first
line connects the Swift app to the Python collector, so a harmless-looking
refactor on one side -- moving the header, renaming a kind -- would quietly
file real reports as unknown. On 14 Sep 2026 the header was factored out of
reportBody() into reportHeader(); this pins what that refactor had to preserve.
Target: Python 3.9+
"""

import http.client
import importlib.util
import os
import re
import shutil
import tempfile
import threading
import http.server

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COLLECTOR = os.path.join(ROOT, "site", "deploy", "collector", "collector.py")
SWIFT = os.path.join(ROOT, "packaging", "ClickGraft.swift")

HEADER = ("ClickGraft 1.5.5\n"
          "macOS Version 26.6.2 (Build 25G83)\n"
          "arch: arm64  (hardware: Apple Silicon)\n")


@pytest.fixture
def collector():
    """The real collector, serving on localhost, writing to a temp directory."""
    spec = importlib.util.spec_from_file_location("collector_under_test", COLLECTOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    reports = tempfile.mkdtemp(prefix="cg-reports-")
    mod.REPORTS = reports
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    def post(body, country="CA"):
        c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        data = body.encode("utf-8")
        c.request("POST", "/report", body=data,
                  headers={"Content-Type": "text/plain; charset=utf-8",
                           "Content-Length": str(len(data)), "CF-IPCountry": country})
        status = c.getresponse().status
        c.close()
        return status, sorted(os.listdir(reports))

    try:
        yield post, reports
    finally:
        server.shutdown()
        shutil.rmtree(reports, ignore_errors=True)


def _pile(files):
    assert len(files) == 1, files
    return files[0].split("-")[0]


def test_problem_report_is_filed_as_a_problem(collector):
    post, _ = collector
    status, files = post("kind: problem\n" + HEADER
                         + "outcome: the build did not finish\n\n"
                         + "contact: someone@example.com\n"
                         + "  (they asked to be told when this is fixed)\n\n"
                         + "error:\nCommand failed\n")
    assert status == 200 and _pile(files) == "problem"
    assert files[0].endswith("-CA.txt")


def test_result_report_is_filed_as_a_result(collector):
    post, _ = collector
    _, files = post("kind: result\n" + HEADER
                    + "outcome: the build finished and every check passed\n")
    assert _pile(files) == "result"


def test_version_report_with_new_header_and_contact_is_a_version(collector):
    post, _ = collector
    _, files = post("kind: unsupported-version\n" + HEADER
                    + "contact: someone@example.com\n"
                    + "  (they asked to hear when this version is supported)\n\n"
                    + "=== Probe Report for /Applications/HP Click V4.7.app ===\n"
                    + "App Version: 4.7.28\n")
    assert _pile(files) == "version"


def test_a_body_without_kind_is_not_a_problem(collector):
    post, _ = collector
    _, files = post("hello, is this thing on?\n")
    assert _pile(files) == "unknown"


@pytest.mark.parametrize("probe", ["healthcheck", '{"font":"__healthcheck__"}'])
def test_healthchecks_are_answered_and_never_stored(collector, probe):
    post, _ = collector
    status, files = post(probe)
    assert status == 200 and files == []


def test_home_folder_is_scrubbed_on_arrival(collector):
    post, reports = collector
    _, files = post("kind: problem\n" + HEADER + "source: /Users/janedoe/Applications/HP Click.app\n")
    with open(os.path.join(reports, files[0]), encoding="utf-8") as f:
        stored = f.read()
    assert "janedoe" not in stored and "/Users/~" in stored


# -- the Swift side of the contract ----------------------------------------

def _swift():
    with open(SWIFT, encoding="utf-8") as f:
        return f.read()


def test_the_header_starts_with_kind():
    src = _swift()
    m = re.search(r"private func reportHeader\(kind: String\) -> String \{(.*?)\n    \}", src, re.S)
    assert m, "reportHeader(kind:) is gone; the collector depends on it"
    first = re.search(r"var out = (.+)", m.group(1))
    assert first and first.group(1).strip() == r'"kind: \(kind)\n"'


def test_kind_is_written_in_exactly_one_place():
    """The old version report prepended its own "kind: ..." to the probe text.
    A second place that writes the line is a second place to get it wrong."""
    src = _swift()
    assert len(re.findall(r'"kind: ', src)) == 1


def test_bodies_are_built_through_the_header():
    src = _swift()
    m = re.search(r"private func reportBody\(.*?\{(.*?)\n    \}", src, re.S)
    assert m and "reportHeader(kind: kind)" in m.group(1)


def test_every_kind_the_app_sends_has_a_pile(collector):
    """A kind renamed in the app without teaching the collector would land in
    "unknown" -- kept, but out of every count anyone reads."""
    src = _swift()
    kinds = set(re.findall(r'reportHeader\(kind: "([a-z-]+)"\)', src))
    kinds |= set(re.findall(r'reportBody\([^)]*kind: "([a-z-]+)"\)', src))
    default = re.search(r'kind: String = "([a-z-]+)"', src)
    assert default, "reportBody lost its default kind"
    kinds.add(default.group(1))
    assert {"problem", "result", "unsupported-version"} <= kinds, kinds

    post, reports = collector
    for k in sorted(kinds):
        for f in os.listdir(reports):
            os.remove(os.path.join(reports, f))
        _, files = post(f"kind: {k}\n" + HEADER)
        assert _pile(files) != "unknown", f"kind {k!r} is filed as unknown"
