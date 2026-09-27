"""
tests/test_toolchain_fallback.py — Running when Xcode is waiting for its licence.

On 15 Sep 2026 Xcode 27.0 updated itself with its licence unaccepted, and every
/usr/bin developer stub -- python3 included -- then exited 69 with "You have not
agreed to the Xcode license agreements". ClickGraft starts its backend through
that stub, so it could not start at all. The app now detects that and, when the
Command Line Tools are installed, runs the backend with DEVELOPER_DIR pointed at
them. These tests hold both halves: the backend really works that way, and the
Swift side still does it.
Target: Python 3.9+
"""

import json
import os
import re
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWIFT = os.path.join(ROOT, "packaging", "ClickGraft.swift")
CLT = "/Library/Developer/CommandLineTools"


def _swift():
    with open(SWIFT, encoding="utf-8") as f:
        return f.read()


@pytest.mark.skipif(not os.path.exists(os.path.join(CLT, "usr", "bin", "python3")),
                    reason="needs the Command Line Tools installed")
def test_backend_runs_on_the_command_line_tools_alone():
    env = dict(os.environ, DEVELOPER_DIR=CLT, PYTHONPATH=ROOT, PYTHONDONTWRITEBYTECODE="1")
    out = subprocess.run(["/usr/bin/python3", "-m", "clickgraft.cli", "agent", "env"],
                         cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    events = [json.loads(l) for l in out.stdout.splitlines() if l.startswith("{")]
    env_event = next(e for e in events if e.get("type") == "env")
    assert env_event["env"]["clt"] is True
    tools = env_event["env"]["tools"]
    assert tools and all(tools.values()), tools


def test_the_backend_is_always_started_through_toolchain():
    src = _swift()
    m = re.search(r"private func process\(_ args: \[String\]\) -> Process \{(.*?)\n    \}", src, re.S)
    assert m, "Agent.process is gone"
    body = m.group(1)
    assert "Toolchain.python" in body
    assert 'env["DEVELOPER_DIR"] = dev' in body and "Toolchain.developerDir" in body
    # No process may be launched straight from the stub, bypassing the check.
    assert 'URL(fileURLWithPath: "/usr/bin/python3")' not in src


def test_only_the_licence_triggers_the_fallback():
    """Missing tools make the stub offer an install; that must stay on the
    existing requirements path, not be mistaken for a licence problem."""
    src = _swift()
    m = re.search(r"private static func probe\(\) -> State \{(.*?)\n    \}", src, re.S)
    assert m, "Toolchain.probe is gone"
    assert 'contains("license")' in m.group(1)
    assert "return .normal" in m.group(1)


def test_requirements_screen_checks_before_starting_the_backend():
    src = _swift()
    m = re.search(r"@objc func showRequirements\(\) \{(.*?)guard let d = agent\.once", src, re.S)
    assert m, "showRequirements no longer starts with the toolchain check"
    assert "Toolchain.refresh()" in m.group(1)
    assert "showXcodeLicence()" in m.group(1)


# --- the gate itself -------------------------------------------------------
#
# Until 27 Sep 2026 REQUIRED_CLT_TOOLS named codesign, ditto and getconf, which
# are base-OS and can never be missing, and omitted clang and nm, which the build
# needed. check_clt() therefore returned True on a Mac with no developer tools
# and the build died at 55% inside the compiler. Two things stop that recurring:
# the gate has to RUN a tool rather than stat it, and the list has to match what
# the code actually invokes.

SHIM_TOOLS = ["clang", "lipo", "otool", "nm", "vtool", "install_name_tool",
              "strip", "dsymutil", "ld", "ar"]


def _fake_tool(directory, name, body):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(path, 0o755)
    return path


def test_gate_fails_when_a_tool_cannot_resolve(tmp_path, monkeypatch):
    """A shim with no developer directory behind it exits non-zero on stderr.

    shutil.which() finds it either way, which is exactly why the old check passed
    on the Macs it existed to catch.
    """
    from clickgraft.deps import REQUIRED_CLT_TOOLS, check_clt

    d = str(tmp_path)
    for tool in REQUIRED_CLT_TOOLS:
        _fake_tool(d, tool, "#!/bin/sh\necho 'xcrun: error: invalid DEVELOPER_DIR path' >&2\nexit 1\n")
    monkeypatch.setenv("PATH", d + os.pathsep + os.environ["PATH"])
    assert check_clt() is False

    import shutil as _shutil
    assert all(_shutil.which(t) is not None for t in REQUIRED_CLT_TOOLS), \
        "the control is broken: the fakes must be findable, or this proves nothing"


def test_gate_passes_when_a_tool_merely_complains_about_arguments(tmp_path, monkeypatch):
    """Run with no arguments, a real tool prints usage and exits non-zero.

    That is a pass. Treating a non-zero exit as failure would reject every
    working Mac.
    """
    from clickgraft.deps import REQUIRED_CLT_TOOLS, check_clt

    d = str(tmp_path)
    for tool in REQUIRED_CLT_TOOLS:
        _fake_tool(d, tool, "#!/bin/sh\necho 'usage: %s ...' >&2\nexit 1\n" % tool)
    monkeypatch.setenv("PATH", d + os.pathsep + os.environ["PATH"])
    assert check_clt() is True


def test_required_list_matches_what_the_code_invokes():
    """The list drifted once because nothing tied it to reality. This ties it."""
    from clickgraft.deps import REQUIRED_CLT_TOOLS

    invoked = set()
    src_dir = os.path.join(ROOT, "clickgraft")
    for fn in sorted(os.listdir(src_dir)):
        if not fn.endswith(".py"):
            continue
        body = open(os.path.join(src_dir, fn), encoding="utf-8").read()
        for tool in SHIM_TOOLS:
            # As an argv element, not as a word in a comment.
            if f'["{tool}"' in body or f'["{tool}",' in body:
                invoked.add(tool)

    assert invoked == set(REQUIRED_CLT_TOOLS), (
        f"clickgraft/ invokes {sorted(invoked)} but REQUIRED_CLT_TOOLS is "
        f"{sorted(REQUIRED_CLT_TOOLS)}")


def test_base_os_tools_are_not_claimed_as_developer_tools():
    """codesign, ditto and file ship with macOS and work with no Xcode at all.

    Listing one as a Command Line Tool makes the gate unfalsifiable, which is
    how it came to pass on a Mac that had none.
    """
    from clickgraft.deps import REQUIRED_CLT_TOOLS

    for tool in ("codesign", "ditto", "file", "xattr", "getconf", "sw_vers"):
        assert tool not in REQUIRED_CLT_TOOLS, f"{tool} is base-OS, not a developer tool"
