import subprocess
from pathlib import Path
import pytest

CLASSIFIER = Path(__file__).resolve().parents[1] / 'site/deploy/visitor-classify.awk'


@pytest.mark.parametrize('ua,method,prefix,kind', [
    ('Mozilla/5.0 Chrome/150.0', 'GET', '1.2.3.', 'browser'),
    ('Mozilla/5.0 Chrome/150.0', 'HEAD', '1.2.3.', 'bot'),
    ('ClaudeBot', 'GET', '1.2.3.', 'bot'),
    ('ClickGraft/1.5.9', 'GET', '1.2.3.', 'app'),
    ('curl/8.0', 'GET', '1.2.3.', 'tool'),
    ('Mozilla/5.0 Windows Chrome/150.0', 'GET', '52.112.', 'bot'),
    ('Mozilla/5.0 Mac Chrome/150.0', 'GET', '52.112.', 'browser'),
    ('Mozilla/5.0 Firefox/100.0', 'GET', '1.2.3.', 'bot'),
])
def test_common_classification(ua, method, prefix, kind):
    program = CLASSIFIER.read_text() + '\nBEGIN { print class(ua, method, prefix) }'
    result = subprocess.run(['awk', '-v', 'newest=154', '-v', 'ua=' + ua,
                             '-v', 'method=' + method, '-v', 'prefix=' + prefix,
                             program], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == kind


def _downloads(root, log):
    """The DOWNLOADS column summary.sh prints for the one day in `log`.

    Read out of the table rather than grepped for a word. The first draft of this
    test looked for '2026-09-22' and 'DOWNLOAD' on one line; the table prints
    '22/Sep/2026' and the heading is on its own line, so the filter matched
    nothing and the test passed with the counter deliberately broken.
    """
    out = subprocess.run(['sh', str(root / 'summary.sh'), str(log)],
                         capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        fields = line.split()
        if len(fields) == 4 and '/' in fields[0]:
            return int(fields[3])
    raise AssertionError(f'no day row in summary.sh output:\n{out}')


def test_the_fetched_interpreter_is_not_counted_as_a_download(tmp_path):
    """/ClickGraft-python-<v>.zip is one fetch per Mac that has no developer
    tools, not one person choosing to try ClickGraft. Counting it would inflate
    the one number worth having.

    The rule is only the digits in summary.sh's version group, which is easy to
    widen by accident, so both halves are here: the interpreter is not counted,
    and a real download still is. Without the second half, a counter that counts
    nothing at all would pass.
    """
    import os
    root = CLASSIFIER.parent
    stamp = '[22/Sep/2026:10:00:00 +0000]'
    # A BROWSER user agent on the interpreter, deliberately. ClickGraft's own
    # fetch identifies itself, and the table counts browsers only, so an app user
    # agent would be excluded before the path was even looked at -- which is what
    # the first draft of this test did, leaving the rule it names untested. With a
    # browser on the line, the path rule is the only thing that can exclude it.
    browser = 'Mozilla/5.0 Chrome/150.0'
    fetch = (f'1.2.3. - - {stamp} "GET /ClickGraft-python-3.13.9.zip HTTP/1.1" '
             f'200 17000000 "-" "{browser}"\n'
             f'1.2.3. - - {stamp} "GET / HTTP/1.1" 200 4000 "-" "{browser}"\n')
    real = (f'1.2.5. - - {stamp} "GET /ClickGraft-1.7.0.zip HTTP/1.1" '
            f'200 800000 "-" "{browser}"\n')

    only_fetches = tmp_path / 'fetches.log'
    only_fetches.write_text(fetch)
    assert _downloads(root, only_fetches) == 0, 'the interpreter was counted as a download'

    both = tmp_path / 'both.log'
    both.write_text(fetch + real)
    assert _downloads(root, both) == 1, 'a real download stopped being counted'

    # The watcher shares the rule, and its own notification must not fire either.
    config = tmp_path / 'watch.conf'
    config.write_text('NTFY_URL=http://invalid.local\nNTFY_TOPIC=test\nNTFY_USER=test\n'
                      'NTFY_PASS=test\nSTATE=' + str(tmp_path / 'state') + '\n')
    watcher = subprocess.run(['sh', str(root / 'watch/clickgraft-watch.sh'),
                              '--dry-run', str(only_fetches)],
                             env=dict(os.environ, CLICKGRAFT_WATCH_CONF=str(config)),
                             capture_output=True, text=True, check=True)
    assert 'download' not in watcher.stdout.lower(), watcher.stdout


def test_summary_and_watcher_use_shared_rules(tmp_path):
    import os
    root = CLASSIFIER.parent
    log = tmp_path / 'access.log'
    log.write_text(
        '1.2.3. - - [22/Sep/2026:10:00:00 +0000] "GET /ClickGraft-1.5.9.zip HTTP/1.1" 200 123 "-" "Mozilla/5.0 Chrome/150.0"\n'
        '9.8.7. - - [22/Sep/2026:10:01:00 +0000] "GET /ClickGraft-1.5.9.zip HTTP/1.1" 200 123 "-" "ClaudeBot"\n')
    summary = subprocess.run(['sh', str(root / 'summary.sh'), str(log)], capture_output=True, text=True, check=True)
    assert 'DOWNLOAD' in summary.stdout.upper()
    config = tmp_path / 'watch.conf'
    config.write_text('NTFY_URL=http://invalid.local\nNTFY_TOPIC=test\nNTFY_USER=test\nNTFY_PASS=test\nSTATE=' + str(tmp_path / 'state') + '\n')
    env = dict(os.environ, CLICKGRAFT_WATCH_CONF=str(config))
    watcher = subprocess.run(['sh', str(root / 'watch/clickgraft-watch.sh'), '--dry-run', str(log)],
                             env=env, capture_output=True, text=True, check=True)
    assert watcher.stdout.count('would send:') == 1
    assert '1.2.3.' in watcher.stdout
