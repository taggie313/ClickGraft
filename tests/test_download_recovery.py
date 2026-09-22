"""Exercise failed reads, poisoned caches, and concurrent cache publication.

Every download ClickGraft makes goes through clickgraft/deps.py: Electron's
archive from GitHub and four Homebrew bottles from ghcr.io. Until the 22 Sep
2026 review a failed or interrupted one could leave a partial file under the
real name, a stalled one held the build for ever, and the first fix for that
stopped slow downloads that were still arriving. The timing tests here run
against a real HTTP server on 127.0.0.1, with the stall limit scaled down.
"""
import hashlib
import http.server
import io
import json
import os
import socket
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from clickgraft import deps

VERSION = '39.8.4'
NAME = f'electron-v{VERSION}-darwin-arm64.zip'
PAYLOAD = b'controlled electron archive'
SUMS = (hashlib.sha256(PAYLOAD).hexdigest() + '  ' + NAME + '\n').encode()
GITHUB = f'https://github.com/electron/electron/releases/download/v{VERSION}/'

# The timing tests run at this stall limit instead of deps.STALL_SECONDS, and
# the five-minute cap on a whole download that the review removed, scaled the
# same way, is OLD_CAP.
TEST_STALL = 0.5
OLD_CAP = 300 * TEST_STALL / deps.STALL_SECONDS


def responses(monkeypatch, fail=None):
    calls = []
    def open_url(req, timeout):
        assert timeout == deps.STALL_SECONDS
        calls.append(req.full_url)
        if fail and fail in req.full_url:
            class Interrupted(io.BytesIO):
                def read1(self, size):
                    if self.tell():
                        raise TimeoutError('read stalled')
                    return super().read1(3)
            return Interrupted(SUMS if req.full_url.endswith('.txt') else PAYLOAD)
        return io.BytesIO(SUMS if req.full_url.endswith('.txt') else PAYLOAD)
    monkeypatch.setattr(deps.urllib.request, 'urlopen', open_url)
    return calls


@pytest.mark.parametrize('broken', [b'', b'abc', b'bad  ' + NAME.encode(), b'f' * 64 + b'  other.zip'])
def test_bad_checksum_cache_heals(tmp_path, monkeypatch, broken):
    """1.5.9 never fetched SHASUMS256.txt again once cached, so a truncated
    copy failed every build until someone deleted it by hand."""
    (tmp_path / f'SHASUMS256-{VERSION}.txt').write_bytes(broken)
    calls = responses(monkeypatch)
    assert Path(deps.fetch_electron(VERSION, str(tmp_path))).read_bytes() == PAYLOAD
    assert len(calls) == 2


@pytest.mark.parametrize('stage', ['.txt', '.zip'])
def test_interrupted_download_can_retry(tmp_path, monkeypatch, stage):
    """An interrupted download leaves nothing under the real name, and says
    it stalled rather than urllib's bare "timed out"."""
    responses(monkeypatch, fail=stage)
    with pytest.raises(deps.DownloadError, match='stalled'):
        deps.fetch_electron(VERSION, str(tmp_path))
    assert not (tmp_path / NAME).exists()
    assert not list(tmp_path.glob('.download-*'))
    responses(monkeypatch)
    assert Path(deps.fetch_electron(VERSION, str(tmp_path))).read_bytes() == PAYLOAD


def test_hash_mismatch_never_published(tmp_path, monkeypatch):
    """Bytes that fail their checksum are never renamed into the cache."""
    def open_url(req, timeout):
        return io.BytesIO(SUMS if req.full_url.endswith('.txt') else b'corrupt')
    monkeypatch.setattr(deps.urllib.request, 'urlopen', open_url)
    with pytest.raises(ValueError, match='does not match the SHA-256'):
        deps.fetch_electron(VERSION, str(tmp_path))
    assert not (tmp_path / NAME).exists()


