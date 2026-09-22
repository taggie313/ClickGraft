#!/usr/bin/env python3
"""ClickGraft's release gates. Neither one signs, tags, uploads or deploys.

  python3 packaging/check_release.py
      Before signing. Needs the developer tools and a stock HP Click 4.8.117
      in /Applications. Runs the patch guard and every test, then builds a
      universal ClickGraft.app in a temporary folder, checks that it ships
      exactly the files its source record lists, and throws it away.

  python3 packaging/check_release.py --artifact dist/ClickGraft.zip
      Before distributing, and on every deploy: site/deploy/redeploy.sh runs
      it. Ties the ZIP to the tag its own version names, v<version>. That
      tag's build_app.sh must build that version, the ZIP's source record must
      equal the sources committed at the tag, and every file in the app must be
      one of those sources, byte for byte, or a named build product. Then
      codesign, the stapled ticket and Gatekeeper, on the app extracted from
      the ZIP. HEAD and the working tree play no part, so a site-only commit
      after the tag still deploys.

      --allow-legacy-artifact VERSION, or CLICKGRAFT_ALLOW_LEGACY_ARTIFACT=VERSION
      in the environment (which is how it reaches redeploy.sh), accepts a ZIP
      of that one version without a source record. Only up to 1.5.9: those
      releases were built before the record existed. The payload of such a ZIP
      is still checked against its tag; only its launcher cannot be tied to the
      Swift it was compiled from. A later version without a record was built by
      a build_app.sh older than the record, which is the stale binary this gate
      is for, and there is nothing to allow: build it again.

  python3 packaging/check_release.py --record PATH
      Run by build_app.sh. Writes the source record into the app.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LEGACY_ENV = 'CLICKGRAFT_ALLOW_LEGACY_ARTIFACT'
# The last release built before build_app.sh wrote a source record. The
# allowance is for those and nothing else. Offering it for any version would
# hand the operator a way past the one check that catches what CLAUDE.md's
# release order warns about: skipping the rebuild "once notarised a stale
# binary that no longer matched the source".
LEGACY_UNTIL = (1, 5, 9)
APP = 'ClickGraft.app/Contents/'
RECORD = 'Resources/build-source.json'

# Copied into the app by name, as build_app.sh does it: repo path -> path under
# Contents/. clickgraft/ and manifests/ are copied whole, into Resources/.
COPIED = {'LICENSE': 'Resources/LICENSE', 'NOTICE': 'Resources/NOTICE',
          'packaging/AppIcon.icns': 'Resources/AppIcon.icns'}

# The rest of a signed, stapled ClickGraft.app, as the real 1.5.9 ZIP has it
# (read 22 Sep 2026): the launcher compiled from packaging/*.swift, the plist
# build_app.sh writes, the signature, stapler's ticket, and the record itself.
# Anything else in the app is refused.
PRODUCTS = frozenset({'Info.plist', 'MacOS/ClickGraft', '_CodeSignature/CodeResources',
                      'CodeResources', RECORD})


class Refused(Exception):
    """Why the gate says no, in plain words, with any detail lines."""

    def __init__(self, reason, details=()):
        super().__init__(reason)
        self.details = list(details)


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def is_source(path):
    """Whether a repo path belongs in the source record.

    The record is everything the app is made from: each file build_app.sh
    copies into it, and the files it is compiled or written from, which are
    the Swift and build_app.sh itself (it writes Info.plist). release.json is
    left out: it never enters the app, and the appcast reads it from the tag.

    An earlier draft recorded only clickgraft/*.py. In a review on 22 Sep 2026
    a stale clickgraft/data/printers-4.8.117.json and clickgraft/shims/pngshim.c
    both passed it, although the rsync ships both.

    Every deploy checks the live release's ZIP against this rule, so a change
    here must still pass that ZIP.
    """
    parts = path.split('/')
    if parts[0] in ('clickgraft', 'manifests') and len(parts) > 1:
        # build_app.sh deletes .DS_Store from the whole app, and its rsync of
        # clickgraft/ leaves out __pycache__ and *.pyc.
        if parts[-1] == '.DS_Store':
            return False
        return parts[0] == 'manifests' or ('__pycache__' not in parts and not path.endswith('.pyc'))
    if path in COPIED or path == 'packaging/build_app.sh':
        return True
    return len(parts) == 2 and parts[0] == 'packaging' and path.endswith('.swift')


def app_path(source):
    """Where build_app.sh puts a recorded source, under Contents/. None for
    the ones the app is built from rather than copied from."""
    if source.startswith(('clickgraft/', 'manifests/')):
        return 'Resources/' + source
    return COPIED.get(source)


def source_record(root=ROOT):
    """{repo path: sha256} of every source in the working tree, as build_app.sh
    is about to ship it."""
    root = Path(root)
    found = [name for name in (*COPIED, 'packaging/build_app.sh') if (root / name).is_file()]
    found += [f'packaging/{path.name}' for path in (root / 'packaging').glob('*.swift')]
    for top in ('clickgraft', 'manifests'):
        for folder, dirs, files in os.walk(root / top):
            dirs[:] = [d for d in dirs if d != '__pycache__']
            found += [Path(folder, name).relative_to(root).as_posix() for name in files]
    return {name: _sha256((root / name).read_bytes()) for name in sorted(found) if is_source(name)}


def _git(root, *args, why=None):
    """git's output, or None if it fails. A list passed as `why` gets git's
    own last words, which name the real problem when it is not the obvious
    one: /usr/bin/git is the same Xcode stub as /usr/bin/python3, and refuses
    to run while an updated Xcode waits for its licence (see build_app.sh)."""
    try:
        run = subprocess.run(['git', *args], cwd=str(root), capture_output=True)
    except FileNotFoundError:
        raise Refused('git is not installed, and the gate reads the release from its tag.') from None
    if run.returncode and why is not None:
        why.extend(run.stderr.decode(errors='replace').strip().splitlines()[-2:])
    return None if run.returncode else run.stdout


def tag_sources(root, version):
    """The source record of the tree committed at v<version>, once that tag's
    build_app.sh is known to build <version>.

    The tag, not HEAD, is the release's identity. Site, summary, watcher and
    collector changes are committed and deployed between releases as a matter
    of course (6b6ec45, site copy; f1030cc, summary and watcher; among others),
    and each deploy ships the ZIP that is already live. Requiring HEAD to be
    the tag refused every one of those deploys.

    The tag still has to be the source of the build: v1.5.8 sits two commits
    after "ClickGraft 1.5.8", and 77f6cfb between them changed ClickGraft.swift.
    """
    tag = f'v{version}'
    why = []
    if _git(root, 'rev-parse', '--git-dir', why=why) is None:
        raise Refused(f'git cannot read {root} as a checkout, and the gate reads the release from its tag.', why)
    commit = _git(root, 'rev-parse', '--verify', '--quiet', f'refs/tags/{tag}^{{commit}}')
    if commit is None:
        raise Refused(f'There is no tag {tag} in this checkout. Tag the release commit '
                      '(CLAUDE.md, "Shipping a release") or fetch the tags, then run this again.')
    commit = commit.decode().strip()
    script = _git(root, 'cat-file', 'blob', f'{commit}:packaging/build_app.sh') or b''
    built = re.search(rb'CLICKGRAFT_VERSION:-([^}]+)', script)
    built = built.group(1).decode() if built else 'no version'
    if built != version:
        raise Refused(f"{tag}'s build_app.sh builds {built}, but the ZIP is {version}.")
    listing = _git(root, 'ls-tree', '-r', '-z', '--full-tree', commit)
    if listing is None:
        raise Refused(f'git could not list the files at {tag}.')
    record = {}
    for entry in listing.split(b'\0'):
        meta, _tab, name = entry.partition(b'\t')
        if not name:
            continue
        _mode, kind, blob = meta.split()
        name = name.decode('utf-8', 'surrogateescape')
        if kind == b'blob' and is_source(name):
            record[name] = _sha256(_git(root, 'cat-file', 'blob', blob.decode()) or b'')
    return record


def _differences(found, expected, found_in, expected_in):
    """One line per path that is missing, extra or different. Names only: a
    whole diff buries the one line that says what to do."""
    lines = []
    for name in sorted(set(found) | set(expected)):
        if name not in expected:
            lines.append(f'{name}: in {found_in}, not in {expected_in}')
        elif name not in found:
            lines.append(f'{name}: in {expected_in}, not in {found_in}')
        elif found[name] != expected[name]:
            lines.append(f'{name}: differs from {expected_in}')
    return lines


def check_payload(files, record, against):
    """Every file in the app ({path under Contents/: bytes}) is a recorded
    source, byte for byte, or a named build product, and every recorded
    source that ships is there. Returns how many sources it ships.

    Both directions. On 22 Sep 2026 a review added Resources/manifests/
    personal.json to a built app, re-signed and zipped it, and a check that
    only looked up the recorded names let it through. A manifest dropped into
    dist/ after the build is exactly what CLAUDE.md's "Never distribute" is
    about.
    """
    shipped = {app_path(name): digest for name, digest in record.items() if app_path(name)}
    found = {name: _sha256(data) for name, data in files.items() if name not in PRODUCTS}
    problems = _differences(found, shipped, 'the app', against)
    if problems:
        raise Refused(f'The app does not match {against}.', problems)
    return len(shipped)


def _zip_contents(archive):
    try:
        with zipfile.ZipFile(archive) as bundle:
            files, stray = {}, []
            for info in bundle.infolist():
                if info.is_dir():
                    continue
                if info.filename.startswith(APP):
                    files[info.filename[len(APP):]] = bundle.read(info)
                else:
                    stray.append(info.filename)
    except zipfile.BadZipFile:
        raise Refused(f'{archive} is not a ZIP.') from None
    except OSError as error:
        raise Refused(f'{archive} cannot be read: {error.strerror or error}.') from None
    if stray:
        raise Refused(f'{archive} holds files outside {APP}.', stray)
    return files


def _version(files):
    try:
        info = plistlib.loads(files['Info.plist'])
    except Exception:  # missing, or not a plist: plistlib raises several kinds
        raise Refused(f'The ZIP has no readable {APP}Info.plist.') from None
    short, full = info.get('CFBundleShortVersionString'), info.get('CFBundleVersion')
    # The version names a git tag, so it must look like one and nothing else.
    if short != full or not isinstance(short, str) or not re.fullmatch(r'[0-9]+(\.[0-9]+)*', short):
        raise Refused(f"The ZIP's Info.plist gives no single version: "
                      f'CFBundleShortVersionString {short!r}, CFBundleVersion {full!r}.')
    return short


def _predates_records(version):
    """Whether a ZIP of this version can legitimately have no source record."""
    return tuple(int(part) for part in version.split('.')) <= LEGACY_UNTIL


def check_artifact(archive, root=ROOT, check_platform=True, allow_legacy=None, say=lambda line: None):
    """Refused unless the ZIP is the app built from its own release tag,
    signed, stapled and accepted by Gatekeeper."""
    files = _zip_contents(archive)
    version = _version(files)
    tag = f'v{version}'
    expected = tag_sources(root, version)
    say(f'  ✓ ClickGraft {version}, and {tag} builds {version}')
    if RECORD in files:
        try:
            recorded = json.loads(files[RECORD])
        except ValueError:
            recorded = None
        if not isinstance(recorded, dict):
            raise Refused(f"The ZIP's source record ({RECORD}) cannot be read.")
        differences = _differences(recorded, expected, 'the record', tag)
        if differences:
            raise Refused(f'The ZIP was built from sources that differ from {tag}. '
                          'Build it again from the tag.', differences)
        say(f'  ✓ built from {tag}: all {len(expected)} sources match')
    elif not _predates_records(version):
        raise Refused(f'This {version} ZIP has no source record, so nothing ties it to {tag}. '
                      f'Every release after 1.5.9 has one, so this ZIP was built by a '
                      f'build_app.sh older than the record. Build it again from {tag}.')
    elif allow_legacy == version:
        say(f'  - no source record: {version} predates them and was allowed on request, '
            f'so its launcher is not tied to {tag}')
    else:
        raise Refused(f'This {version} ZIP has no source record (no release up to 1.5.9 has one), '
                      f'so nothing ties it to {tag}. To deploy it anyway, run with {LEGACY_ENV}={version}.')
    count = check_payload(files, expected, tag)
    say(f'  ✓ the app ships exactly the {count} files committed at {tag}, and nothing else')
    if check_platform:
        _check_signature(archive, say)


def _check_signature(archive, say):
    if sys.platform != 'darwin':
        raise Refused('The signature, ticket and Gatekeeper checks need macOS.')
    # The app INSIDE the ZIP, not dist/ClickGraft.app: the ZIP is what people
    # download. Full paths, because on 14 Sep 2026 a restricted PATH left out
    # /usr/sbin and a bare spctl was "command not found" (sign_and_notarize.sh).
    with tempfile.TemporaryDirectory(prefix='cg-release-artifact-') as folder:
        steps = [
            (['/usr/bin/ditto', '-x', '-k', str(Path(archive).resolve()), folder],
             None, 'ditto could not extract the ZIP.'),
            (['/usr/bin/codesign', '--verify', '--deep', '--strict', f'{folder}/ClickGraft.app'],
             'signature verifies', 'codesign does not accept the signature.'),
            (['/usr/bin/xcrun', 'stapler', 'validate', f'{folder}/ClickGraft.app'],
             'notarisation ticket is stapled',
             'The app carries no valid notarisation ticket. Ship the ZIP that '
             'sign_and_notarize.sh makes after stapling.'),
            (['/usr/sbin/spctl', '--assess', '--type', 'execute', '--verbose', f'{folder}/ClickGraft.app'],
             'Gatekeeper accepts it', 'Gatekeeper would not open the app. Do not distribute it.'),
        ]
        for command, passed, failed in steps:
            run = subprocess.run(command, capture_output=True, text=True)
            if run.returncode:
                raise Refused(failed, (run.stdout + run.stderr).strip().splitlines()[-4:])
            if passed:
                say(f'  ✓ {passed}')


def check_manifests(folder):
    """The patch guard over a manifests folder. check_manifests_dir returns its
    failures rather than raising, and until 22 Sep 2026 this gate called it and
    dropped the list, so a manifest with a disallowed op passed here."""
    from clickgraft.manifest_guard import check_manifests_dir
    failures = check_manifests_dir(str(folder))
    if failures:
        raise Refused('A manifest fails the patch guard. See CLAUDE.md, "Never distribute".',
                      [line for failure in failures for line in failure.splitlines()])


def source_gate(root=ROOT, say=print):
    if sys.platform != 'darwin':
        raise Refused('The release gate needs macOS. The portable tests alone cannot approve a release.')
    from tests.test_clickgraft import find_stock_bundle
    from clickgraft.deps import check_clt
    if not check_clt() or not find_stock_bundle('4.8.117'):
        raise Refused('The release gate needs the developer tools and a stock HP Click 4.8.117 in /Applications.')
    check_manifests(root / 'manifests')
    say('  ✓ every manifest passes the patch guard')
    if subprocess.run([sys.executable, '-m', 'pytest', '-q', '-rs', 'tests'], cwd=str(root)).returncode:
        raise Refused('The tests failed. Their output is above.')
    say('  ✓ the tests passed. Read every skipped case listed above')
    with tempfile.TemporaryDirectory(prefix='cg-release-build-') as folder:
        # Through its own #!/bin/bash, as a release runs it. A bash 5 earlier
        # in PATH compares mtimes to the sub-second, and on a fresh clone
        # (22 Sep 2026) that made build_app.sh regenerate the icon.
        if subprocess.run([str(root / 'packaging/build_app.sh'), folder], cwd=str(root)).returncode:
            raise Refused('build_app.sh failed. Its output is above.')
        contents = Path(folder) / 'ClickGraft.app/Contents'
        files = {path.relative_to(contents).as_posix(): path.read_bytes()
                 for path in contents.rglob('*') if path.is_file()}
        if RECORD not in files:
            raise Refused(f'build_app.sh wrote no source record ({RECORD}).')
        recorded = json.loads(files[RECORD])
        differences = _differences(recorded, source_record(root), 'the record', 'the working tree')
        if differences:
            raise Refused('The source record in the app does not match the working tree.', differences)
        count = check_payload(files, recorded, 'its source record')
    say(f'  ✓ universal app built, shipping exactly its {count} recorded files. It was a '
        'throwaway: sign a fresh build_app.sh output, not this one')


def _report(refused):
    print(f'  ✗ {refused}', file=sys.stderr)
    for line in refused.details[:12]:
        print(f'      {line}', file=sys.stderr)
    if len(refused.details) > 12:
        print(f'      and {len(refused.details) - 12} more', file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--artifact', metavar='ZIP',
                        help='check a signed, stapled ZIP against its release tag')
    parser.add_argument('--allow-legacy-artifact', metavar='VERSION', default=os.environ.get(LEGACY_ENV) or None,
                        help=f'accept a ZIP of this version without a source record (also ${LEGACY_ENV})')
    parser.add_argument('--record', metavar='PATH', help='write the source record (build_app.sh does this)')
    args = parser.parse_args(argv)
    # Line by line. Piped (22 Sep 2026), stdout was held back and the ✗ on
    # stderr printed above the ✓ lines that led to it.
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(line_buffering=True)
    try:
        if args.record:
            Path(args.record).write_text(json.dumps(source_record(ROOT), indent=2, sort_keys=True) + '\n')
        elif args.artifact:
            print(f'==> checking {args.artifact} against its release tag')
            check_artifact(args.artifact, ROOT, allow_legacy=args.allow_legacy_artifact, say=print)
            print('    ready to distribute')
        else:
            print('==> release gate: patch guard, tests and a throwaway build')
            source_gate(ROOT)
            print('    next: build fresh into dist/, then sign_and_notarize.sh')
    except Refused as refused:
        _report(refused)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
