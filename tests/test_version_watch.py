"""
tests/test_version_watch.py — a dropped notification must not be silently
consumed.

The watcher records what it has reported so it does not alert every morning.
That record used to advance whether or not the notification actually went out,
so a new HP build found while ntfy was unreachable was marked reported, never
sent, and skipped on every later run. These drive the real script with a stub
curl: HP answers, ntfy is switchable.
"""

import os
import shutil
import subprocess

import pytest

WATCH = os.path.join(os.path.dirname(__file__), "..", "site", "deploy", "watch",
                     "clickgraft-version-watch.sh")

pytestmark = pytest.mark.skipif(not shutil.which("bash") or not os.path.isfile(WATCH),
                                reason="needs bash and the watcher script")

# HP's x64 feed names 4.12.26, so the generated candidate range covers 4.12.30.
NEW = "4.12.30"
KNOWN = "4.8.117"
FEED_DEFAULT = '{"currentRelease":"4.12.26"}'

STUB = r"""#!/bin/bash
url="${@: -1}"; args=" $* "
case "$url" in *ntfy.example.invalid*)
  [ "${NTFY_OK:-0}" = "1" ] && exit 0 || exit 1 ;;
esac
case "$url" in
  *update-darwin.json*) echo "$FEED_BODY_STUB"; exit 0 ;;
  *discover*) echo 'lfp-downloads.hpcloud.hp.com/hpclick/'; exit 0 ;;
  *RELEASES*) echo 'A https://ftp.hp.com/x64/hpclick-4.12.26-full.nupkg 1'; exit 0 ;;
esac
if [[ "$args" == *" -sI "* ]]; then
  case "$url" in
    *HPClick-%(known)s.zip|*HPClick-%(new)s.zip)
      printf 'HTTP/1.1 200 OK\r\nContent-Length: 572397601\r\n\r\n'; exit 0 ;;
    *) printf 'HTTP/1.1 404 Not Found\r\n\r\n'; exit 0 ;;
  esac
fi
if [[ "$args" == *"%%{http_code}"* ]]; then
  case "$url" in *HPClick-%(new)s.dmg) echo -n 206; exit 0 ;; *) echo -n 404; exit 0 ;; esac
fi
exit 0
""" % {"known": KNOWN, "new": NEW}


@pytest.fixture
def watcher(tmp_path):
    """Returns run(ntfy_ok, **env) -> CompletedProcess, over a fresh state dir."""
    binr = tmp_path / "bin"
    binr.mkdir()
    stub = binr / "curl"
    stub.write_text(STUB)
    stub.chmod(0o755)
    # The script sleeps 1s per candidate to be polite to HP's server. There are
    # ~117 candidates, so a real sleep makes this file take minutes. Nothing
    # under test depends on the delay.
    nap = binr / "sleep"
    nap.write_text("#!/bin/bash\nexit 0\n")
    nap.chmod(0o755)
    conf = tmp_path / "conf"
    conf.write_text('NTFY_URL="https://ntfy.example.invalid"\n'
                    'NTFY_TOPIC="t"; NTFY_USER="u"; NTFY_PASS="p"\n')
    state = tmp_path / "state"
    state.mkdir()
    # Seed the Windows baseline so the candidate range is generated and the
    # Windows alert itself stays quiet.
    (state / "win-x64").write_text("A https://ftp.hp.com/x64/hpclick-4.12.26-full.nupkg 1")
    (state / "win-x86").write_text("A https://ftp.hp.com/x64/hpclick-4.12.26-full.nupkg 1")

    def run(ntfy_ok, **extra):
        env = dict(os.environ)
        env.update({"PATH": "%s:%s" % (binr, env["PATH"]),
                    "CLICKGRAFT_WATCH_CONF": str(conf),
                    "STATE": str(state),
                    "FEED_BODY_STUB": FEED_DEFAULT,
                    "NTFY_OK": "1" if ntfy_ok else "0"})
        env.update(extra)
        return subprocess.run(["bash", WATCH], env=env, capture_output=True, text=True)

    run.state = state
    run.seen = state / "versions-seen"
    return run


def _seen(run):
    return run.seen.read_text().split() if run.seen.exists() else []


def test_a_build_found_while_ntfy_is_down_is_reported_when_it_comes_back(watcher):
    first = watcher(False)
    assert "FOUND HPClick-%s.dmg" % NEW in first.stdout
    assert "publish failed" in first.stderr
    # The whole point: not banked as reported.
    assert NEW not in _seen(watcher), "marked reported although nothing was sent"

    again = watcher(False)
    assert "FOUND HPClick-%s.dmg" % NEW in again.stdout, "the find was not retried"

    recovered = watcher(True)
    assert "FOUND HPClick-%s.dmg" % NEW in recovered.stdout
    assert "publish failed" not in recovered.stderr
    assert NEW in _seen(watcher), "delivered but never recorded"


def test_a_delivered_build_alert_is_not_repeated(watcher):
    """The control. Holding state back must not turn into alerting every run."""
    first = watcher(True)
    assert "FOUND HPClick-%s.dmg" % NEW in first.stdout
    assert NEW in _seen(watcher)

    second = watcher(True)
    assert "FOUND HPClick-%s.dmg" % NEW not in second.stdout
    assert "no new macOS build" in second.stdout
    assert _seen(watcher).count(NEW) == 1, "recorded twice"


def test_the_feed_baseline_is_held_back_when_the_push_fails(watcher):
    body = FEED_DEFAULT
    watcher(True, FEED_BODY_STUB=body)          # establish a baseline
    moved = '{"currentRelease":"4.13.0"}'

    down = watcher(False, FEED_BODY_STUB=moved)
    assert "publish failed" in down.stderr
    assert (watcher.state / "feed-body.json").read_text() == body, \
        "baseline advanced although the change was never reported"

    up = watcher(True, FEED_BODY_STUB=moved)
    assert "publish failed" not in up.stderr
    assert (watcher.state / "feed-body.json").read_text() == moved
