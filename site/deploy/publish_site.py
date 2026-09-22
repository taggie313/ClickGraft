#!/usr/bin/env python3
"""Put a staged ClickGraft site live, or put a kept one back.

  python3 publish_site.py SITE/.incoming-ID SITE
      What redeploy.sh runs inside CT 136, with SITE /opt/edge/sites/clickgraft.
      Checks the staged download against its appcast, switches html/, then
      installs collector.py, summary.sh, visitor-classify.awk and this script.
      The site it replaced is kept as SITE/.previous-<UTC time>-ID.

  python3 publish_site.py --restore SITE/.previous-<UTC time>-ID SITE
      Puts a kept site back the same way, and keeps the one it replaces too.

  python3 publish_site.py --check BUILD/html
      What redeploy.sh runs on this Mac before it uploads anything, so a tree
      this script would refuse inside the container is refused here instead,
      in the same one-line form. Reads only; changes nothing.

Writes only under SITE. The collector directory is mounted into its own
container, so its file is replaced but the directory itself never is. nginx,
the tunnel and the other projects' sites are never touched.
"""
import argparse
import contextlib
import ctypes
import datetime
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import uuid

# Installed beside html/ on every publication. This script is one of them so
# the README's --restore command can run from the site directory.
FILES = ('collector/collector.py', 'summary.sh', 'visitor-classify.awk', 'publish_site.py')

# How many .previous-* copies to keep. Ten, because 8 to 10 Sep 2026 had five
# releases (1.5.0 to 1.5.4) and nine commits to site/ in three days: a bad
# release noticed after the page fixes that follow it has to reach back past
# several deploys. Each copy is under 1 MB (0.8 MB, 22 Sep 2026), so the limit
# keeps the list to choose from short; disk was never the reason.
KEEP = 10
KEPT = re.compile(r'\.previous-\d{8}T\d{6}\.\d{6}Z-')

# What renameat2 and renamex_np answer when the filesystem or kernel cannot
# exchange at all, as opposed to a real failure: EINVAL for a flag the
# filesystem does not support, ENOSYS without the call, ENOTSUP on macOS.
UNSUPPORTED = {errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP}


class Busy(RuntimeError):
    """Another publication holds the site lock."""


class NeedsOperator(RuntimeError):
    """Something failed and could not be undone. Nothing has been deleted, and
    the detail lines say where everything is and what to run."""

    def __init__(self, reason, details=()):
        super().__init__(reason)
        self.details = list(details)


def validate(html):
    """Refuse a tree that would serve a broken page or a download that does not
    match its appcast. Runs before anything served changes."""
    html = Path(html)
    appcast = html / 'appcast.json'
    try:
        info = json.loads(appcast.read_text())
        version, sha256 = info['version'], info['sha256']
    except (ValueError, KeyError, TypeError):
        raise ValueError(f'{appcast} is not an appcast with a version and a sha256') from None
    name = f'ClickGraft-{version}.zip'
    digest = hashlib.sha256()
    with (html / name).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    if digest.hexdigest() != sha256:
        raise ValueError('Appcast checksum does not match the staged download')
    if not (html / 'ClickGraft.zip').is_symlink() or os.readlink(html / 'ClickGraft.zip') != name:
        raise ValueError('Download alias must be a relative symlink to the versioned ZIP')
    for page in ('index.html', 'es/index.html', 'sitemap.xml'):
        if '{{' in (html / page).read_text():
            raise ValueError(f'Unexpanded placeholder in {page}')
    if 'ClickGraft' not in (html / 'index.html').read_text():
        raise ValueError('Missing site page')


