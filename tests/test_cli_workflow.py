"""The CLI and the wizard share one build: the same refusals, the same check of
the copy being replaced, the same verify and rollback (agent.run_build).

Up to 1.5.9 `clickgraft build` had its own copy of that workflow: it did not
verify, deleted the copy it replaced as soon as the build finished, and
replaced a T-series copy with 4.8.118 without a word (reproduced 22 Sep 2026).
These tests also pin what the 22 Sep 2026 review found missing: the check just
before the install, the refusal of a copy that is not the one Review showed,
and CLI output that says plainly what became of each copy.

No HP Click is built or launched: the builds are fakes that put a fake copy in
place for real, in a temporary folder.
"""
import hashlib
import os
import plistlib
import shutil
from types import SimpleNamespace

import pytest

from clickgraft import agent, cli
from clickgraft.build import Built, install_copy
from tests.test_review_replace import wired, tmp, _copy, _source, _run, COMMON, T_SERIES
from tests.test_rollback_and_signing import route, quiet, _tree, _fail_verify, _set_aside
from tests.test_rollback_and_signing import _copy as installed_copy


def args(source, out, consent=False, preload=True):
    return SimpleNamespace(source=source, out=out, no_preload=not preload,
                           accept_printer_loss=consent, allow_intel_host=False)


def _cli(capsys, *a, **kw):
    """cmd_build's exit code (0 when it returns) and everything it printed."""
    try:
        cli.cmd_build(args(*a, **kw))
        code = 0
    except SystemExit as exc:
        code = exc.code
    return code, capsys.readouterr().out


@pytest.fixture
def hooked(tmp, route, monkeypatch):
    """route, with a build that ends the way the real one does: it calls
    before_install, then puts the copy in place. route["during"] runs first,
    standing in for whatever happens to the output path while the copy is made."""
    route["during"] = lambda out: None

    def fake_build(source_app_path, output_app_path, before_install=None, **_kw):
        route["built"] += 1
        staging = installed_copy(tmp, "4.10.42", b"the new copy",
                                 name=f"staging_clickgraft_{os.getpid()}")
        route["during"](output_app_path)
        try:
            if before_install is not None:
                before_install()
        except BaseException:
            shutil.rmtree(staging)
            raise
        return Built(output_app_path, install_copy(staging, output_app_path))

    monkeypatch.setattr(agent, "build_apple_silicon_bundle", fake_build)
    return route


def _events(route, out, *extra):
    events = []
    code = agent.run_build(route["source"], out, list(extra), events.append)
    return code, events


def _left_behind(folder):
    return sorted(n for n in os.listdir(folder)
                  if n.startswith(("staging_clickgraft_", ".clickgraft-")))


# --- printer-loss consent ------------------------------------------------------

def test_cli_refuses_printer_loss(tmp, wired, capsys):
    old = _copy(tmp, '4.8.117', T_SERIES + COMMON)
    new = _source(tmp, '4.8.118', COMMON)
    code, output = _cli(capsys, new, old)
    assert code == 1
    assert wired['build'] == 0
    assert '--accept-printer-loss' in output
    # In words, not the wizard's stage names.
    assert 'Not replaced: the new copy would lose printers' in output
    assert 'printers_lost' not in output
    assert 'has been left as it was' in output


def test_cli_accepts_explicit_consent(tmp, wired, capsys):
    old = _copy(tmp, '4.8.117', T_SERIES + COMMON)
    new = _source(tmp, '4.8.118', COMMON)
    code, output = _cli(capsys, new, old, True)
    assert code == 0 and wired['build'] == 1
    assert 'BUILD SUCCESSFUL' in output


def test_cli_says_a_missing_source_does_not_exist(tmp, capsys):
    """The 1.5.9 CLI said this; for a while the manifest lookup that replaced
    it called a missing --source "not a supported HP Click version"."""
    missing = os.path.join(tmp, 'Nowhere', 'HP Click.app')
    code, output = _cli(capsys, missing, os.path.join(tmp, agent.APP_NAME))
    assert code == 1
    assert 'Source app path does not exist' in output
    assert 'not a supported' not in output
    assert os.listdir(tmp) == []


# --- verify decides what happens to each copy ---------------------------------

def test_cli_failed_verify_restores_copy(tmp, route, capsys):
    old = installed_copy(tmp, '4.8.117', b'previous working app')
    before = _tree(old)
    route['verify'] = _fail_verify()
    code, output = _cli(capsys, route['source'], old)
    assert code == 1
    assert _tree(old) == before
    assert 'The new copy did not pass its checks' in output
    assert 'The copy that was there has been put back as it was' in output
    assert 'The new copy has been removed' in output
    assert 'BUILD SUCCESSFUL' not in output


