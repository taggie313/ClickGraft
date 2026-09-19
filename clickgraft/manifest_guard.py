"""
clickgraft.manifest_guard — Which patch ops a manifest may contain, and which it never may.

CLAUDE.md "Never distribute": the ink-level workaround and the advertising
removal stay out of the shipped tool and out of every manifest, because
publishing them invites HP to lock the app against the graft itself. Until
1.5.8 nothing enforced that. Nothing on the release path runs the tests --
build_app.sh and sign_and_notarize.sh do not, and there is no CI or git hook
(checked 19 Sep 2026) -- so a manifest edit that looked reviewed would have
shipped.

The lock that ships is ALLOWED_OPS: every op the shipped manifests use,
verbatim. A new file, anchor, replacement, value or field fails until someone
adds it here on purpose, in a change that can be reviewed. Nothing else is
needed to stop a never-distribute op, because such an op is not on it.

There is a second, optional lock, checked on its own so that an op matching it
fails even if it has been added to ALLOWED_OPS: a list of signatures read from
a file outside this repository, named by CLICKGRAFT_NEVER_DISTRIBUTE. Its
contents are deliberately not here. Written out, they would name the very code
CLAUDE.md says not to publish, in a module that packaging/build_app.sh copies
into ClickGraft.app and that lives in a public repository -- and publishing it
is what invites HP to lock the app against the graft. Format: one
"label<TAB>regex" per line, "#" for comments, matched against every string in
every op.

Enforced in two places: build.py, before a graft fetches or writes anything
however the manifest arrived; and packaging/build_app.sh, before manifests/ is
copied into ClickGraft.app, as

    python3 -m clickgraft.manifest_guard manifests/

Target: Python 3.9+ (Standard Library only)
"""

import json
import os
import re
import sys


# HP Click 4.8 and 4.10 log the SNMPv3 user name and both passwords when Return
# is pressed in an SNMPv3 field; 4.11.31 logs the replacement line instead.
_SNMP_CREDENTIAL_LOG = ('this.log("credentials key enter with - authenticationPassword: "'
                        '+this.authenticationPassword+" policyPassword: "+this.policyPassword'
                        '+" userName: "+this.userName)')

# (patch path, op), compared as canonical JSON, so False and 0 differ and an
# extra key such as "create": true makes it a different op.
ALLOWED_OPS = (
    # Crash reports off: app/main.js require()s app/package.json and passes
    # this key to crashReporter.start as uploadToServer. 4.8.117, 4.8.118, 4.10.42.
    ("app/package.json",
     {"type": "json_set", "path": "hp_configs.crashAutoSubmit", "value": False}),
    # Updater lock. 4.8.117, 4.8.118, 4.10.42.
    ("app/node/main/app-updater.js",
     {"type": "replace", "anchor": "function startup(e){",
      "replacement": "function startup(e){return;"}),
    # HP's SyntaxError where index.html loads these as classic scripts. 4.8.x only.
    ("app/shared/constants.js",
     {"type": "replace", "anchor": "export var SharedConstants;",
      "replacement": "var SharedConstants;"}),
    ("app/shared/constants.js",
     {"type": "append",
      "text": "\nif (typeof exports !== 'undefined') { exports.SharedConstants = SharedConstants; }\n"}),
    ("app/shared/industries.js",
     {"type": "replace", "anchor": "export const Industries = [",
      "replacement": "const Industries = ["}),
    ("app/shared/industries.js",
     {"type": "append",
      "text": "\nif (typeof exports !== 'undefined') { exports.Industries = Industries; }\n"}),
    # SNMPv3 credentials out of the log, as HP's own 4.11.31 line, verbatim.
    # 4.8.117, 4.8.118, 4.10.42.
    ("app/bundle.js",
     {"type": "replace", "anchor": _SNMP_CREDENTIAL_LOG,
      "replacement": 'this.log("credentials key enter event received")'}),
)

NEVER_DISTRIBUTE_ENV = "CLICKGRAFT_NEVER_DISTRIBUTE"


def load_never_distribute(path=None):
    """[(label, compiled pattern)] from a signature file, or [] without one.

    Each pattern is searched in the lower-cased string and again with "-", "_"
    and whitespace removed, so one signature covers a name however it is spelt.
    A file that is named but unreadable, or that has a pattern re cannot
    compile, raises: a lock that silently does nothing is worse than none.
    """
    if path is None:
        path = os.environ.get(NEVER_DISTRIBUTE_ENV) or ""
    if not path:
        return []
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    out = []
    for i, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        label, _tab, pattern = line.partition("\t")
        pattern = pattern.strip() or label
        try:
            out.append((label.strip(), re.compile(pattern.lower())))
        except re.error as e:
            raise ValueError(f"{path}:{i}: {pattern!r} is not a valid regex: {e}") from None
    return out


_PATTERN_CACHE = {}


