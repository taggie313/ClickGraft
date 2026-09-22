"""packaging/check_release.py: the ZIP is tied to its own release tag.

These build a small repository with a tag, and a ZIP laid out as
build_app.sh and sign_and_notarize.sh lay it out, without the signature:
codesign, stapler and Gatekeeper need a Developer ID and Apple's notary, so
the signature half is only exercised by a real release. Everything before it
runs here, on Linux in CI too.
"""
import importlib.util
import json
from pathlib import Path
import plistlib
import subprocess
import zipfile

import pytest

spec = importlib.util.spec_from_file_location(
    'release_gate', Path(__file__).resolve().parents[1] / 'packaging/check_release.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

VERSION = '9.1.0'
# A version the allowance is actually for: every release up to 1.5.9 was built
# before build_app.sh wrote a source record (gate.LEGACY_UNTIL).
LEGACY = '1.5.9'
PREFIX = 'ClickGraft.app/Contents/'
SOURCES = {
    'LICENSE': 'MIT',
    'NOTICE': 'notice',
    'packaging/AppIcon.icns': 'icon',
    'packaging/ClickGraft.swift': 'print("wizard")',
    'packaging/BackendTransport.swift': 'struct Transport {}',
    'packaging/release.json': '{"importance": "recommended"}',
    'packaging/sign_and_notarize.sh': 'exit 0',
    'clickgraft/__init__.py': '',
    'clickgraft/build.py': 'original',
    'clickgraft/data/printers-4.8.117.json': '{"printers": []}',
    'clickgraft/shims/pngshim.c': 'int shim;',
    'manifests/4.8.117.json': '{"patches": []}',
    'site/index.html': '<p>ClickGraft</p>',
}


def git(repo, *args):
    # Settings a developer's own git config could override: signing, hooks.
    return subprocess.run(['git', '-c', 'user.name=Gate Test', '-c', 'user.email=gate@example.invalid',
                           '-c', 'commit.gpgsign=false', '-c', 'tag.gpgsign=false',
                           '-c', 'core.hooksPath=/dev/null', *args],
                          cwd=repo, check=True, capture_output=True)


def write(repo, files):
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)


def commit(repo, message):
    git(repo, 'add', '-A')
    git(repo, 'commit', '-q', '-m', message)


def released(tmp_path, version=VERSION, builds=None):
    """A repository whose tag v<version> is the release commit."""
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q')
    write(repo, SOURCES)
    write(repo, {'packaging/build_app.sh': f'VERSION="${{CLICKGRAFT_VERSION:-{builds or version}}}"\n'})
    commit(repo, f'ClickGraft {version}')
    git(repo, 'tag', '-a', f'v{version}', '-m', f'ClickGraft {version}')
    return repo


def build_zip(repo, version=VERSION, record=True, add=None, change=None, drop=None):
    """dist/ClickGraft.zip as the release scripts make it from the working tree,
    with the record build_app.sh writes; then `add`, `change` or `drop` files
    in it ({path under Contents/: text}) as if dist/ had been touched."""
    source = gate.source_record(repo)
    files = {'Info.plist': plistlib.dumps({'CFBundleShortVersionString': version, 'CFBundleVersion': version}),
             'MacOS/ClickGraft': b'launcher', '_CodeSignature/CodeResources': b'seal', 'CodeResources': b'ticket'}
    if record:
        files[gate.RECORD] = json.dumps(source).encode()
    for name in source:
        if gate.app_path(name):
            files[gate.app_path(name)] = (repo / name).read_bytes()
    files.update({name: text.encode() for name, text in (add or {}).items()})
    files.update({name: text.encode() for name, text in (change or {}).items()})
    for name in drop or ():
        del files[name]
    archive = repo.parent / 'ClickGraft.zip'
    with zipfile.ZipFile(archive, 'w') as out:
        for name, data in files.items():
            out.writestr(PREFIX + name, data)
    return archive


def check(archive, repo, **kwargs):
    lines = []
    gate.check_artifact(archive, repo, check_platform=False, say=lines.append, **kwargs)
    return lines