def test_a_verifier_returning_false_restores_the_previous_copy(tmp, route, capsys):
    """verify_app_bundle raises for every failure it knows. A (False, results)
    used to be settled as a pass, and the copy it replaced deleted."""
    old = installed_copy(tmp, '4.8.117', os.urandom(4096))
    before, inode = _tree(old), os.stat(old).st_ino
    route['verify'] = lambda out: (False, {'architectures': 'PASSED (fake)'})

    code, events = _events(route, old)
    ev = events[-1]
    assert code == 1 and ev['stage'] == 'verify' and ev['check'] == 'verification'
    assert ev['previous_copy'] == 'restored' and ev['new_copy'] == 'removed'
    assert ev['results'] == {'architectures': 'PASSED (fake)'}
    assert _tree(old) == before and os.stat(old).st_ino == inode
    assert _set_aside(tmp) == []

    code, output = _cli(capsys, route['source'], old)
    assert code == 1 and 'put back as it was' in output and 'BUILD SUCCESSFUL' not in output


def test_a_verifier_returning_false_with_nothing_before_keeps_and_says_so(tmp, route, capsys):
    """Nothing to put back, so the new copy stays; the 1.5.9-era CLI said only
    "Previous copy: none" and never that an unchecked copy was left there."""
    route['verify'] = lambda out: (False, {})
    code, events = _events(route, route['out'])
    ev = events[-1]
    assert code == 1 and ev['previous_copy'] == 'none' and ev['new_copy'] == 'kept'
    assert os.path.isdir(route['out'])

    shutil.rmtree(route['out'])
    code, output = _cli(capsys, route['source'], route['out'])
    assert code == 1 and os.path.isdir(route['out'])
    assert 'There was no copy at the output path.' in output
    assert 'The new copy was left at' in output and 'did NOT pass its checks' in output


def test_cli_prints_where_the_log_is(tmp, route, capsys, monkeypatch):
    log = os.path.join(tmp, 'clickgraft-test.log')
    monkeypatch.setattr(agent, '_log_path', lambda: log)
    route['verify'] = _fail_verify()
    code, output = _cli(capsys, route['source'], route['out'])
    assert code == 1 and f'The full log is at {log}' in output


# --- the copy being replaced changes during the build --------------------------

def _set_version(app, version):
    """Edit Info.plist where it is: same file, same folder, same inode."""
    p = os.path.join(app, 'Contents', 'Info.plist')
    with open(p, 'rb') as f:
        info = plistlib.load(f)
    info['CFBundleShortVersionString'] = version
    with open(p, 'r+b') as f:
        f.truncate(0)
        plistlib.dump(info, f)


def test_the_build_asks_before_install_and_goes_ahead_when_nothing_changed(tmp, hooked):
    old = installed_copy(tmp, '4.8.117', b'old')
    code, events = _events(hooked, old)
    assert code == 0 and events[-1]['type'] == 'done'
    assert events[-1]['previous_copy'] == 'replaced'


@pytest.mark.parametrize('how', ['edited_in_place', 'swapped'])
def test_a_replacement_changed_during_the_build_is_left_alone(tmp, hooked, how):
    old = installed_copy(tmp, '4.8.117', b'old')
    inode = os.stat(old).st_ino

    def change(out):
        if how == 'edited_in_place':
            _set_version(out, '4.8.118')
        else:
            os.rename(out, os.path.join(tmp, 'moved.app'))
            _copy(tmp, '4.8.117', T_SERIES + COMMON)
    hooked['during'] = change

    code, events = _events(hooked, old)
    ev = events[-1]
    assert code == 1 and hooked['built'] == 1
    assert ev['stage'] == 'replacement_changed' and ev['change'] == 'changed'
    assert ev['during_build'] is True and ev['previous_copy'] == 'untouched'
    assert ev['output_exists'] is True
    assert 'changed while the new one was being made' in ev['error']
    if how == 'edited_in_place':
        assert os.stat(old).st_ino == inode
        assert agent._bundle_version(old) == '4.8.118'
    assert _left_behind(tmp) == [], 'a staging copy or a set-aside copy was left'


def test_a_replacement_removed_during_the_build_is_not_replaced(tmp, hooked, capsys):
    old = installed_copy(tmp, '4.8.117', b'old')
    hooked['during'] = shutil.rmtree
    code, events = _events(hooked, old)
    ev = events[-1]
    assert code == 1 and ev['stage'] == 'replacement_changed' and ev['change'] == 'removed'
    assert ev['previous_copy'] == 'gone' and ev['output_exists'] is False
    assert not os.path.lexists(old), 'the new copy was put there after all'
    assert _left_behind(tmp) == []

    old = installed_copy(tmp, '4.8.117', b'old')
    code, output = _cli(capsys, hooked['source'], old)
    assert code == 1 and 'Not replaced: the copy there changed' in output
    assert 'removed by something else during the build' in output
    assert 'left as it was' not in output