# Why html/ is exchanged rather than emptied and refilled. Measured 22 Sep 2026
# with a reader looping over index.html, ClickGraft.zip, appcast.json and
# es/index.html: the old deploy's rm -rf html/* then tar -x missed 11,654 of
# 17,392 reads, every file "not found" while it ran, and 150 exchanges under
# the same reader missed 0 of 222,448. Someone loading the page or checking for
# an update in that window got a 404.
#
# nginx only sees the exchange because edge mounts the PARENT directory,
# ./sites -> /srv/sites (docker inspect of edge-nginx-1, 22 Sep 2026), and looks
# html/ up by name on every request. Were html/ itself mounted, as it was on
# CT 117, nginx would stay on the old directory. redeploy.sh checks what edge
# actually serves afterwards for that reason.
def exchange(left, right):
    """Swap two directory entries in one step, so neither name is ever missing."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        fn = libc.renamex_np
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = fn(os.fsencode(left), os.fsencode(right), 2)  # RENAME_SWAP
    elif sys.platform.startswith('linux'):
        try:
            fn = libc.renameat2
        except AttributeError:  # glibc before 2.28 has no wrapper for it
            raise OSError(errno.ENOSYS, 'renameat2 is not in this C library', os.fspath(right)) from None
        fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = fn(-100, os.fsencode(left), -100, os.fsencode(right), 2)  # AT_FDCWD, RENAME_EXCHANGE
    else:
        raise OSError(errno.ENOSYS, 'No atomic directory exchange on this platform', os.fspath(right))
    if result:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), os.fspath(right))


def swap_entries(left, right):
    """What exchange() does, one entry at a time, for a filesystem that will not.

    Each name under `right` is missing only between two renames, where the old
    deploy had every file missing for the whole of an rm and a tar. Both
    directories keep their inodes. A name only in the old tree leaves with it,
    not copied over: the old deploy's tar -x merged, and kept serving two
    replaced icons for a day. A failure part way reverses the renames already
    made. Calling it again with the same arguments undoes it, as with
    exchange().
    """
    hold = left.parent / (left.name + '.swapping')
    moves = []

    def move(source, target):
        os.rename(source, target)
        moves.append((source, target))

    hold.mkdir()
    try:
        old, new = set(os.listdir(right)), set(os.listdir(left))
        for name in sorted(old | new):
            if name in old:
                move(right / name, hold / name)
            if name in new:
                move(left / name, right / name)
        for name in sorted(old):
            move(hold / name, left / name)
    except BaseException as failure:
        try:
            for source, target in reversed(moves):
                os.rename(target, source)
        except OSError as error:
            raise NeedsOperator(
                f'html/ was being switched file by file, failed ({_plain(failure)}), '
                f'and could not be switched back ({_plain(error)}).',
                [f'The site is split between {right}, {hold} and {left}.',
                 'Nothing was deleted. Put the files back by hand, then run the full healthcheck.']) from failure
        raise
    finally:
        with contextlib.suppress(OSError):
            hold.rmdir()


# renameat2(RENAME_EXCHANGE) needs the filesystem to support it. CT 136's root
# is ZFS (OpenZFS 2.4.4, read 22 Sep 2026), which added it in 2.2.0, but it has
# only been run on ext4 and tmpfs. A refusal says this filesystem cannot
# exchange, so the fallback runs rather than failing a deploy that has already
# passed every check. Any other error is a real failure and is raised.
#
# What the fallback costs, measured 22 Sep 2026 on ext4 with renameat2 refused
# by an LD_PRELOAD shim, over 50 publications of the real 1.5.9 site: the same
# reader as above missed 52 of 72,380 reads, about one per publication, against
# none for the exchange and two in three for the old rm and tar.
def _switch(new, live, say):
    """Exchange two html trees, or swap their entries if the filesystem refuses."""
    try:
        exchange(new, live)
        return
    except OSError as error:
        if error.errno not in UNSUPPORTED:
            raise
        say(f'⚠ The filesystem would not exchange html/ in one step ({error.strerror}), so its '
            'files are being moved one at a time instead. Each is missing for a moment.')
    swap_entries(new, live)


def _plain(error):
    """One plain line for an error, without Python's [Errno n] prefix."""
    if isinstance(error, OSError) and error.strerror:
        return f'{error.strerror}: {error.filename}' if error.filename else error.strerror
    return str(error) or type(error).__name__


def _discard(folder):
    shutil.rmtree(folder, ignore_errors=True)


def _pending(target, incoming):
    return target.with_name(target.name + '.pending-' + incoming.name)