def test_record_covers_every_file_build_app_ships(tmp_path):
    write(tmp_path, SOURCES)
    write(tmp_path, {'packaging/build_app.sh': 'VERSION=1', 'manifests/extra/nested.json': '{}',
                     'clickgraft/__pycache__/build.cpython-39.pyc': 'x', 'clickgraft/stray.pyc': 'x',
                     'clickgraft/.DS_Store': 'x', 'manifests/.DS_Store': 'x', 'packaging/make-icon.sh': 'x'})
    assert sorted(gate.source_record(tmp_path)) == [
        'LICENSE', 'NOTICE',
        'clickgraft/__init__.py', 'clickgraft/build.py', 'clickgraft/data/printers-4.8.117.json',
        'clickgraft/shims/pngshim.c',
        'manifests/4.8.117.json', 'manifests/extra/nested.json',
        'packaging/AppIcon.icns', 'packaging/BackendTransport.swift', 'packaging/ClickGraft.swift',
        'packaging/build_app.sh',
    ]


def test_tagged_zip_passes_after_a_later_site_only_commit(tmp_path):
    """The routine case the first draft refused: HEAD has moved on past the
    tag, with site copy and with work towards the next release, the working
    tree is dirty as well, and the live ZIP is deployed again."""
    repo = released(tmp_path)
    archive = build_zip(repo)
    write(repo, {'site/index.html': '<p>new copy</p>'})
    commit(repo, 'Site copy')
    write(repo, {'clickgraft/build.py': 'next release', 'manifests/4.11.31.json': '{"patches": []}'})
    commit(repo, 'Work towards the next release')
    write(repo, {'clickgraft/build.py': 'work in progress', 'clickgraft/new.py': 'untracked'})
    lines = check(archive, repo)
    assert lines[0] == f'  ✓ ClickGraft {VERSION}, and v{VERSION} builds {VERSION}'
    assert any('all 11 sources match' in line for line in lines)
    assert any('exactly the 8 files committed' in line for line in lines)


@pytest.mark.parametrize('stale', ['clickgraft/data/printers-4.8.117.json', 'clickgraft/shims/pngshim.c',
                                   'packaging/ClickGraft.swift', 'packaging/build_app.sh', 'NOTICE'])
def test_zip_built_before_a_source_changed_is_refused(tmp_path, stale):
    """Built, then a source changed and the tag was moved onto the change:
    the notarised binary no longer matches what the tag says it is."""
    repo = released(tmp_path)
    archive = build_zip(repo)
    (repo / stale).write_text((repo / stale).read_text() + '\n# changed after the build\n')
    commit(repo, 'Change after the build')
    git(repo, 'tag', '-f', '-a', f'v{VERSION}', '-m', 'moved')
    with pytest.raises(gate.Refused, match='differ from v9.1.0') as refused:
        check(archive, repo)
    assert refused.value.details == [f'{stale}: differs from v{VERSION}']


@pytest.mark.parametrize('extra', ['Resources/manifests/personal.json', 'Resources/clickgraft/extra.py',
                                   'Resources/clickgraft/data/extra.json', 'Resources/extra.txt',
                                   'MacOS/helper'])
def test_file_added_to_the_app_after_the_build_is_refused(tmp_path, extra):
    repo = released(tmp_path)
    with pytest.raises(gate.Refused, match='does not match v9.1.0') as refused:
        check(build_zip(repo, add={extra: '{"patches": []}'}), repo)
    assert refused.value.details == [f'{extra}: in the app, not in v{VERSION}']


def test_changed_payload_is_refused(tmp_path):
    repo = released(tmp_path)
    with pytest.raises(gate.Refused, match='does not match') as refused:
        check(build_zip(repo, change={'Resources/clickgraft/shims/pngshim.c': 'int other;'}), repo)
    assert refused.value.details == [f'Resources/clickgraft/shims/pngshim.c: differs from v{VERSION}']


def test_missing_payload_file_is_refused(tmp_path):
    repo = released(tmp_path)
    with pytest.raises(gate.Refused) as refused:
        check(build_zip(repo, drop=['Resources/manifests/4.8.117.json']), repo)
    assert refused.value.details == [f'Resources/manifests/4.8.117.json: in v{VERSION}, not in the app']


def test_file_outside_the_app_is_refused(tmp_path):
    repo = released(tmp_path)
    archive = build_zip(repo)
    with zipfile.ZipFile(archive, 'a') as out:
        out.writestr('__MACOSX/._ClickGraft.app', b'x')
    with pytest.raises(gate.Refused, match='outside'):
        check(archive, repo)


def test_missing_tag_is_refused_in_plain_words(tmp_path):
    repo = released(tmp_path)
    with pytest.raises(gate.Refused, match='There is no tag v9.2.0 in this checkout'):
        check(build_zip(repo, version='9.2.0'), repo)


def test_tag_that_builds_another_version_is_refused(tmp_path):
    repo = released(tmp_path, builds='9.1.1')
    with pytest.raises(gate.Refused, match="v9.1.0's build_app.sh builds 9.1.1"):
        check(build_zip(repo), repo)


