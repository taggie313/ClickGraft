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