def _patterns():
    """The loaded signatures, read once per file named. Read through the
    environment on each call rather than at import, so the guard a build runs
    is the one that build's environment names."""
    path = os.environ.get(NEVER_DISTRIBUTE_ENV) or ""
    if path not in _PATTERN_CACHE:
        _PATTERN_CACHE[path] = tuple(load_never_distribute(path))
    return _PATTERN_CACHE[path]


def _canonical(path, op):
    return json.dumps([path, op], sort_keys=True)


_ALLOWED = frozenset(_canonical(path, op) for path, op in ALLOWED_OPS)


class ManifestGuardError(ValueError):
    """A manifest contains an op the guard does not allow."""


def forbidden(text):
    """The never-distribute label that `text` matches, or None. Always None
    when no signature file is named (see the module docstring)."""
    lowered = text.lower()
    squashed = re.sub(r"[-_\s]", "", lowered)
    for label, pattern in _patterns():
        if pattern.search(lowered) or pattern.search(squashed):
            return label
    return None


def _strings(path, op):
    """Every string in an op: the file it patches, and each key and value,
    anchor, replacement, appended text and JSON path included."""
    yield path
    for key, value in op.items():
        yield str(key)
        yield value if isinstance(value, str) else json.dumps(value)


def _describe(path, op):
    key = op.get("anchor", op.get("path", op.get("text", "")))
    key = key if isinstance(key, str) else json.dumps(key)
    if len(key) > 80:
        key = key[:77] + "..."
    return f"{path}: {op.get('type', '(no type)')} {key!r}"


def problems(manifest):
    """Every reason this manifest may not be used, as a list of lines.

    Empty means it passes.
    """
    patches = manifest.get("patches") if isinstance(manifest, dict) else None
    if not isinstance(patches, list):
        return ["'patches' is missing or is not a list"]

    found = []
    seen = set()
    for i, patch in enumerate(patches):
        if not isinstance(patch, dict) or not isinstance(patch.get("path"), str):
            found.append(f"patches[{i}] has no 'path'")
            continue
        path = patch["path"]
        if path in seen:
            found.append(f"{path}: listed more than once, and only one entry per file can apply")
        seen.add(path)

        ops = patch.get("ops")
        if not isinstance(ops, list) or not ops:
            found.append(f"{path}: has no ops")
            label = forbidden(path)
            if label:
                found.append(f"{path}: matches the never-distribute signature '{label}'")
            continue
        for op in ops:
            if not isinstance(op, dict):
                found.append(f"{path}: an op is not an object")
                continue
            label = next((hit for hit in map(forbidden, _strings(path, op)) if hit), None)
            if label:
                found.append(f"{_describe(path, op)}: matches the never-distribute signature '{label}'")
            elif _canonical(path, op) not in _ALLOWED:
                found.append(f"{_describe(path, op)}: not on the allowlist")
    return found


def _failure(name, lines):
    return (f"Manifest {name} failed ClickGraft's patch guard (clickgraft/manifest_guard.py):\n  - "
            + "\n  - ".join(lines)
            + "\nOps on ALLOWED_OPS are the only ones a manifest may contain; a new op is added "
              "there on purpose, in its own reviewed change. An op matching a never-distribute "
              "signature is never added: see CLAUDE.md, \"Never distribute\".")


def check_manifest(manifest, name=None):
    """Raise ManifestGuardError unless every op in `manifest` is allowed."""
    found = problems(manifest)
    if found:
        if name is None:
            name = (manifest.get("app_version") if isinstance(manifest, dict) else None) or "(unnamed)"
        raise ManifestGuardError(_failure(name, found))


def check_manifests_dir(manifests_dir):
    """Every failure in a manifests/ directory, one message per bad file.

    A file that does not parse fails too: at run time ManifestManager would
    quietly leave that version out, which is not something to ship.
    """
    failures = []
    names = sorted(f for f in os.listdir(manifests_dir) if f.endswith(".json"))
    if not names:
        failures.append(f"No manifests in {manifests_dir}")
    for fname in names:
        try:
            with open(os.path.join(manifests_dir, fname), "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            failures.append(f"Manifest {fname} could not be read: {e}")
            continue
        found = problems(manifest)
        if found:
            failures.append(_failure(fname, found))
    return failures


def main(argv):
    if len(argv) != 1 or not os.path.isdir(argv[0]):
        print("usage: python3 -m clickgraft.manifest_guard <manifests dir>", file=sys.stderr)
        return 2
    failures = check_manifests_dir(argv[0])
    for message in failures:
        print(message, file=sys.stderr)
    if failures:
        return 1
    count = len([f for f in os.listdir(argv[0]) if f.endswith(".json")])
    # Say when the optional second lock was in use, so a release build's output
    # shows whether $CLICKGRAFT_NEVER_DISTRIBUTE was actually found.
    extra = len(_patterns())
    print(f"    {count} manifest(s) pass the patch guard"
          + (f", and {extra} local never-distribute signature(s)" if extra else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
