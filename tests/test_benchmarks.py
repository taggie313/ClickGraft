import importlib.util
from pathlib import Path
import sys
import time
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'benchmarks'))
import common as benchmark
import perf_ab2 as perf
from tests.test_verify_safety import stand_in, bundles, _start, _stop


def test_rejects_historical_non_app_path(tmp_path):
    with pytest.raises(ValueError, match='.app bundle'):
        benchmark.inspect(tmp_path / 'IntelWork', 'x86_64')


def test_failed_runs_do_not_enter_median():
    assert perf.med([{'error': 'timed out', 'cpu_s': 1}, {'cpu_s': 20}], 'cpu_s') == 20
    assert perf.med([{'error': 'crashed', 'cpu_s': 1}], 'cpu_s') is None


def test_private_launch_cleanup_spares_owner(bundles, monkeypatch):
    target, _ = bundles
    monkeypatch.setattr(benchmark, 'get_archs', lambda _: ['x86_64'])
    monkeypatch.setenv('CG_FAKE_MODE', 'sleep')
    owner = _start(target, 'sleep')
    try:
        with benchmark.launch(target, {}) as (proc, folder, mark):
            assert (Path(folder) / 'home/Library/Application Support/hpclick/userpref.json').is_file()
            time.sleep(0.1)
            assert proc.poll() is None
        assert proc.poll() is not None
        assert owner.poll() is None
        assert not Path(folder).exists()
    finally:
        _stop(owner)


def test_apple_silicon_is_started_through_its_own_launcher(bundles, monkeypatch, tmp_path):
    """What is timed has to be what a user runs. The harness used to run
    HPClickExe with a DYLD_INSERT_LIBRARIES list of its own, which left out the
    PNG shim the launcher inserts (verify.smoke_launch had the same defect
    until 22 Sep 2026)."""
    target, _ = bundles
    monkeypatch.setattr(benchmark, 'get_archs', lambda _: ['arm64'])
    monkeypatch.setenv('CG_FAKE_MODE', 'syntax')
    record = tmp_path / 'launch-record'
    monkeypatch.setenv('CG_FAKE_RECORD', str(record))
    with benchmark.launch(target, {}) as (proc, folder, mark):
        for _ in range(100):
            if record.exists():
                break
            time.sleep(0.05)
        assert proc.poll() is None
    first_argument, _tmpdir, _home, launched_by = record.read_text().splitlines()[:4]
    assert launched_by == 'the launcher'
    assert first_argument == '--user-data-dir=' + folder + '/user-data'


def test_apple_silicon_input_without_its_preloads_is_refused(bundles, monkeypatch):
    """A copy the launcher does not load its support libraries into is not a
    ClickGraft build anyone runs, so it is refused before it is timed."""
    target, _ = bundles
    monkeypatch.setattr(benchmark, 'get_archs', lambda _: ['arm64'])
    manifest = {'required_dylibs': [{'name': 'libidn2.0.dylib', 'preload': True}]}
    with pytest.raises(ValueError, match='does not preload libidn2.0.dylib'):
        with benchmark.launch(target, manifest):
            pass


def test_missing_milestone_never_counts_as_finished(bundles, monkeypatch):
    target, _ = bundles
    monkeypatch.setattr(benchmark, 'get_archs', lambda _: ['x86_64'])
    monkeypatch.setattr(perf, 'CAP', 0.3)
    monkeypatch.setattr(perf, 'LOG_QUIET', 0)
    monkeypatch.setenv('CG_FAKE_MODE', 'sleep')
    result = perf.run_once(target, {}, 'test')
    assert not result['reached']
    assert 'error' in result


def test_incomplete_cleanup_retains_profile(bundles, monkeypatch):
    import shutil
    target, _ = bundles
    monkeypatch.setattr(benchmark, 'get_archs', lambda _: ['x86_64'])
    monkeypatch.setenv('CG_FAKE_MODE', 'sleep')
    # The root is reaped separately; simulate an unkillable owned descendant.
    monkeypatch.setattr(benchmark.verify, 'kill_hpclick_processes', lambda *a, **kw: False)
    folder = None
    try:
        with pytest.raises(RuntimeError, match='profile retained'):
            with benchmark.launch(target, {}) as (proc, folder, mark):
                pass
        assert Path(folder).is_dir()
        assert proc.poll() is not None
    finally:
        if folder:
            shutil.rmtree(folder)
