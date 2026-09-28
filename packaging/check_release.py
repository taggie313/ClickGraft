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
          'packaging/AppIcon.icns': 'Resources/AppIcon.icns',
          'packaging/python-pin.json': 'Resources/python-pin.json'}

# The rest of a signed, stapled ClickGraft.app, as the real 1.5.9 ZIP has it
# (read 22 Sep 2026): the launcher compiled from packaging/*.swift, the plist
# build_app.sh writes, the signature, stapler's ticket, and the record itself.
# Anything else in the app is refused.
PRODUCTS = frozenset({'Info.plist', 'MacOS/ClickGraft', '_CodeSignature/CodeResources',
                      'CodeResources', RECORD})

# Files the app is built FROM rather than copied from: build_app.sh writes the
# Info.plist, and fetch_python.py builds the archive the pin names. The pin
# itself is COPIED -- it ships inside the app, because the app reads it to know
# what to fetch -- and being a recorded source either way is what makes the tag
# fix which interpreter a release will run on.
BUILT_FROM = ('packaging/build_app.sh', 'packaging/fetch_python.py')

# An interpreter inside the app. A release must NOT have one. Bundling
# python.org's framework was tried during 1.8.0's development and abandoned
# before release -- it worked, and took the download from 770 KB to 18 MB -- so
# the app ships the pin and a Mac that needs an interpreter fetches it once. CLICKGRAFT_BUNDLE_PYTHON=1 still builds a bundled copy for an estate
# with no internet; check_python_pin refuses to let one be released, because
# what the site serves has to be the small one. check_payload skips these paths
# so that refusal is one line rather than four thousand.
PYTHON_PREFIX = 'Frameworks/Python.framework/'

# Whether a release needs a pin is NOT decided here by its number. check_artifact
# asks whether the tag committed packaging/python-pin.json, which is the real
# question and gets a 1.7.1 cut from a tree that has one right. A version cutoff
# lived here until that change and then sat unused for a commit, with a test
# pinning it -- dead code a test makes look live. site/deploy/publish_site.py and
# site/deploy/healthcheck.sh still key on the version, because neither has a tag
# to ask; they are floors and say so.


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
    if path in COPIED or path in BUILT_FROM:
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
    found = [name for name in (*COPIED, *BUILT_FROM) if (root / name).is_file()]
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
    found = {name: _sha256(data) for name, data in files.items()
             if name not in PRODUCTS and not name.startswith(PYTHON_PREFIX)}
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
    # The interpreter is not one of those files. The pin that names it is, so
    # the tag fixes which one a Mac will fetch.
    # Whether a pin is REQUIRED is decided by the tag, not by the version number.
    #
    # Keying it on "is this below 1.8.0" meant a release numbered 1.7.1, cut from
    # a tree that has the pin, would ship an app that needs one with nothing
    # checking it -- the single thing that makes the fetch defensible, skipped
    # because of how the release was numbered. The tag either committed
    # packaging/python-pin.json or it did not, and that is exactly the question.
    # The "no interpreter" half runs either way.
    pinned = check_python_pin(files, root,
                              require_pin='packaging/python-pin.json' in expected)
    if pinned is None:
        say(f'  - v{version} committed no Python pin, so none is required. Carries '
            f'no interpreter, which is checked for every version')
    else:
        say(f'  ✓ carries the Python {pinned} pin the tag committed, and no interpreter')
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


def check_pngshim(root=ROOT):
    """Rebuild the shipped PNG shim from its C source and compare the code.

    This is the price of shipping a binary. ClickGraft used to compile the shim
    on the user's Mac, and the reason given for not shipping it was exactly
    right: "a 16KB .dylib in git that nobody can diff is worse than four lines
    of C". Shipping it removed clang from what a user needs, so the objection
    has to be answered rather than dropped -- the .c stays the reviewable thing,
    and the release cannot go out unless the .dylib is that .c compiled.

    Compared as code, not as bytes. LC_BUILD_VERSION records the SDK, so the
    same source built against MacOSX15, MacOSX26 and MacOSX27 gives three
    different files -- while __TEXT,__text, the exports, the install name and
    the declared minimum are identical across all three (measured 28 September
    2026). Those are what the copy depends on.
    """
    from clickgraft import macho_read

    shipped = root / 'clickgraft/shims/libclickgraft-pngshim.dylib'
    if not shipped.exists():
        raise Refused(f'The PNG shim is missing: {shipped}')
    with tempfile.TemporaryDirectory(prefix='cg-pngshim-') as folder:
        fresh = Path(folder) / 'rebuilt.dylib'
        run = subprocess.run([str(root / 'packaging/build_pngshim.sh'), str(fresh)],
                             cwd=str(root), capture_output=True, text=True)
        if run.returncode:
            raise Refused('The PNG shim could not be rebuilt from pngshim.c.',
                          (run.stdout + run.stderr).strip().splitlines()[-4:])
        a, b = macho_read.read(str(shipped)), macho_read.read(str(fresh))

        def facts(data):
            return {
                'code': macho_read.section_data(data, 'arm64', '__TEXT', '__text'),
                'exports': sorted(macho_read.defined_global_symbols(data, 'arm64')),
                'install name': macho_read.dylib_paths(data)[:1],
                'architectures': macho_read.archs(data),
                'minimum macOS': sorted(set(macho_read.minimum_versions(data).values())),
            }

        shipped_facts, fresh_facts = facts(a), facts(b)
        differences = [f'{key}: shipped {shipped_facts[key]!r}, rebuilt {fresh_facts[key]!r}'
                       for key in shipped_facts if shipped_facts[key] != fresh_facts[key]]
        if differences:
            raise Refused(
                'The shipped PNG shim is not what pngshim.c compiles to. '
                'Rebuild it with packaging/build_pngshim.sh and commit it.',
                differences)


