"""Exercise the wizard's real transport against controlled stand-in backends.

The bridge is ClickGraft.swift's own Agent (and the Toolchain it uses) compiled
with BackendTransport.swift as two files, the way build_app.sh builds the app,
so Agent.stream and Agent.once run as the wizard calls them. Agent starts
`python3 -m clickgraft.cli agent ...` with PYTHONPATH at the resources folder;
here that folder holds a stand-in clickgraft/cli.py that runs the code a test
passes as its argument.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unicodedata

import pytest

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
let agent = Agent(resources: URL(fileURLWithPath: CommandLine.arguments[1]))
let request = Array(CommandLine.arguments.dropFirst(2))
func show(_ event: [String: Any]) {
    let data = try! JSONSerialization.data(withJSONObject: event)
    FileHandle.standardOutput.write(data + Data([10]))
}
if ProcessInfo.processInfo.environment["BRIDGE_ONCE"] == "1" {
    // As the wizard asks: on the main thread, which once() blocks.
    precondition(Thread.isMainThread)
    let reply: [String: Any]? = agent.once(request)
    show(reply ?? ["type": "no reply"])
    exit(0)
}
// How long to stay after the final event, watching for a second one.
let linger = Double(ProcessInfo.processInfo.environment["BRIDGE_LINGER"] ?? "") ?? 0.05
var terminals = 0
agent.stream(request) { event in
    precondition(Thread.isMainThread)
    show(event)
    if let kind = event["type"] as? String, kind == "done" || kind == "error" {
        terminals += 1
        precondition(terminals == 1)
        DispatchQueue.main.asyncAfter(deadline: .now() + linger) { exit(0) }
    }
}
RunLoop.main.run()
'''

STAND_IN = '''import sys
exec(sys.argv[2])
'''


@pytest.fixture(scope="module")
def bridge(tmp_path_factory):
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        pytest.skip("the AppKit bridge requires macOS and Swift")
    folder = tmp_path_factory.mktemp("backend-stream")
    source = (ROOT / "packaging/ClickGraft.swift").read_text()
    production = source.split("/// Content shorter than its scroll view")[0]
    main = folder / "main.swift"
    main.write_text(production + HARNESS)
    binary = folder / "bridge"
    subprocess.run(["swiftc", "-O", str(main), str(ROOT / "packaging/BackendTransport.swift"),
                    "-o", str(binary), "-module-cache-path", str(folder / "cache")],
                   check=True, capture_output=True, text=True, timeout=300)
    resources = folder / "resources"
    (resources / "clickgraft").mkdir(parents=True)
    (resources / "clickgraft" / "__init__.py").write_text("")
    (resources / "clickgraft" / "cli.py").write_text(STAND_IN)
    return binary, resources


def run(bridge, code=None, once=False, linger=None, timeout=10):
    binary, resources = bridge
    env = dict(os.environ, CLICKGRAFT_PYTHON=sys.executable)
    if code is None:
        env["CLICKGRAFT_PYTHON"] = "/nonexistent-clickgraft-python"
    if once:
        env["BRIDGE_ONCE"] = "1"
    if linger is not None:
        env["BRIDGE_LINGER"] = str(linger)
    result = subprocess.run([str(binary), str(resources), code or "pass"], env=env,
                            capture_output=True, text=True, timeout=timeout, check=True)
    events = [json.loads(line) for line in result.stdout.splitlines()]
    if not once:
        assert sum(e["type"] in ("done", "error") for e in events) == 1
    return events


def unconfirmed(event):
    """A transport failure, worded for the screen it is shown on."""
    assert event["type"] == "error"
    assert event["stage"] == "backend"
    first = event["error"].split("\n\n")[0]
    assert first.startswith("The part of ClickGraft that does the work")
    # docs/wizard-copy.md: the wizard never says these words to anyone.
    for word in ("backend", "background task", "output stream"):
        assert word not in first.lower()
    return first


def test_launch_failure(bridge):
    assert "couldn't be started" in unconfirmed(run(bridge)[-1])


def test_stderr_cannot_block_final_result(bridge):
    # Lingers past the 2 s wait for EOF, so a second completion would show.
    events = run(bridge, '''import os
os.write(2, b"diagnostic" * 30000)
print('{"type":"progress","step":1}', flush=True)
print('{"type":"done","ok":true}', flush=True)
''', linger=2.5)
    assert [e["type"] for e in events] == ["progress", "done"]


def test_fragmented_unicode_and_unterminated_final_line(bridge):
    events = run(bridge, '''import os
payload = '{"type":"progress","text":"café"}\\n{"type":"done"}'.encode()
for byte in payload:
    os.write(1, bytes([byte]))
''')
    assert events[0]["type"] == "progress"
    assert unicodedata.normalize("NFC", events[0]["text"]) == "café"
    assert events[1:] == [{"type": "done"}]


@pytest.mark.parametrize("code", [
    "pass",
    "print('broken json')",
    "print('{\"type\":\"done\"}'); raise SystemExit(1)",
    "print('{\"type\":\"done\"}\\n{\"type\":\"done\"}')",
    "print('{\"type\":')",
    "print('x' * (1024 * 1024 + 1))",
])
def test_invalid_or_missing_result(bridge, code):
    unconfirmed(run(bridge, code)[-1])


def test_signal_is_reported_as_a_signal(bridge):
    first = unconfirmed(run(bridge, "import os, signal; os.kill(os.getpid(), signal.SIGKILL)")[-1])
    assert "signal 9" in first
    assert "exit status" not in first


def test_structured_failure_survives_nonzero_exit(bridge):
    events = run(bridge, '''print('{"type":"error","stage":"printers_lost","error":"safe rollback"}')
raise SystemExit(1)
''')
    assert events[-1]["stage"] == "printers_lost"


def test_diagnostics_are_bounded(bridge):
    event = run(bridge, "import os; os.write(2, b'x' * 200000 + b'END')")[-1]
    assert event["error"].endswith("END")
    assert len(event["error"]) < 17000


def test_inherited_pipe_has_bounded_wait(bridge):
    # The descendant keeps the pipes for 3 s; the answer is given up on at
    # 2 s, and the bridge stays until 4.5 s to see that nothing else arrives
    # when the pipes finally close.
    event = run(bridge, '''import subprocess, sys
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"])
print('{"type":"done"}')
''', linger=2.5)[-1]
    assert "still connected" in unconfirmed(event)


def test_final_line_written_after_exit_is_read(bridge):
    """The terminal event waits for EOF, not for the exit: deterministic.

    1.5.9 stopped reading when the process exited (its terminationHandler
    removed the reader), which lost a final line that was still in the pipe
    -- 3 of 2,000 idle runs and 5 of 2,000 with the CPU oversubscribed, in the
    22 Sep 2026 review. Here the final line is written 0.3 s after the process
    has exited, by a descendant holding its pipes, so a reader that stops at
    exit misses it every time. Inside the transport's 2 s wait for EOF, it must
    arrive.
    """
    code = r'''import subprocess, sys
later = "import os, time; time.sleep(0.3); os.write(1, b'{\"type\":\"done\"}\\n')"
subprocess.Popen([sys.executable, "-c", later])
'''
    for _ in range(3):
        assert run(bridge, code) == [{"type": "done"}]


def test_large_final_line_at_exit(bridge):
    """A 300 KB final event, then an immediate exit. The writer blocks until
    the reader has drained all but the last pipeful, so the exit arrives
    with that pipeful unread: 1.5.9 lost it 200 of 200 times (22 Sep 2026)."""
    for _ in range(5):
        events = run(bridge, '''import json, os
data = (json.dumps({"type": "done", "pad": "x" * 300000}) + "\\n").encode()
while data:
    data = data[os.write(1, data):]
os._exit(0)
''')
        assert [e["type"] for e in events] == ["done"]
        assert len(events[0]["pad"]) == 300000


def test_final_event_at_exit_stress(bridge):
    """The real shape: a short final line, then an immediate exit. 1.5.9 lost
    it a few times in a thousand, so this alone would rarely catch that; the
    two tests above are the guard, and this keeps the common case honest."""
    for _ in range(100):
        assert run(bridge, "import os; os.write(1, b'{\"type\":\"done\"}\\n'); os._exit(0)") == [{"type": "done"}]


def test_one_shot_reply_drains_stderr(bridge):
    events = run(bridge, "import os; os.write(2, b'x' * 250000); "
                         "print('{\"type\":\"env\",\"env\":{}}')", once=True)
    assert events == [{"type": "env", "env": {}}]


def test_one_shot_without_a_reply_is_an_error_not_nothing(bridge):
    # The wizard checks a reply's type, so no answer has to come back as one.
    assert "couldn't be started" in unconfirmed(run(bridge, once=True)[0])
    assert "without giving an answer" in unconfirmed(
        run(bridge, "raise SystemExit(1)", once=True)[0])
