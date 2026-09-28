"""The runtime archives the site must keep serving, and where they live.

Each released ClickGraft embeds an immutable URL and SHA-256. Publication
exchanges the whole of `html/`, so before 1.8.2 shipping a new payload took the
old one off the site -- an app that had not yet fetched an interpreter got a
404, or, for a same-version re-sign, bytes whose hash it refuses. The old files
survived in `.previous-*` backups, which is not where anybody's app looks.

So the archives live OUTSIDE the exchanged tree, in a private store keyed by
digest, and every published tree gets a verified copy:

    SITE/runtime-store/<sha256>.zip     immutable, never pruned by a deploy
    SITE/runtime-inventory.json         durable, additive, under the deploy lock
    SITE/html/<registered path>         a verified copy, replaced each exchange
    SITE/html/runtime-inventory.json    published, so anyone can check retention

Regular copies, not hard links: a link between served content and the durable
store would let an accidental in-place write alter both, and the store is the
thing that must not be alterable. 17 MB per artifact is a price worth paying
for that.

Nothing here decides which interpreter an app runs. The pin embedded in the app
is authoritative; this only decides what the site keeps available.
"""

import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import tempfile

SCHEMA = 1
STORE_DIR = 'runtime-store'
INVENTORY = 'runtime-inventory.json'

# A registered path is a bare filename. Anything with a separator, a scheme, a
# query or a traversal segment is rejected rather than normalised: this string
# becomes a served URL and a filesystem path, and "clean it up and carry on" is
# how one of those two ends up somewhere unintended.
_NAME = re.compile(r'^ClickGraft-python-[0-9A-Za-z.+-]+\.zip$')
_SHA256 = re.compile(r'^[0-9a-f]{64}$')


class InventoryError(ValueError):
    """An inventory that cannot be trusted to describe what to serve."""


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def validate_entry(entry):
    """One artifact, or InventoryError saying which field is wrong."""
    if not isinstance(entry, dict):
        raise InventoryError(f'an artifact entry is {type(entry).__name__}, not an object')
    for key in ('path', 'python_version', 'sha256'):
        if not isinstance(entry.get(key), str) or not entry[key]:
            raise InventoryError(f'an artifact entry has no {key}')
    path, sha = entry['path'], entry['sha256']

    # Checked before the pattern, so the message names the actual problem.
    if '/' in path or '\\' in path or path in ('.', '..') or path.startswith('.'):
        raise InventoryError(f'{path!r} is not a bare filename')
    if '://' in path or '?' in path or '#' in path:
        raise InventoryError(f'{path!r} looks like a URL, not a filename')
    if not _NAME.match(path):
        raise InventoryError(
            f'{path!r} is not a runtime archive name '
            f'(ClickGraft-python-<version>.zip or -<version>-<sha256>.zip)')
    if not _SHA256.match(sha):
        raise InventoryError(f'{path}: {sha!r} is not a lowercase sha256')
    releases = entry.get('releases', [])
    if not isinstance(releases, list) or not all(isinstance(r, str) for r in releases):
        raise InventoryError(f'{path}: releases must be a list of version strings')
    return {'path': path, 'python_version': entry['python_version'],
            'sha256': sha, 'releases': sorted(set(releases))}


def load(path):
    """Read an inventory file. A missing file is an empty inventory, because
    the first deploy after this change has nothing to migrate from."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except ValueError as why:
        raise InventoryError(f'{path} is not readable JSON: {why}') from None
    if not isinstance(raw, dict) or raw.get('schema') != SCHEMA:
        raise InventoryError(f'{path} is not a schema {SCHEMA} runtime inventory')
    entries = raw.get('artifacts')
    if not isinstance(entries, list):
        raise InventoryError(f'{path} has no artifacts list')
    out = {}
    for entry in entries:
        checked = validate_entry(entry)
        name = checked['path']
        if name in out and out[name] != checked:
            raise InventoryError(f'{path} lists {name} twice, with different details')
        out[name] = checked
    return out


def dump(inventory):
    """Serialise, sorted, so a deploy's diff is about content and not ordering."""
    return json.dumps(
        {'schema': SCHEMA,
         'artifacts': [inventory[name] for name in sorted(inventory)]},
        indent=2) + '\n'


def merge(durable, incoming):
    """Additive. A registered path may never acquire different bytes.

    Both directions matter. An entry the incoming checkout omits is KEPT --
    deploying from an older checkout must not retire an artifact a newer one
    registered. And an entry whose hash or Python version disagrees with the
    durable record is refused outright: that is either a re-signed payload
    published under a name apps already trust, or a mistake, and both are
    served to somebody whose app will refuse the bytes.
    """
    out = dict(durable)
    for name, entry in incoming.items():
        existing = out.get(name)
        if existing is None:
            out[name] = entry
            continue
        for field in ('sha256', 'python_version'):
            if existing[field] != entry[field]:
                raise InventoryError(
                    f'{name} is already published with {field} {existing[field]!r}; '
                    f'this deploy says {entry[field]!r}. An immutable name cannot '
                    f'change its bytes -- publish it under a new name instead.')
        out[name] = {**existing,
                     'releases': sorted(set(existing['releases']) | set(entry['releases']))}
    return out


def store_path(site, sha256):
    return Path(site) / STORE_DIR / f'{sha256}.zip'


def ingest(site, source, sha256):
    """Put an archive in the store, verified, without a partial ever counting.

    Written to a temporary file in the same directory and renamed, so an
    interrupted ingest leaves rubbish that the next run overwrites rather than
    a short file that looks like a published artifact.
    """
    source = Path(source)
    got = _sha256(source)
    if got != sha256:
        raise InventoryError(
            f'{source.name} hashes to {got}, but the inventory says {sha256}')
    target = store_path(site, sha256)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        # Verify what is already there rather than trusting its name.
        if _sha256(target) == sha256:
            return target
        target.unlink()
    handle, temporary = tempfile.mkstemp(dir=str(target.parent), suffix='.part')
    os.close(handle)
    try:
        shutil.copyfile(source, temporary)
        if _sha256(temporary) != sha256:
            raise InventoryError(f'{source.name} changed while it was being stored')
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def verify_store(site, inventory):
    """Every registered artifact is present in the store with the right bytes.

    Returns the total retained size, so a deploy can report what it is keeping
    rather than growing silently.
    """
    total = 0
    missing, wrong = [], []
    for name, entry in sorted(inventory.items()):
        path = store_path(site, entry['sha256'])
        if not path.exists():
            missing.append(f'{name} ({entry["sha256"][:12]}…)')
            continue
        if _sha256(path) != entry['sha256']:
            wrong.append(f'{name}: stored bytes do not match its recorded hash')
            continue
        total += path.stat().st_size
    if missing or wrong:
        raise InventoryError(
            'the runtime store cannot serve every retained artifact: '
            + '; '.join(missing + wrong))
    return total


def populate(html, site, inventory):
    """Copy every retained artifact and its checksum sidecar into a tree.

    Called on the INCOMING tree before the exchange, so what goes live already
    holds everything; nothing is written into the live tree in place.
    """
    html = Path(html)
    for name, entry in sorted(inventory.items()):
        source = store_path(site, entry['sha256'])
        shutil.copyfile(source, html / name)
        (html / f'{name}.sha256').write_text(f'{entry["sha256"]}  {name}\n')
    (html / INVENTORY).write_text(dump(inventory))