def test_a_copy_that_appears_during_the_build_is_left_alone(tmp, hooked, capsys):
    out = hooked['out']
    hooked['during'] = lambda path: installed_copy(tmp, '4.8.117', b'appeared')
    code, events = _events(hooked, out)
    ev = events[-1]
    assert code == 1 and ev['stage'] == 'replacement_changed' and ev['change'] == 'appeared'
    assert ev['previous_copy'] == 'untouched' and ev['output_exists'] is True
    assert _tree(out)['Contents/Resources/marker.bin'] == hashlib.sha256(b'appeared').hexdigest()
    assert _left_behind(tmp) == []

    shutil.rmtree(out)
    code, output = _cli(capsys, hooked['source'], out)
    assert code == 1 and 'A copy appeared at' in output
    assert 'There was no copy' not in output


# --- Review's token: the copy the consent was given for -------------------------

def test_plan_carries_the_token_for_what_it_showed(tmp, wired, monkeypatch):
    old = _copy(tmp, '4.8.117', T_SERIES + COMMON)
    new = _source(tmp, '4.8.118', COMMON)
    monkeypatch.setattr(agent, 'resolve_output', lambda: (old, False))
    code, events = _run(['plan', '--source', new, '--out', old])
    assert code == 0
    assert events[-1]['plan']['replacing_token'] == agent.replacement_token(old, new)
    # Nothing there is a token too, so a copy that appears can be told apart.
    empty = os.path.join(tmp, 'Empty', agent.APP_NAME)
    assert agent.replacement_token(empty, new) == agent._NOTHING_TOKEN
    assert agent.replacement_token(old, new) != agent._NOTHING_TOKEN


def test_the_token_does_not_change_when_the_copy_is_opened_or_quit(tmp, wired):
    old = _copy(tmp, '4.8.117', COMMON)
    new = _source(tmp, '4.8.118', COMMON)
    quiet_token = agent.replacement_token(old, new)
    wired['running'] = [(4242, f'{old}/Contents/MacOS/HPClickExe')]
    assert agent.replacement_token(old, new) == quiet_token


def test_a_matching_token_builds(tmp, wired):
    old = _copy(tmp, '4.8.117', T_SERIES + COMMON)
    new = _source(tmp, '4.8.118', COMMON)
    token = agent.replacement_token(old, new)
    code, events = _run(['build', '--source', new, '--out', old,
                         '--accept-printer-loss', '--expect-replacing', token])
    assert code == 0 and events[-1]['type'] == 'done' and wired['build'] == 1


def test_printer_consent_given_on_review_does_not_cover_another_copy(tmp, wired):
    """The tick was for the copy Review showed. Before the token it was a bare
    flag, and a T-series copy put there after Review was replaced on it."""
    old = _copy(tmp, '4.8.118', COMMON)
    new = _source(tmp, '4.8.118', COMMON)
    token = agent.replacement_token(old, new)           # Review: nothing lost
    shutil.rmtree(old)
    _copy(tmp, '4.8.117', T_SERIES + COMMON)            # then swapped
    code, events = _run(['build', '--source', new, '--out', old,
                         '--accept-printer-loss', '--expect-replacing', token])
    ev = events[-1]
    assert code == 1 and wired['build'] == 0, 'the build started'
    assert ev['stage'] == 'replacement_changed' and ev['change'] == 'changed'
    assert ev['during_build'] is False and ev['previous_copy'] == 'untouched'
    assert 'since you reviewed it' in ev['error']
    assert 'Nothing has been downloaded or changed' in ev['error']


def test_an_in_place_edit_after_review_is_seen(tmp, wired):
    old = _copy(tmp, '4.8.117', COMMON)
    new = _source(tmp, '4.8.118', COMMON)
    token = agent.replacement_token(old, new)
    inode = os.stat(old).st_ino
    _set_version(old, '4.8.118')
    assert os.stat(old).st_ino == inode
    code, events = _run(['build', '--source', new, '--out', old, '--expect-replacing', token])
    assert code == 1 and wired['build'] == 0
    assert events[-1]['change'] == 'changed'


def test_a_copy_removed_after_review_is_gone_not_untouched(tmp, wired):
    old = _copy(tmp, '4.8.117', COMMON)
    new = _source(tmp, '4.8.118', COMMON)
    token = agent.replacement_token(old, new)
    shutil.rmtree(old)
    code, events = _run(['build', '--source', new, '--out', old, '--expect-replacing', token])
    ev = events[-1]
    assert code == 1 and wired['build'] == 0
    assert ev['change'] == 'removed' and ev['previous_copy'] == 'gone'
    assert ev['output_exists'] is False


def test_a_copy_that_appeared_after_review_is_not_replaced(tmp, wired):
    new = _source(tmp, '4.8.118', COMMON)
    out = os.path.join(tmp, agent.APP_NAME)
    token = agent.replacement_token(out, new)            # Review: nothing there
    _copy(tmp, '4.8.117', COMMON)
    code, events = _run(['build', '--source', new, '--out', out, '--expect-replacing', token])
    ev = events[-1]
    assert code == 1 and wired['build'] == 0
    assert ev['change'] == 'appeared' and ev['previous_copy'] == 'untouched'
    assert ev['output_exists'] is True
