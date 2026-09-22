import errno
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'site/deploy/publish_site.py'
spec = importlib.util.spec_from_file_location('publish_site', SCRIPT)
publish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish)


def staged(root, name, version):
    folder = root / name
    html = folder / 'html'
    (html / 'es').mkdir(parents=True)
    (html / 'index.html').write_text('ClickGraft ' + version)
    (html / 'es/index.html').write_text('ClickGraft')
    (html / 'sitemap.xml').write_text('<xml/>')
    archive = f'ClickGraft-{version}.zip'
    (html / archive).write_bytes(version.encode())
    (html / 'ClickGraft.zip').symlink_to(archive)
    (html / 'appcast.json').write_text(json.dumps({'version': version, 'sha256': hashlib.sha256(version.encode()).hexdigest()}))
    (folder / 'collector').mkdir()
    (folder / 'collector/collector.py').write_text(version)
    (folder / 'summary.sh').write_text(version)
    (folder / 'visitor-classify.awk').write_text(version)
    (folder / 'publish_site.py').write_text(version)
    return folder


def leftovers(root, prefix):
    return sorted(path.name for path in root.iterdir() if path.name.startswith(prefix))


def live_page(root):
    return (root / 'html/index.html').read_text()


def test_swap_preserves_previous_site_and_mounted_collector(tmp_path):
    initial = staged(tmp_path, '.incoming-one', '1')
    publish.publish(initial, tmp_path)
    collector_inode = (tmp_path / 'collector').stat().st_ino
    second = staged(tmp_path, '.incoming-two', '2')
    backup = publish.publish(second, tmp_path)
    assert live_page(tmp_path) == 'ClickGraft 2'
    assert (backup / 'html/index.html').read_text() == 'ClickGraft 1'
    assert (backup / 'collector/collector.py').read_text() == '1'
    assert (tmp_path / 'collector').stat().st_ino == collector_inode
    assert leftovers(tmp_path, '.incoming-') == []


def test_corrupt_download_leaves_current_usable(tmp_path):
    publish.publish(staged(tmp_path, '.incoming-one', '1'), tmp_path)
    second = staged(tmp_path, '.incoming-two', '2')
    (second / 'html/ClickGraft-2.zip').write_bytes(b'bad')
    with pytest.raises(ValueError, match='checksum'):
        publish.publish(second, tmp_path)
    assert live_page(tmp_path) == 'ClickGraft 1'
    # A refused upload is not kept: the next deploy stages its own.
    assert leftovers(tmp_path, '.incoming-') == []


def test_failure_after_switch_restores_previous(tmp_path, monkeypatch):
    publish.publish(staged(tmp_path, '.incoming-one', '1'), tmp_path)
    second = staged(tmp_path, '.incoming-two', '2')
    def fail(*args):
        raise OSError('injected copy failure')
    monkeypatch.setattr(publish.shutil, 'copy2', fail)
    with pytest.raises(OSError):
        publish.publish(second, tmp_path)
    assert live_page(tmp_path) == 'ClickGraft 1'
    assert (tmp_path / 'collector/collector.py').read_text() == '1'
    assert leftovers(tmp_path, '.incoming-') == [] and leftovers(tmp_path, '.previous-') == []