@contextlib.contextmanager
def _locked(root):
    with (root / '.deploy.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy('Another site publication is in progress; retry when it has finished') from None
        yield


def _publish(incoming, root, say, spare=(), done='published'):
    """The publication itself, with the lock held.

    Removes `incoming` when it refuses, and when it fails and everything is put
    back: what is left then is only an upload, and the next deploy stages its
    own. Keeps it whenever it may hold the only copy of the previous site."""
    current = root / 'html'
    try:
        validate(incoming / 'html')
        previous = {}
        for name in FILES:
            if not (incoming / name).is_file():
                raise ValueError(f'The staged release has no {name}')
            target = root / name
            previous[name] = target.read_bytes() if target.exists() else None
    except BaseException:
        _discard(incoming)
        raise

    first = not current.exists()
    switched = False
    try:
        if first:
            os.rename(incoming / 'html', current)
        else:
            _switch(incoming / 'html', current, say)
        switched = True
        for name in FILES:
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            pending = _pending(target, incoming)
            shutil.copy2(incoming / name, pending)
            os.replace(pending, target)
        (root / 'summary.sh').chmod(0o755)
    except NeedsOperator:
        raise
    except BaseException as failure:
        if not switched:
            _discard(incoming)
            raise
        trouble = _put_back(incoming, root, first, previous, failure, say)
        if trouble is None:
            _discard(incoming)
            raise
        raise trouble from failure

    if first and all(content is None for content in previous.values()):
        _discard(incoming)
        say(f'✓ {done}. There was no site here before it.')
        return None
    return _keep(incoming, root, previous, say, spare, done)


def _put_back(incoming, root, first, previous, failure, say):
    """After a failure past the switch, put the previous site back.

    Returns None when everything is back. Otherwise a NeedsOperator naming where
    the previous site is and the exact command that restores it."""
    current = root / 'html'
    for name in FILES:
        with contextlib.suppress(OSError):
            _pending(root / name, incoming).unlink()
    try:
        if first:
            os.rename(current, incoming / 'html')
        else:
            _switch(incoming / 'html', current, say)
        html_error = None
    except BaseException as error:
        html_error = error
    unrestored = {}
    for name, content in previous.items():
        try:
            if content is None:
                (root / name).unlink(missing_ok=True)
            else:
                (root / name).write_bytes(content)
        except BaseException as error:
            unrestored[name] = error
    if html_error is None and not unrestored:
        return None
    if isinstance(html_error, NeedsOperator):
        # Split part way through a file-by-file swap: no one directory holds
        # the previous site, so say where the pieces are and move nothing.
        return NeedsOperator(f'Publishing failed ({_plain(failure)}). {html_error}', html_error.details)
    if html_error is not None and first:
        return NeedsOperator(f'Publishing failed ({_plain(failure)}), and the new site could not be taken '
                             f'down again ({_plain(html_error)}).',
                             [f'There was no site here before it. Remove {current} to take it down.'])

    if html_error is not None:
        # incoming/html still holds the previous site. Complete it as a kept
        # copy, so the ordinary --restore puts it back. Its publish_site.py
        # stays the staged one, the code that wrote these lines: the site may
        # have had none before this deploy.
        for name, content in previous.items():
            if content is not None and name != 'publish_site.py':
                with contextlib.suppress(OSError):
                    (incoming / name).write_bytes(content)
        backup = root / _kept_name(incoming)
        restore = f'python3 {backup}/publish_site.py --restore {backup} {root}'
        try:
            os.rename(incoming, backup)
            where = [f'The new site is live. The previous one is intact at {backup}', 'Put it back with:',
                     f'  {restore}']
        except OSError:
            where = [f'The new site is live. The previous one is intact at {incoming}/html', 'Put it back with:',
                     f'  mv {incoming} {backup} && {restore}']
        return NeedsOperator(f'Publishing failed ({_plain(failure)}), and the previous site could not be '
                             f'put back ({_plain(html_error)}).', where)

    details = []
    for name, error in unrestored.items():
        if previous[name] is None:
            details.append(f'{root / name} could not be removed ({_plain(error)}). '
                           'This publication added it; remove it.')
            continue
        try:
            (incoming / name).write_bytes(previous[name])
            details.append(f'{root / name} could not be put back ({_plain(error)}). Its previous contents '
                           f'are in {incoming / name}: cp {incoming / name} {root / name}')
        except OSError as saving:
            details.append(f'{root / name} could not be put back ({_plain(error)}), and its previous '
                           f'contents could not be saved either ({_plain(saving)}).')
    details.append(f'Then remove {incoming}, and run the full healthcheck.')
    return NeedsOperator(f'Publishing failed ({_plain(failure)}). The previous page is back, but not every file.',
                         details)


def _kept_name(incoming):
    # The time first, so a listing sorts oldest to newest and the choice of what
    # to restore reads in order. Microseconds, so two deploys cannot share it.
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    return f".previous-{stamp}-{incoming.name[len('.incoming-'):]}"


def _keep(incoming, root, previous, say, spare, done):
    """Keep what was replaced as .previous-<time>-ID, and prune the oldest."""
    backup = root / _kept_name(incoming)
    try:
        for name, content in previous.items():
            if content is not None:
                (incoming / name).write_bytes(content)
        os.rename(incoming, backup)
    except OSError as error:
        raise NeedsOperator(f'The new site is live, but the one it replaced could not be kept ({_plain(error)}).',
                            [f'It is intact at {incoming}/html. Keep it with: mv {incoming} {backup}']) from error
    say(f'✓ {done}. The site it replaced is kept at {backup}')
    try:
        kept = sorted((path for path in root.iterdir() if KEPT.match(path.name) and path.is_dir()
                       and not path.is_symlink()), key=lambda path: path.name, reverse=True)
        for old in kept[KEEP:]:
            if old not in spare:
                shutil.rmtree(old)
                say(f'- removed {old.name}: only the newest {KEEP} are kept')
    except OSError as error:
        say(f'⚠ Old .previous-* copies could not be removed ({_plain(error)}).')
    return backup


def publish(incoming, root, say=print):
    incoming, root = Path(incoming).resolve(), Path(root).resolve()
    if incoming.parent != root or not incoming.name.startswith('.incoming-'):
        raise ValueError('The release to publish must be a .incoming-* directory inside the site directory')
    try:
        with _locked(root):
            return _publish(incoming, root, say)
    except Busy:
        # Its name is unique to this deploy, and a retry stages its own.
        _discard(incoming)
        raise


def restore(backup, root, say=print):
    backup, root = Path(backup).resolve(), Path(root).resolve()
    if backup.parent != root or not backup.name.startswith('.previous-'):
        raise ValueError('--restore takes a .previous-* copy kept in the site directory')
    validate(backup / 'html')
    incoming = root / ('.incoming-restore-' + uuid.uuid4().hex)
    # The lock before the copy, so a restore refused because a deploy is running
    # leaves nothing behind.
    with _locked(root):
        try:
            shutil.copytree(backup, incoming, symlinks=True)
        except BaseException:
            _discard(incoming)
            raise
        # The same path as a deploy: validated again, switched, put back on
        # failure. The requested copy stays, and the site it displaces is kept.
        return _publish(incoming, root, say, spare=(backup,), done=f'put back {backup.name}')


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Put a staged ClickGraft site live, with --restore put a kept one back, or with --check only check a staged tree.')
    parser.add_argument('--restore', action='store_true', help='RELEASE is a kept .previous-* copy to put back')
    parser.add_argument('--check', action='store_true',
                        help='RELEASE is a staged html/ tree: check it, change nothing, take no SITE')
    parser.add_argument('release', metavar='RELEASE',
                        help='SITE/.incoming-ID to publish, SITE/.previous-* with --restore, '
                             'or a staged html/ tree with --check')
    parser.add_argument('site', metavar='SITE', nargs='?',
                        help='the site directory: /opt/edge/sites/clickgraft on edge')
    args = parser.parse_args(argv)
    if args.check and args.restore:
        parser.error('--check and --restore do different jobs; use one')
    if not args.check and args.site is None:
        parser.error('SITE is required')
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(line_buffering=True)

    def say(line):
        print(line, file=sys.stderr if line.startswith(('✗', '⚠')) else sys.stdout)

    try:
        if args.check:
            # Through main(), not a `python3 -c` calling validate(): redeploy.sh
            # runs this mid-deploy, and a refusal there has to read like every
            # other line it prints. Until this existed, an appcast that did not
            # match its ZIP ended the deploy with a Python traceback.
            validate(args.release)
            say('✓ the staged site is complete, and its download matches its appcast')
        else:
            (restore if args.restore else publish)(args.release, args.site, say)
    except NeedsOperator as trouble:
        print(f'✗ {trouble}', file=sys.stderr)
        for line in trouble.details:
            print(f'  {line}', file=sys.stderr)
        return 1
    except (ValueError, RuntimeError, OSError) as error:
        print(f'✗ {_plain(error)}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