def test_concurrent_fetches_publish_once(tmp_path, monkeypatch):
    """Two builds sharing the cache download once; the second waits and uses
    the first one's archive."""
    calls = responses(monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        paths = list(pool.map(lambda _: deps.fetch_electron(VERSION, str(tmp_path)), range(2)))
    assert paths[0] == paths[1]
    assert len(calls) == 2
    assert Path(paths[0]).read_bytes() == PAYLOAD


# ---------------------------------------------------------------------------
# Timing, against a real server


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass            # the client hanging up on a stalled reply is the point


@pytest.fixture
def server(monkeypatch):
    """A local server that urlopen is pointed at in place of the real hosts.

    server['mode'] picks the behaviour: steady (a small piece every
    TEST_STALL / 5 seconds, for 2 * OLD_CAP), stall (a few bytes, then
    silence), short (a few bytes, then it hangs up), gone (404).
    """
    release = threading.Event()
    piece = bytes(range(256)) * 4
    steady = piece * round(2 * OLD_CAP / (TEST_STALL / 5))

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, length):
            self.send_response(200)
            self.send_header('Content-Length', str(length))
            self.end_headers()

        def do_GET(self):
            mode = self.path.lstrip('/').partition('/')[0]
            if mode == 'steady':
                self.reply(len(steady))
                for i in range(0, len(steady), len(piece)):
                    self.wfile.write(steady[i:i + len(piece)])
                    self.wfile.flush()
                    time.sleep(TEST_STALL / 5)
            elif mode in ('stall', 'short'):
                self.reply(1000)
                self.wfile.write(b'x' * 10)
                self.wfile.flush()
                if mode == 'stall':
                    release.wait(10)
            else:
                self.send_error(404)

    httpd = _Server(('127.0.0.1', 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    real_urlopen = urllib.request.urlopen
    state = {'mode': 'gone', 'port': httpd.server_address[1], 'steady': steady}

    def open_url(req, timeout):
        assert timeout == deps.STALL_SECONDS
        path = req.full_url.split('://', 1)[1].split('/', 1)[1]
        local = urllib.request.Request(f"http://127.0.0.1:{state['port']}/{state['mode']}/{path}",
                                       headers=dict(req.header_items()))
        return real_urlopen(local, timeout=timeout)

    monkeypatch.setattr(deps, 'STALL_SECONDS', TEST_STALL)
    monkeypatch.setattr(deps.urllib.request, 'urlopen', open_url)
    yield state
    release.set()
    httpd.shutdown()
    httpd.server_close()


def test_slow_steady_download_outlasts_the_old_cap(tmp_path, server):
    """A download still arriving is never cut off, however long it takes.

    Measured in the 22 Sep 2026 review: a steady 200 KB/s download, with the
    old five-minute cap scaled down, was stopped with "The download exceeded
    five minutes" although bytes were arriving every 0.1 s. At the real cap
    that stopped Electron's 112 MB archive on any link below about 3 Mbit/s,
    on every retry.
    """
    server['mode'] = 'steady'
    started = time.monotonic()
    archive = deps.fetch_electron(VERSION, str(tmp_path),
                                  sha256=hashlib.sha256(server['steady']).hexdigest())
    took = time.monotonic() - started
    assert Path(archive).read_bytes() == server['steady']
    assert took > OLD_CAP, (took, OLD_CAP)
    assert took > 3 * TEST_STALL, 'the test must outlast the stall limit several times over'


def test_stalled_download_fails_promptly_and_says_so(tmp_path, server):
    """Nothing arriving for the stall limit fails the download, and the
    message names the file, the host and what happened."""
    server['mode'] = 'stall'
    started = time.monotonic()
    with pytest.raises(deps.DownloadError) as exc:
        deps.fetch_electron(VERSION, str(tmp_path), sha256='0' * 64)
    took = time.monotonic() - started
    message = str(exc.value)
    assert 'stalled' in message and 'github.com' in message
    assert f'Electron {VERSION}' in message
    assert f'{TEST_STALL:g} seconds' in message
    assert took < 5, 'it must not wait for the server to give up'
    assert not (tmp_path / NAME).exists()
    assert not list(tmp_path.glob('.download-*'))


def test_connection_cut_short_is_not_called_a_bad_file(tmp_path, server):
    """A body that ends before its Content-Length is a broken connection,
    not a checksum mismatch that reads as if the file had been altered."""
    server['mode'] = 'short'
    with pytest.raises(deps.DownloadError, match='broke during the download') as exc:
        deps.fetch_electron(VERSION, str(tmp_path), sha256='0' * 64)
    assert '990 bytes still to come' in str(exc.value)


def test_no_network_says_so(tmp_path, monkeypatch):
    """A host that cannot be reached is said to be unreachable, by name."""
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        closed_port = s.getsockname()[1]
    real_urlopen = urllib.request.urlopen
    monkeypatch.setattr(deps.urllib.request, 'urlopen',
                        lambda req, timeout: real_urlopen(f'http://127.0.0.1:{closed_port}/', timeout=timeout))
    with pytest.raises(deps.DownloadError) as exc:
        deps.fetch_electron(VERSION, str(tmp_path), sha256='0' * 64)
    assert 'Could not reach github.com' in str(exc.value)
    assert 'online' in str(exc.value)


def test_missing_pinned_electron_says_a_newer_clickgraft_is_needed(tmp_path, server):
    """A 404 on a pinned file never heals by retrying, so it is not an
    OSError: whatever treats OSError as "network down, skip or retry" must
    not swallow it. Measured 22 Sep 2026: the release gate's network test
    reported a dead pin as a skip."""
    server['mode'] = 'gone'
    with pytest.raises(deps.DownloadGone) as exc:
        deps.fetch_electron(VERSION, str(tmp_path), sha256='0' * 64)
    assert not isinstance(exc.value, OSError)
    message = str(exc.value)
    assert f'Electron {VERSION}' in message and 'github.com' in message
    assert 'HTTP 404' in message and 'newer ClickGraft' in message


def test_missing_pinned_bottle_names_the_library_and_host(tmp_path, server, monkeypatch):
    """What a person sees when a pinned Homebrew blob disappears. On 22 Sep
    2026 it was "[ERROR] build: HTTP Error 404: Not Found"."""
    monkeypatch.setattr(deps, 'find_local_brew_dylib', lambda name: None)
    info = {'name': 'libidn2.0.dylib', 'brew_formula': 'libidn2'}
    digest = 'd' * 64
    bottle = {'tag': 'arm64_ventura', 'sha256': digest, 'dylib_sha256': 'e' * 64,
              'url': f'https://ghcr.io/v2/homebrew/core/libidn2/blobs/sha256:{digest}'}
    server['mode'] = 'gone'
    with pytest.raises(deps.DownloadGone) as exc:
        deps.resolve_dylib(info, cache_dir=str(tmp_path), bottle=bottle)
    message = str(exc.value)
    assert 'libidn2.0.dylib' in message and 'ghcr.io' in message
    assert 'newer ClickGraft' in message
    assert not list(tmp_path.glob('.bottle-*')) and not (tmp_path / 'libidn2.0.dylib').exists()


# ---------------------------------------------------------------------------
# The cache lock and what a killed build leaves behind


def test_a_killed_builds_temporary_files_are_swept(tmp_path, monkeypatch):
    """Measured 22 Sep 2026: a build SIGKILLed mid-download left a
    .download-* file in the cache, and nothing ever removed it. Only files
    older than the lock's idle limit go; a fresh one could be a live
    download on a file system where the lock does not hold."""
    archive = tmp_path / NAME
    archive.write_bytes(PAYLOAD)
    old = time.time() - deps.LOCK_IDLE_SECONDS - 60
    stale_file = tmp_path / '.download-q7yveawj'
    stale_file.write_bytes(b'partial electron')
    stale_dir = tmp_path / '.bottle-abc123'
    stale_dir.mkdir()
    (stale_dir / 'gettext.tar.gz').write_bytes(b'partial bottle')
    for p in (stale_dir / 'gettext.tar.gz', stale_dir, stale_file):
        os.utime(p, (old, old))
    fresh = tmp_path / '.download-inflight'
    fresh.write_bytes(b'still arriving')

    def offline(*args, **kwargs):
        pytest.fail('a cached, matching archive needs no network')
    monkeypatch.setattr(deps.urllib.request, 'urlopen', offline)
    deps.fetch_electron(VERSION, str(tmp_path), sha256=hashlib.sha256(PAYLOAD).hexdigest())
    assert not stale_file.exists() and not stale_dir.exists()
    assert fresh.exists() and archive.exists() and (tmp_path / '.download.lock').exists()


def test_a_waiting_build_outlasts_a_slow_holder_but_not_a_stuck_one(tmp_path, monkeypatch):
    """The lock wait used to be the five-minute download cap, so a second
    build gave up behind a first one whose download was slow but arriving."""
    monkeypatch.setattr(deps, 'LOCK_IDLE_SECONDS', 0.5)
    held, done = threading.Event(), threading.Event()

    def holder(progressing, seconds):
        with deps._cache_lock(str(tmp_path)):
            held.set()
            until = time.monotonic() + seconds
            while time.monotonic() < until:
                time.sleep(0.02)
                if progressing:
                    deps._note_progress()
            done.set()

    t = threading.Thread(target=holder, args=(True, 3 * deps.LOCK_IDLE_SECONDS))
    t.start()
    held.wait(5)
    with deps._cache_lock(str(tmp_path)):
        assert done.is_set()
    t.join()

    held.clear()
    done.clear()
    t = threading.Thread(target=holder, args=(False, 4 * deps.LOCK_IDLE_SECONDS))
    t.start()
    held.wait(5)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match='received nothing for 0.5 seconds'):
        with deps._cache_lock(str(tmp_path)):
            pass
    assert time.monotonic() - started < 2 * deps.LOCK_IDLE_SECONDS
    assert not done.is_set()
    t.join()


# ---------------------------------------------------------------------------
# Pins


def test_shipped_dependency_selection_needs_no_live_api(monkeypatch):
    """Shipped manifests pin every bottle, so choosing them asks nothing of
    Homebrew's formula API, whose answers change as Homebrew moves on."""
    from clickgraft.manifest import ManifestManager
    def forbidden(*args, **kwargs):
        pytest.fail('pinned dependency selection queried the changing formula API')
    monkeypatch.setattr(deps, '_http_get', forbidden)
    for manifest in ManifestManager().manifests.values():
        chosen = deps.choose_bottles(manifest)
        assert all(b['dylib_sha256'] for b in chosen.values())
        assert len(manifest['electron_sha256']) == 64
        assert chosen == deps.choose_bottles(manifest)


def test_every_shipped_pin_is_well_formed_and_dated():
    """Each bottle's url names the blob its sha256 gives, electron_sha256 is
    a SHA-256, and the manifest says when and how the pins were measured.
    Measured 22 Sep 2026: none of this was checked, so a mismatched url or
    an electron_sha256 of "XYZ" would have failed every build as a checksum
    "mismatch" blamed on the download."""
    from clickgraft.manifest import ManifestManager
    manifests = ManifestManager().manifests
    assert manifests
    for version, manifest in manifests.items():
        deps.check_pins(manifest)
        assert deps._SHA256.fullmatch(manifest['electron_sha256']), version
        assert '22 Sep 2026' in manifest['$bottles_pinned'], version
        assert '22 Sep 2026' in manifest['$electron_sha256_measured'], version
        for info in manifest['required_dylibs']:
            bottle = info['bottle']
            assert bottle['url'].endswith('/blobs/sha256:' + bottle['sha256']), (version, info['name'])
            assert bottle['version'] in manifest['$bottles_pinned'], (version, info['name'])
            assert bottle['tag'] in manifest['$bottles_pinned'], (version, info['name'])


def _pinned_manifest():
    from clickgraft.manifest import ManifestManager
    return json.loads(json.dumps(ManifestManager().find_manifest(app_version='4.10.42')))


@pytest.mark.parametrize('damage, says', [
    (lambda m: m.update(electron_sha256='XYZ'), 'electron_sha256'),
    (lambda m: m['required_dylibs'][2]['bottle'].update(sha256='a' * 64), "'gettext'"),
    (lambda m: m['required_dylibs'][0]['bottle'].update(dylib_sha256='A' * 64), "'libidn2'"),
    (lambda m: m['required_dylibs'][1]['bottle'].update(tag='arm64_linux'), "'libunistring'"),
    (lambda m: m['required_dylibs'][3]['bottle'].update(url='http://example.com/x'), "'libnghttp2'"),
    (lambda m: m['required_dylibs'][3].pop('bottle'), 'libnghttp2.14.dylib'),
])
def test_a_malformed_pin_fails_before_any_download(monkeypatch, damage, says):
    """Loading the pins refuses, naming the formula, before a byte is fetched."""
    def forbidden(*args, **kwargs):
        pytest.fail('a malformed pin reached the network')
    monkeypatch.setattr(deps, '_http_get', forbidden)
    monkeypatch.setattr(deps.urllib.request, 'urlopen', forbidden)
    manifest = _pinned_manifest()
    damage(manifest)
    with pytest.raises(ValueError, match=says):
        deps.choose_bottles(manifest)


def test_a_malformed_electron_pin_fails_before_any_download(tmp_path, monkeypatch):
    """fetch_electron refuses a pinned hash that is not a SHA-256 without
    touching the network or the cache."""
    def forbidden(*args, **kwargs):
        pytest.fail('a malformed pin reached the network')
    monkeypatch.setattr(deps.urllib.request, 'urlopen', forbidden)
    with pytest.raises(ValueError, match='64 lowercase hex'):
        deps.fetch_electron(VERSION, str(tmp_path), sha256='XYZ')
    assert not list(tmp_path.iterdir())


def test_pinned_electron_ignores_sticky_checksum_file(tmp_path, monkeypatch):
    """With the archive's hash pinned, a bad cached SHASUMS256.txt is never
    read, and the release's copy is never fetched."""
    (tmp_path / f'SHASUMS256-{VERSION}.txt').write_text('incomplete')
    calls = responses(monkeypatch)
    archive = deps.fetch_electron(VERSION, str(tmp_path), sha256=hashlib.sha256(PAYLOAD).hexdigest())
    assert Path(archive).read_bytes() == PAYLOAD
    assert calls == [GITHUB + NAME]
