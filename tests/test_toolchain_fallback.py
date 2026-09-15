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