def test_plist_versions_that_disagree_are_refused(tmp_path):
    repo = released(tmp_path)
    archive = build_zip(repo, change={'Info.plist': plistlib.dumps(
        {'CFBundleShortVersionString': VERSION, 'CFBundleVersion': '9.0.0'}).decode()})
    with pytest.raises(gate.Refused, match='no single version'):
        check(archive, repo)


def test_not_a_checkout_is_refused(tmp_path):
    repo = released(tmp_path)
    archive = build_zip(repo)
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    with pytest.raises(gate.Refused, match='cannot read .* as a checkout'):
        check(archive, elsewhere)


def test_zip_without_a_record_is_refused_in_one_line(tmp_path, monkeypatch, capsys):
    """Every release up to 1.5.9 has no record. The operator gets one line
    saying so and what to do, not a KeyError."""
    repo = released(tmp_path, LEGACY)
    archive = build_zip(repo, LEGACY, record=False)
    monkeypatch.setattr(gate, 'ROOT', repo)
    monkeypatch.delenv(gate.LEGACY_ENV, raising=False)
    assert gate.main(['--artifact', str(archive)]) == 1
    err = capsys.readouterr().err.splitlines()
    assert len(err) == 1 and err[0].startswith(f'  ✗ This {LEGACY} ZIP has no source record')
    assert f'{gate.LEGACY_ENV}={LEGACY}' in err[0]


@pytest.mark.parametrize('version', ['1.5.10', '1.6.0', '2.0.0'])
def test_a_record_is_not_optional_after_1_5_9(tmp_path, version):
    """A ZIP of a version that always carries a record, and hasn't one, came
    from a build_app.sh older than the checkout: the stale binary the gate is
    for. It is refused, and no allowance is offered or accepted."""
    repo = released(tmp_path, version)
    archive = build_zip(repo, version, record=False)
    for allowed in (None, version):
        with pytest.raises(gate.Refused) as refused:
            check(archive, repo, allow_legacy=allowed)
        assert 'build_app.sh older than the record' in str(refused.value)
        assert gate.LEGACY_ENV not in str(refused.value)


@pytest.mark.parametrize('how', ['flag', 'environment'])
def test_legacy_zip_passes_when_allowed_for_its_version(tmp_path, monkeypatch, capsys, how):
    repo = released(tmp_path, LEGACY)
    archive = build_zip(repo, LEGACY, record=False)
    monkeypatch.setattr(gate, 'ROOT', repo)
    # The signature half needs a notarised app; tested by a real release.
    monkeypatch.setattr(gate, '_check_signature', lambda archive, say: None)
    argv = ['--artifact', str(archive)]
    if how == 'flag':
        monkeypatch.delenv(gate.LEGACY_ENV, raising=False)
        argv += ['--allow-legacy-artifact', LEGACY]
    else:
        monkeypatch.setenv(gate.LEGACY_ENV, LEGACY)
    assert gate.main(argv) == 0
    out = capsys.readouterr().out
    assert f'  - no source record: {LEGACY} predates them and was allowed on request' in out
    assert f'exactly the 8 files committed at v{LEGACY}' in out


def test_legacy_allowance_still_checks_the_payload(tmp_path):
    repo = released(tmp_path, LEGACY)
    archive = build_zip(repo, LEGACY, record=False, add={'Resources/manifests/personal.json': '{}'})
    with pytest.raises(gate.Refused, match=f'does not match v{LEGACY}'):
        check(archive, repo, allow_legacy=LEGACY)


def test_legacy_allowance_is_for_one_version_only(tmp_path):
    repo = released(tmp_path, LEGACY)
    with pytest.raises(gate.Refused, match='no source record'):
        check(build_zip(repo, LEGACY, record=False), repo, allow_legacy='1.5.8')


def test_disallowed_manifest_op_fails_the_source_gate(tmp_path):
    """check_manifests_dir returns its failures; the gate used to drop them."""
    folder = tmp_path / 'manifests'
    folder.mkdir()
    (folder / '4.8.117.json').write_text(json.dumps({'app_version': '4.8.117', 'patches': [
        {'path': 'app/bundle.js', 'ops': [{'type': 'replace', 'anchor': 'a', 'replacement': 'b'}]}]}))
    with pytest.raises(gate.Refused, match='patch guard') as refused:
        gate.check_manifests(folder)
    assert any('not on the allowlist' in line for line in refused.value.details)


def test_shipped_manifests_pass_the_source_gate_guard():
    gate.check_manifests(gate.ROOT / 'manifests')