def check_python_pin(files, root=ROOT, require_pin=True):
    """The app carries the pin, and does not carry an interpreter.

    Both halves matter, and they pull in opposite directions.

    The pin is what makes fetching one defensible: the tag fixes
    packaging/python-pin.json, the pin fixes a sha256, and the app installs
    nothing whose bytes do not match it. check_payload already proves the
    shipped copy is the committed one, byte for byte, because the pin is a
    recorded source -- so what is left here is to read it and refuse a pin that
    could not actually be acted on.

    The absence is the other half. 1.8.0 bundled the framework instead: 50 MB of
    binaries nobody can diff, and 17 MB on every download for a problem only a
    Mac without Apple's Command Line Tools has. CLICKGRAFT_BUNDLE_PYTHON=1 still
    builds that app on purpose, for an estate with no internet, and this is what
    stops one being published by accident in place of the small one.
    """
    # Unconditional, whatever the version says. This half was behind the same
    # version gate as the pin, which made it inert for exactly the releases that
    # could still be numbered below 1.8.0 -- and since check_payload skips
    # PYTHON_PREFIX to keep a refusal one line rather than four thousand, a
    # sub-1.8.0 release could carry anything at all under that prefix and no
    # check would look. No ClickGraft has ever shipped an interpreter, so there
    # is nothing for the escape hatch to protect.
    bundled = [name for name in files if name.startswith(PYTHON_PREFIX)]
    if bundled:
        raise Refused(
            f'The app carries a Python.framework ({len(bundled)} files). A released '
            f'ClickGraft ships the pin and fetches an interpreter only on a Mac that '
            f'needs one.',
            ['built with CLICKGRAFT_BUNDLE_PYTHON=1, which is for deploying to an '
             'estate with no internet, not for the download on the site'])

    if not require_pin:
        return None

    shipped = files.get(COPIED['packaging/python-pin.json'])
    if shipped is None:
        raise Refused(
            'The app ships no python-pin.json, so a Mac with no interpreter has '
            'nothing to fetch and ClickGraft cannot start on it at all.')
    try:
        pin = json.loads(shipped)
    except ValueError:
        raise Refused('The python-pin.json inside the app is not readable JSON.')

    # A pin the app cannot act on is worse than none: it would reach the screen
    # that offers the fetch and fail there, on a Mac that has no other way in.
    missing = [key for key in ('version', 'payload_url', 'payload_zip_sha256')
               if not pin.get(key)]
    if missing:
        raise Refused(
            f"The pin inside the app cannot be acted on: no {', '.join(missing)}.",
            ['rebuild the archive and re-pin with '
             'python3 packaging/fetch_python.py --payload dist/ClickGraft-python.zip'])
    if len(pin['payload_zip_sha256']) != 64:
        raise Refused('The pin\'s payload_zip_sha256 is not a sha256.')
    if not pin['payload_url'].startswith('https://'):
        raise Refused(f"The pin fetches over {pin['payload_url'].split(':')[0]}, not https.")

    # The app fetches payload_url verbatim, but every publisher -- redeploy.sh,
    # publish_site.validate(), healthcheck.sh -- composes the filename from
    # `version` instead. Nothing compared the two, so a re-pin that moved the
    # version and left the old basename in the URL would publish one file and
    # send every app to a different one, with all three gates green.
    expected = f"ClickGraft-python-{pin['version']}.zip"
    if pin['payload_url'].rsplit('/', 1)[-1] != expected:
        raise Refused(
            f"The pin fetches {pin['payload_url'].rsplit('/', 1)[-1]}, but the deploy "
            f"publishes {expected}.",
            ['every app would ask for a file the site does not serve'])
    return pin['version']


def source_gate(root=ROOT, say=print):
    if sys.platform != 'darwin':
        raise Refused('The release gate needs macOS. The portable tests alone cannot approve a release.')
    from tests.test_clickgraft import find_stock_bundle
    from clickgraft.deps import check_clt
    if not check_clt() or not find_stock_bundle('4.8.117'):
        raise Refused('The release gate needs the developer tools and a stock HP Click 4.8.117 in /Applications.')
    check_manifests(root / 'manifests')
    say('  ✓ every manifest passes the patch guard')
    check_pngshim(root)
    say('  ✓ the shipped PNG shim is what pngshim.c compiles to')
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
        pinned = check_python_pin(files, root)
    say(f'  ✓ carries the Python {pinned} pin, and no interpreter of its own')
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