def test_overlapping_publication_is_refused(tmp_path):
    incoming = staged(tmp_path, '.incoming-one', '1')
    with (tmp_path / '.deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match='in progress'):
            publish.publish(incoming, tmp_path)
    assert not (tmp_path / 'html').exists()
    assert not incoming.exists()


def test_operator_can_restore_a_retained_release(tmp_path):
    publish.publish(staged(tmp_path, '.incoming-one', '1'), tmp_path)
    previous = publish.publish(staged(tmp_path, '.incoming-two', '2'), tmp_path)
    displaced = publish.restore(previous, tmp_path)
    assert live_page(tmp_path) == 'ClickGraft 1'
    assert (tmp_path / 'collector/collector.py').read_text() == '1'
    assert (displaced / 'html/index.html').read_text() == 'ClickGraft 2'
    assert previous.exists()


def test_restore_refused_by_the_lock_copies_nothing(tmp_path):
    publish.publish(staged(tmp_path, '.incoming-one', '1'), tmp_path)
    previous = publish.publish(staged(tmp_path, '.incoming-two', '2'), tmp_path)
    with (tmp_path / '.deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match='in progress'):
            publish.restore(previous, tmp_path)
    assert leftovers(tmp_path, '.incoming-') == []
    assert live_page(tmp_path) == 'ClickGraft 2'


def refuse_exchange(monkeypatch, code=errno.EINVAL):
    def refused(left, right):
        raise OSError(code, os.strerror(code), str(right))
    monkeypatch.setattr(publish, 'exchange', refused)


def test_refused_exchange_falls_back_loudly(tmp_path, monkeypatch):
    publish.publish(staged(tmp_path, '.incoming-one', '1'), tmp_path)
    html_inode = (tmp_path / 'html').stat().st_ino
    refuse_exchange(monkeypatch)
    said = []
    backup = publish.publish(staged(tmp_path, '.incoming-two', '2'), tmp_path, say=said.append)
    assert any(line.startswith('⚠') and 'one at a time' in line for line in said)
    assert live_page(tmp_path) == 'ClickGraft 2'
    assert (tmp_path / 'html/es/index.html').exists()
    assert os.readlink(tmp_path / 'html/ClickGraft.zip') == 'ClickGraft-2.zip'
    # The entries moved, not the directory.
    assert (tmp_path / 'html').stat().st_ino == html_inode
    assert (backup / 'html/index.html').read_text() == 'ClickGraft 1'
    assert not (backup / 'html.swapping').exists()
    publish.validate(tmp_path / 'html')
    publish.validate(backup / 'html')


def test_fallback_puts_the_old_site_back_too(tmp_path, monkeypatch):
    publish.publish(staged(tmp_path, '.incoming-one', '1'), tmp_path)
    (tmp_path / 'html/only-in-1.txt').write_text('old')
    refuse_exchange(monkeypatch)
    def fail(*args):
        raise OSError('injected copy failure')
    monkeypatch.setattr(publish.shutil, 'copy2', fail)
    with pytest.raises(OSError, match='injected'):
        publish.publish(staged(tmp_path, '.incoming-two', '2'), tmp_path, say=lambda line: None)
    assert live_page(tmp_path) == 'ClickGraft 1'
    assert (tmp_path / 'html/only-in-1.txt').read_text() == 'old'
    assert not (tmp_path / 'html/ClickGraft-2.zip').exists()
    assert leftovers(tmp_path, '.incoming-') == [] and leftovers(tmp_path, '.previous-') == []


def test_a_real_exchange_error_is_not_taken_for_unsupported(tmp_path, monkeypatch):
    publish.publish(staged(tmp_path, '.incoming-one', '1'), tmp_path)
    refuse_exchange(monkeypatch, errno.EIO)
    with pytest.raises(OSError) as raised:
        publish.publish(staged(tmp_path, '.incoming-two', '2'), tmp_path, say=pytest.fail)
    assert raised.value.errno == errno.EIO
    assert live_page(tmp_path) == 'ClickGraft 1'
    assert leftovers(tmp_path, '.incoming-') == []


def test_double_fault_leaves_a_restorable_copy_and_says_how(tmp_path, monkeypatch):
    publish.publish(staged(tmp_path, '.incoming-one', '1'), tmp_path)
    real, calls = publish.exchange, []
    def works_once(left, right):
        calls.append(right)
        if len(calls) > 1:
            raise OSError(errno.EIO, os.strerror(errno.EIO), str(right))
        real(left, right)
    monkeypatch.setattr(publish, 'exchange', works_once)
    def fail(*args):
        raise OSError('injected copy failure')
    monkeypatch.setattr(publish.shutil, 'copy2', fail)
    with pytest.raises(publish.NeedsOperator) as raised:
        publish.publish(staged(tmp_path, '.incoming-two', '2'), tmp_path)
    [kept] = leftovers(tmp_path, '.previous-')
    backup = tmp_path / kept
    command = f'python3 {backup}/publish_site.py --restore {backup} {tmp_path}'
    assert f'  {command}' in raised.value.details
    assert live_page(tmp_path) == 'ClickGraft 2'
    assert (backup / 'html/index.html').read_text() == 'ClickGraft 1'
    assert (backup / 'collector/collector.py').read_text() == '1'
    monkeypatch.undo()
    assert publish.main(['--restore', str(backup), str(tmp_path)]) == 0
    assert live_page(tmp_path) == 'ClickGraft 1'
    assert (tmp_path / 'collector/collector.py').read_text() == '1'


def test_only_the_newest_ten_are_kept(tmp_path):
    publish.publish(staged(tmp_path, '.incoming-00', '0'), tmp_path)
    backups = [publish.publish(staged(tmp_path, f'.incoming-{n:02}', str(n)), tmp_path) for n in range(1, 13)]
    assert leftovers(tmp_path, '.previous-') == sorted(path.name for path in backups[-publish.KEEP:])
    assert (backups[-1] / 'html/index.html').read_text() == 'ClickGraft 11'


def test_the_copy_being_restored_is_never_pruned(tmp_path):
    publish.publish(staged(tmp_path, '.incoming-00', '0'), tmp_path)
    backups = [publish.publish(staged(tmp_path, f'.incoming-{n:02}', str(n)), tmp_path) for n in range(1, 11)]
    oldest = backups[0]
    publish.restore(oldest, tmp_path, say=lambda line: None)
    assert oldest.exists()
    assert live_page(tmp_path) == 'ClickGraft 0'
    assert len(leftovers(tmp_path, '.previous-')) == publish.KEEP + 1


def run_script(*args, **env):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True,
                          env={**os.environ, **env})


def test_operator_sees_plain_lines_not_tracebacks(tmp_path):
    usage = run_script()
    assert usage.returncode == 2 and 'usage:' in usage.stderr and 'Traceback' not in usage.stderr
    incoming = staged(tmp_path, '.incoming-one', '1')
    (incoming / 'html/ClickGraft-1.zip').write_bytes(b'bad')
    refused = run_script(incoming, tmp_path)
    assert refused.returncode == 1 and 'Traceback' not in refused.stderr
    assert refused.stderr.splitlines() == ['✗ Appcast checksum does not match the staged download']
    missing = run_script('--restore', tmp_path / '.previous-nothing', tmp_path)
    assert missing.returncode == 1 and missing.stderr.startswith('✗ No such file or directory: ')
    published = run_script(staged(tmp_path, '.incoming-two', '2'), tmp_path)
    assert published.returncode == 0, published.stderr
    assert published.stdout.startswith('✓ published.')


def test_check_only_reads_the_staged_tree_and_says_so_plainly(tmp_path):
    """redeploy.sh checks the staged site before uploading it. That refusal
    used to be a `python3 -c` traceback in the middle of a deploy."""
    good = staged(tmp_path, '.incoming-ok', '3')
    before = sorted(path.name for path in tmp_path.iterdir())
    passed = run_script('--check', good / 'html')
    assert passed.returncode == 0, passed.stderr
    assert passed.stdout.startswith('✓ the staged site is complete')
    bad = staged(tmp_path, '.incoming-bad', '4')
    (bad / 'html/ClickGraft-4.zip').write_bytes(b'not what the appcast says')
    refused = run_script('--check', bad / 'html')
    assert refused.returncode == 1 and 'Traceback' not in refused.stderr
    assert refused.stderr.splitlines() == ['✗ Appcast checksum does not match the staged download']
    # A check writes nothing: no lock, no .incoming-*, no html/.
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(before + ['.incoming-bad'])
    assert not (tmp_path / 'html').exists()


# renameat2 refused for real, not by monkeypatching exchange(): an LD_PRELOAD
# library that answers EINVAL, as a filesystem without RENAME_EXCHANGE does.
# SHIM_PASS lets that many calls through first; SHIM_ERRNO picks the error.
SHIM = r'''
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <stdlib.h>
int renameat2(int od, const char *op, int nd, const char *np, unsigned int flags) {
    static int calls = 0;
    const char *pass = getenv("SHIM_PASS"), *code = getenv("SHIM_ERRNO");
    if (pass && calls++ < atoi(pass)) {
        int (*real)(int, const char *, int, const char *, unsigned int) = dlsym(RTLD_NEXT, "renameat2");
        return real(od, op, nd, np, flags);
    }
    errno = code ? atoi(code) : EINVAL;
    return -1;
}
'''


def test_linux_refusals_of_renameat2_for_real(tmp_path):
    if not sys.platform.startswith('linux') or not shutil.which('cc'):
        pytest.skip('needs Linux, where the exchange is renameat2, and a C compiler')
    (tmp_path / 'shim.c').write_text(SHIM)
    shim = str(tmp_path / 'shim.so')
    subprocess.run(['cc', '-shared', '-fPIC', '-o', shim, str(tmp_path / 'shim.c'), '-ldl'], check=True)

    # Refused outright: the entries move one at a time and the deploy succeeds.
    site = tmp_path / 'fallback'
    site.mkdir()
    publish.publish(staged(site, '.incoming-one', '1'), site)
    html_inode = (site / 'html').stat().st_ino
    run = run_script(staged(site, '.incoming-two', '2'), site, LD_PRELOAD=shim)
    assert run.returncode == 0, run.stderr
    assert run.stderr.startswith('⚠ The filesystem would not exchange html/ in one step (Invalid argument)')
    assert live_page(site) == 'ClickGraft 2' and (site / 'html').stat().st_ino == html_inode
    [kept] = leftovers(site, '.previous-')
    assert (site / kept / 'html/index.html').read_text() == 'ClickGraft 1'

    # Double fault: the exchange works, the install then fails on a directory
    # squatting where the classifier's pending copy goes, and the exchange
    # back fails with EIO. The printed command must put the old site back.
    site = tmp_path / 'double'
    site.mkdir()
    publish.publish(staged(site, '.incoming-one', '1'), site)
    incoming = staged(site, '.incoming-two', '2')
    # The printed command runs the staged copy, as it would after a real deploy.
    shutil.copy(SCRIPT, incoming / 'publish_site.py')
    (site / 'visitor-classify.awk.pending-.incoming-two').mkdir()
    run = run_script(incoming, site, LD_PRELOAD=shim, SHIM_PASS='1', SHIM_ERRNO=str(errno.EIO))
    assert run.returncode == 1 and 'Traceback' not in run.stderr
    assert run.stderr.startswith('✗ Publishing failed')
    command = next(line.strip() for line in run.stderr.splitlines() if '--restore' in line)
    assert live_page(site) == 'ClickGraft 2'
    restored = subprocess.run(command.replace('python3', sys.executable, 1).split(), capture_output=True, text=True)
    assert restored.returncode == 0, restored.stderr
    assert live_page(site) == 'ClickGraft 1'
    assert (site / 'collector/collector.py').read_text() == '1'
