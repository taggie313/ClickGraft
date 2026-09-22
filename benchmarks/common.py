"""Inputs, dedicated copies and private launch ownership shared by both benchmarks."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile

from clickgraft import verify
from clickgraft.asar import AsarArchive, patch_and_repack_asar
from clickgraft.bundle_audits import check_launcher
from clickgraft.manifest import ManifestManager
from clickgraft.manifest_guard import check_manifest
from clickgraft.macho import get_archs
from clickgraft.patches import PatchEngine
from clickgraft.signing import sign_bundle


def arguments(description):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('--arm', required=True, help='Verified Apple Silicon copy')
    parser.add_argument('--intel', required=True, help='Stock Intel app of the same version')
    parser.add_argument('--runs', type=int, default=3)
    parser.add_argument('--output', required=True, help='JSON results path')
    args = parser.parse_args()
    if args.runs < 1:
        parser.error('--runs must be positive')
    return args


def inspect(bundle, architecture):
    bundle = Path(bundle).resolve()
    if bundle.suffix != '.app':
        raise ValueError(f'{bundle}: supply an .app bundle')
    with (bundle / 'Contents/Info.plist').open('rb') as source:
        info = plistlib.load(source)
    executable = bundle / 'Contents/MacOS/HPClickExe'
    if architecture not in get_archs(str(executable)):
        raise ValueError(f'{bundle}: HPClickExe must contain {architecture}')
    return {'path': str(bundle), 'version': info['CFBundleShortVersionString'],
            'architecture': architecture,
            'asar_sha256': hashlib.sha256((bundle / 'Contents/Resources/app.asar').read_bytes()).hexdigest()}


@contextmanager
def prepared_pair(arm, intel):
    inputs = {'arm64': inspect(arm, 'arm64'), 'x86_64': inspect(intel, 'x86_64')}
    if inputs['arm64']['version'] != inputs['x86_64']['version']:
        raise ValueError('Both benchmark inputs must be the same HP Click version')
    manifest = ManifestManager().find_manifest(asar_sha256=inputs['x86_64']['asar_sha256'])
    if manifest is None:
        raise ValueError('Intel input must be a supported, unchanged stock app')
    check_manifest(manifest)
    folder = tempfile.mkdtemp(prefix='cg-benchmark-', dir='/private/tmp')
    copies = {}
    try:
        for architecture, info in inputs.items():
            copy = str(Path(folder) / (architecture + '.app'))
            subprocess.run(['ditto', info['path'], copy], check=True)
            copies[architecture] = copy
        # Apply the same allowed JS repairs/settings to the Intel control. No
        # architecture changes, and the caller's installed inputs stay untouched.
        archive = copies['x86_64'] + '/Contents/Resources/app.asar'
        patch_and_repack_asar(archive, archive, PatchEngine(manifest['patches']), manifest)
        sign_bundle(copies['x86_64'])
        for bundle in copies.values():
            archive = AsarArchive(bundle + '/Contents/Resources/app.asar')
            nodes = archive.get_all_file_nodes()
            verify.check_patch_outcomes(lambda path: archive.read_file_content(nodes[path]).decode() if path in nodes else None, manifest)
            updater = archive.read_file_content(nodes['app/node/main/app-updater.js']).decode()
            if 'function startup(e){return;' not in updater:
                raise ValueError('Both copies must have the updater disabled')
        yield copies, manifest, inputs
    finally:
        if any(verify.processes_inside(bundle) for bundle in copies.values()):
            raise RuntimeError(f"A benchmark copy is still open; retained the copies at {folder}")
        shutil.rmtree(folder)


@contextmanager
def launch(bundle, manifest, extra=()):
    folder = tempfile.mkdtemp(prefix='cg-bench-run-', dir='/private/tmp')
    safe_to_remove = True
    try:
        resources = bundle + '/Contents/Resources/app/appData/macx'
        env = os.environ.copy()
        for key in list(env):
            if key.startswith('DYLD_'):
                del env[key]
        env.update(HOME=verify.private_profile(folder), TMPDIR=folder + '/')
        if 'arm64' in get_archs(bundle + '/Contents/MacOS/HPClickExe'):
            # The ClickGraft copy is started through its own launcher, the way
            # macOS starts it, and given nothing DYLD_*: the launcher sets all
            # three variables itself. Building DYLD_INSERT_LIBRARIES here from
            # the manifest's preload names instead left out the PNG shim the
            # launcher also inserts (the same defect verify.smoke_launch had
            # until 22 Sep 2026), so the arm64 numbers came from a
            # configuration nobody runs. check_launcher is what verification
            # uses, so an input whose launcher is wrong or is missing a
            # library is refused before it is timed.
            check_launcher(bundle, manifest)
            command = bundle + '/Contents/MacOS/HP Click'
        else:
            # The stock Intel control keeps the direct HPClickExe launch. HP's
            # own launcher execs it without "$@" (read 22 Sep 2026 in stock
            # 4.8.117), so --user-data-dir would be dropped and the run would
            # share the owner's profile and single-instance lock. These are the
            # two variables that launcher exports, and all it does.
            env.update(DYLD_FRAMEWORK_PATH=resources + '/Frameworks',
                       DYLD_LIBRARY_PATH=resources + '/lib')
            command = bundle + '/Contents/MacOS/HPClickExe'
        proc = subprocess.Popen([command, '--user-data-dir=' + folder + '/user-data', *extra],
                                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        mark = os.path.basename(folder)
        try:
            yield proc, folder, mark
        finally:
            safe_to_remove = False
            safe_to_remove = verify.kill_hpclick_processes(bundle, mark=mark, root_pid=proc.pid)
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            if not safe_to_remove:
                raise RuntimeError(f"Benchmark cleanup could not stop every owned process; profile retained at {folder}")
    finally:
        if safe_to_remove:
            shutil.rmtree(folder)
