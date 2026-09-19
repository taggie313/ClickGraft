"""
tests/test_verify_safety.py — What verify may touch, and what it checks on the result.

Until 1.5.8 the smoke launch ran `pkill -x HPClickExe` (every HP Click on the
Mac, the user's own included, even mid-print) and deleted HP's two current
logs. These tests pin the replacement: only processes running from inside the
copy are ever signalled, HP's logs are only ever read, and the launch runs
with its own user-data dir and TMPDIR.

build.py's refusal to delete a running copy is tested here too, because this is
where the stand-in bundles are.

Nothing here launches HP Click. The live tests start a tiny compiled stand-in
inside fake .app bundles in a temporary folder, and skip without clang.
Target: Python 3.9+
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from clickgraft import verify
from clickgraft.verify import (LogTail, SNMP_CREDENTIAL_FRAGMENT, check_patch_outcomes,
                               kill_hpclick_processes, launched_processes, processes_inside,
                               smoke_launch)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

COPY = "/Users/x/Applications/HP Click (Apple Silicon).app"
FAKE_PS = f"""\
  101 /Applications/HP Click.app/Contents/MacOS/HPClickExe
  102 /Applications/HP Click.app/Contents/Frameworks/HP Click Helper (Renderer).app/Contents/MacOS/HP Click Helper (Renderer) --type=renderer
  103 /Applications/HP Click.app/Contents/Resources/app/appData/macx/bin/JDFPrintProcessor
  201 {COPY}/Contents/MacOS/HPClickExe --user-data-dir=/private/tmp/cg-smoke-abc/user-data
  202 {COPY}/Contents/Frameworks/HP Click Helper (GPU).app/Contents/MacOS/HP Click Helper (GPU) --type=gpu-process
  203 {COPY}/Contents/Resources/app/appData/macx/bin/JDFPrintProcessor -tmpdir /private/tmp/cg-smoke-abc
  204 {COPY}/Contents/Frameworks/Electron Framework.framework/Helpers/chrome_crashpad_handler --no-rate-limit
  301 /Users/x/Applications/HP Click (Apple Silicon) 2.app/Contents/MacOS/HPClickExe
  302 /Users/x/Applications/HP Click (Apple Silicon).application/Contents/MacOS/HPClickExe
  303 /Applications/HP Click (Apple Silicon).app/Contents/MacOS/HPClickExe
  401 /usr/bin/python3 -m clickgraft.cli verify --app {COPY}
  402 /bin/bash {COPY}/Contents/MacOS/HP Click
  501 <defunct>
  garbage line without a pid
"""


# ---------------------------------------------------------------------------
# The matcher


def test_selects_only_processes_inside_the_copy():
    assert [pid for pid, _ in processes_inside(COPY, FAKE_PS)] == [201, 202, 203, 204]


def test_stock_hp_click_is_never_selected():
    for pid, cmd in processes_inside(COPY, FAKE_PS):
        assert not cmd.startswith("/Applications/HP Click.app/")
    stock = "/Applications/HP Click.app/Contents/MacOS/HPClickExe"
    assert stock not in [c for _, c in processes_inside(COPY, FAKE_PS)]


def test_sibling_bundles_sharing_a_prefix_are_not_selected():
    got = {pid for pid, _ in processes_inside(COPY, FAKE_PS)}
    assert 301 not in got          # "... (Apple Silicon) 2.app"
    assert 302 not in got          # "... (Apple Silicon).application"
    assert 303 not in got          # same name, different folder


def test_the_stock_app_matches_only_itself():
    got = [pid for pid, _ in processes_inside("/Applications/HP Click.app", FAKE_PS)]
    assert got == [101, 102, 103]


def test_path_in_arguments_is_not_enough():
    # clickgraft's own `verify --app <copy>` and the launcher's bash, whose
    # executables are not inside the copy.
    got = {pid for pid, _ in processes_inside(COPY, FAKE_PS)}
    assert 401 not in got and 402 not in got


def test_trailing_slash_and_metacharacters():
    odd = "/tmp/a+b [x] (y) ^$.*?|{1}.app"
    ps = (f"  7 {odd}/Contents/MacOS/HPClickExe\n"
          f"  8 /tmp/a+b [x] (y) ^$.*?|{{1}}X.app/Contents/MacOS/HPClickExe\n"
          f"  9 /tmp/aab x y .app/Contents/MacOS/HPClickExe\n")
    assert [p for p, _ in processes_inside(odd + "/", ps)] == [7]


def test_symlinked_path_matches_resolved_command(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    via_link = str(tmp_path / "link" / "HP Click (Apple Silicon).app")
    resolved = os.path.realpath(str(real / "HP Click (Apple Silicon).app"))
    ps = f"  11 {resolved}/Contents/MacOS/HPClickExe\n"
    assert [p for p, _ in processes_inside(via_link, ps)] == [11]


def test_same_folder_spelt_another_way(tmp_path):
    # Seen 19 Sep 2026: the copy started as /private/tmp/.../X.app, and its
    # chrome_crashpad_handler ran as /tmp/.../X.app/... pytest's tmp_path is
    # under /private/var, which is also reachable as /var.
    real = os.path.realpath(str(tmp_path))
    if not real.startswith("/private/"):
        pytest.skip("tmp_path is not under /private here")
    app = os.path.join(real, "HP Click (Apple Silicon).app")
    os.makedirs(os.path.join(app, "Contents", "MacOS"))
    short = real[len("/private"):]
    ps = (f"  21 {short}/HP Click (Apple Silicon).app/Contents/Frameworks/Electron Framework.framework/Helpers/chrome_crashpad_handler --no-rate-limit\n"
          f"  22 {short}/HP Click (Apple Silicon) 2.app/Contents/MacOS/HPClickExe\n"
          f"  23 /bin/echo {app}/Contents/MacOS/HPClickExe\n"
          f"  24 /bin/echo /../..{app}/Contents/MacOS/HPClickExe\n"
          f"  25 {short}/./HP Click (Apple Silicon).app/Contents/MacOS/HPClickExe\n")
    assert [p for p, _ in processes_inside(app, ps)] == [21]


def test_own_process_is_never_selected():
    ps = f"  {os.getpid()} {COPY}/Contents/MacOS/HPClickExe\n  12 {COPY}/Contents/MacOS/HPClickExe\n"
    assert [p for p, _ in processes_inside(COPY, ps)] == [12]


@pytest.mark.parametrize("bad", ["/", "/Applications", "/Applications/HP Click.app/Contents", ""])
def test_refuses_anything_but_an_app_bundle(bad):
    with pytest.raises(ValueError):
        processes_inside(bad, FAKE_PS)


def test_live_process_table_selects_nothing_outside(tmp_path):
    # Read-only: ps, no signals.
    target = str(tmp_path / "HP Click (Apple Silicon).app")
    assert processes_inside(target) == []


# ---------------------------------------------------------------------------
# One launch's own processes
#
# "Inside the copy" is not the same as "this launch's". The owner can open the
# copy while the smoke launch is running -- the wizard has just put it in
# /Applications -- and their already-open copy can spawn helpers of its own.
# Only what carries this launch's mark, or descends from the process it
# started, is ours to stop.

MARK = "cg-smoke-abc"
FAKE_PS_E = f"""\
  201     1 {COPY}/Contents/MacOS/HPClickExe --user-data-dir=/private/tmp/{MARK}/user-data
  202   201 {COPY}/Contents/Frameworks/HP Click Helper (GPU).app/Contents/MacOS/HP Click Helper (GPU) --type=gpu-process TMPDIR=/private/tmp/{MARK}/ HOME=/Users/x
  203     1 {COPY}/Contents/Resources/app/appData/macx/bin/JDFPrintProcessor -tmpdir /tmp/{MARK}
  204   201 {COPY}/Contents/Frameworks/Electron Framework.framework/Helpers/chrome_crashpad_handler --database=/private/tmp/{MARK}/user-data/Crashpad
  205   201 {COPY}/Contents/Resources/app/appData/macx/bin/JDFPrintProcessor
  301     1 {COPY}/Contents/MacOS/HPClickExe
  302   301 {COPY}/Contents/Frameworks/HP Click Helper (Renderer).app/Contents/MacOS/HP Click Helper (Renderer) --type=renderer
  101     1 /Applications/HP Click.app/Contents/MacOS/HPClickExe TMPDIR=/private/tmp/{MARK}/
  garbage line
"""


def test_the_launch_selects_its_marked_and_descended_processes():
    got = [pid for pid, _ in launched_processes(COPY, MARK, 201, FAKE_PS_E)]
    # 201 the process started, 202 marked in its environment, 203 an orphan
    # marked in its arguments and spelling /private/tmp as /tmp, 204 crashpad's
    # --database=, 205 a child with no mark at all.
    assert got == [201, 202, 203, 204, 205]


def test_a_copy_the_user_opens_during_the_launch_is_not_selected():
    got = {pid for pid, _ in launched_processes(COPY, MARK, 201, FAKE_PS_E)}
    assert 301 not in got and 302 not in got


def test_the_mark_alone_is_not_enough_outside_the_bundle():
    got = {pid for pid, _ in launched_processes(COPY, MARK, 201, FAKE_PS_E)}
    assert 101 not in got


def test_without_a_root_pid_only_the_mark_counts():
    got = [pid for pid, _ in launched_processes(COPY, MARK, None, FAKE_PS_E)]
    assert got == [201, 202, 203, 204]


def test_an_orphaned_process_is_still_ours():
    # Its parent is gone, so root_pid cannot reach it; TMPDIR can.
    ps = f"  77     1 {COPY}/Contents/MacOS/HPClickExe TMPDIR=/private/tmp/{MARK}/\n"
    assert [p for p, _ in launched_processes(COPY, MARK, 999, ps)] == [77]


def test_a_parent_loop_does_not_hang():
    ps = (f"  11    12 {COPY}/Contents/MacOS/HPClickExe\n"
          f"  12    11 {COPY}/Contents/MacOS/HPClickExe\n")
    assert launched_processes(COPY, MARK, 999, ps) == []


def test_own_process_is_never_selected_by_mark():
    ps = f"  {os.getpid()}     1 {COPY}/Contents/MacOS/HPClickExe --user-data-dir=/private/tmp/{MARK}/u\n"
    assert launched_processes(COPY, MARK, None, ps) == []


# ---------------------------------------------------------------------------
# The killer, without real processes


def test_no_target_means_nothing_is_signalled(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("must not look at or signal any process")
    monkeypatch.setattr(verify.subprocess, "run", refuse)
    monkeypatch.setattr(os, "kill", refuse)
    assert kill_hpclick_processes(None) is True
    assert kill_hpclick_processes("") is True


def _kill_with_table(monkeypatch, table_text, three_column, **kw):
    """kill_hpclick_processes against a fixed ps table. Returns what it
    signalled; a signalled pid leaves the next sweep's table."""
    table = {"ps": table_text}
    sent = []

    def fake_read(path, *a, **k):
        # processes_inside(path, ps_output) and launched_processes(path, mark,
        # root_pid, ps_output) both end in the table.
        return table["ps"]

    def fake_kill(pid, sig):
        sent.append((pid, sig))
        table["ps"] = "\n".join(ln for ln in table["ps"].splitlines()
                                if not ln.strip().startswith(f"{pid} "))

    if three_column:
        monkeypatch.setattr(verify, "_ps_table", lambda: table["ps"])
    else:
        monkeypatch.setattr(verify.subprocess, "run",
                            lambda *a, **k: subprocess.CompletedProcess(a, 0, table["ps"], ""))
    monkeypatch.setattr(os, "kill", fake_kill)
    result = kill_hpclick_processes(COPY, **kw)
    return result, sent


def test_signals_only_the_launch_that_asked(monkeypatch):
    ok, sent = _kill_with_table(monkeypatch, FAKE_PS_E, True, mark=MARK, root_pid=201)
    assert ok is True
    assert sorted(p for p, _ in sent) == [201, 202, 203, 204, 205]
    assert all(sig == signal.SIGTERM for _, sig in sent)
    # The copy the owner opened during the launch, and its renderer, are left.
    assert not {301, 302} & {p for p, _ in sent}


def test_with_no_mark_everything_inside_the_bundle_is_signalled(monkeypatch):
    ok, sent = _kill_with_table(monkeypatch, FAKE_PS, False)
    assert ok is True
    assert sorted(p for p, _ in sent) == [201, 202, 203, 204]


def test_source_never_kills_by_name_or_deletes_hp_logs():
    with open(os.path.join(REPO, "clickgraft", "verify.py"), encoding="utf-8") as f:
        src = f.read()
    code = "\n".join(ln.split("#", 1)[0] for ln in src.splitlines())
    for banned in ('"pkill"', '"killall"', '"pgrep"', "os.remove(", "os.unlink(",
                   "DARWIN_USER_TEMP_DIR", ".truncate("):
        assert banned not in code, f"verify.py uses {banned}"


# ---------------------------------------------------------------------------
# The log reader


def test_log_tail_reads_only_what_is_appended(tmp_path):
    p = tmp_path / "HP Click.log"
    p.write_bytes(b"old line from the user's session\n")
    t = LogTail(str(p))
    assert t.read_new() == ""
    with open(p, "ab") as f:
        f.write(b"successful initialization\n")
    assert t.read_new() == "successful initialization\n"
    assert "old line" not in t.text
    assert t.read_new() == ""


def test_log_tail_file_created_later(tmp_path):
    p = tmp_path / "HP Click App.main.log"
    t = LogTail(str(p))
    assert t.read_new() == ""
    p.write_bytes(b"debug: starting app...\n")
    assert t.read_new() == "debug: starting app...\n"


def test_log_tail_follows_rotation(tmp_path):
    # HP's logger-service renames the current log and starts a new one.
    p = tmp_path / "HP Click App.main.log"
    p.write_bytes(b"x" * 100)
    t = LogTail(str(p))
    os.rename(p, tmp_path / "HP Click App.main.20260919_092053.log")
    p.write_bytes(b"fresh\n")
    assert t.read_new() == "fresh\n"


def test_log_tail_never_deletes_or_changes_files(tmp_path):
    files = {tmp_path / "HP Click App.main.log": b"main before\n",
             tmp_path / "HP Click.log": b"renderer before\n"}
    for p, data in files.items():
        p.write_bytes(data)
    tails = [LogTail(str(p)) for p in files]
    for p in files:
        with open(p, "ab") as f:
            f.write(b"appended\n")
    for _ in range(3):
        for t in tails:
            t.read_new()
    for p, data in files.items():
        assert p.exists()
        assert p.read_bytes() == data + b"appended\n"


# ---------------------------------------------------------------------------
# Outcome checks

HP_OLD_SNMP = ('this.log("credentials key enter with - authenticationPassword: "'
               '+this.authenticationPassword+" policyPassword: "+this.policyPassword'
               '+" userName: "+this.userName)')
HP_NEW_SNMP = 'this.log("credentials key enter event received")'
SNMP_MANIFEST = {"patches": [{"path": "app/bundle.js", "ops": [
    {"type": "replace", "anchor": HP_OLD_SNMP, "replacement": HP_NEW_SNMP}]}]}


def _files(**kw):
    files = {"app/package.json": json.dumps({"hp_configs": {"crashAutoSubmit": False}}),
             "app/bundle.js": f"a();{HP_NEW_SNMP};b();"}
    files.update(kw)
    return lambda rel: files.get(rel)


def test_outcomes_pass_on_a_good_copy():
    res = check_patch_outcomes(_files(), SNMP_MANIFEST)
    assert res["crash_reports"].startswith("PASSED")
    assert res["snmp_log_line"].startswith("PASSED")


@pytest.mark.parametrize("value", [True, "false", 0, None])
def test_crash_auto_submit_must_be_exactly_false(value):
    pkg = json.dumps({"hp_configs": {"crashAutoSubmit": value}})
    with pytest.raises(ValueError, match="crashAutoSubmit"):
        check_patch_outcomes(_files(**{"app/package.json": pkg}), {})


def test_crash_key_absent_fails():
    with pytest.raises(ValueError, match="crashAutoSubmit"):
        check_patch_outcomes(_files(**{"app/package.json": json.dumps({"hp_configs": {}})}), {})


def test_root_package_json_alone_does_not_count():
    # Every copy before 1.5.8: root package.json false, app/package.json true.
    files = {"package.json": json.dumps({"hp_configs": {"crashAutoSubmit": False}}),
             "app/package.json": json.dumps({"hp_configs": {"crashAutoSubmit": True}})}
    with pytest.raises(ValueError, match="still set to upload"):
        check_patch_outcomes(files.get, {})
    with pytest.raises(ValueError, match="missing"):
        check_patch_outcomes({"package.json": files["package.json"]}.get, {})


def test_snmp_line_left_in_fails():
    with pytest.raises(ValueError, match="SNMPv3"):
        check_patch_outcomes(_files(**{"app/bundle.js": HP_OLD_SNMP}), SNMP_MANIFEST)


def test_snmp_check_only_where_the_manifest_has_the_op():
    # 4.11.31-style: no SNMP op, so bundle.js is not examined at all.
    res = check_patch_outcomes(_files(**{"app/bundle.js": None}), {"patches": []})
    assert "snmp_log_line" not in res
    assert SNMP_CREDENTIAL_FRAGMENT in HP_OLD_SNMP


@pytest.mark.parametrize("version", ["4.8.117", "4.8.118", "4.10.42"])
def test_shipped_manifests_carry_the_snmp_op_and_pass_without_shared_ops(version):
    with open(os.path.join(REPO, "manifests", f"{version}.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    assert verify._manifest_patches_snmp_line(manifest)
    # Nothing in the outcome checks reads app/shared/, which 4.10.42 no longer patches.
    res = check_patch_outcomes(_files(), manifest)
    assert set(res) == {"crash_reports", "snmp_log_line"}


def test_outcomes_against_stock_4_8_117_and_its_patched_form():
    stock = "/Applications/4.8.117 HP Click.app"
    asar_p = os.path.join(stock, "Contents", "Resources", "app.asar")
    if not os.path.exists(asar_p):
        pytest.skip("no stock HP Click 4.8.117 at " + stock)
    from clickgraft.asar import AsarArchive
    from clickgraft.patches import PatchEngine
    with open(os.path.join(REPO, "manifests", "4.8.117.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    archive = AsarArchive(asar_p)
    nodes = archive.get_all_file_nodes()
    stock_files = {rel: archive.read_file_content(nodes[rel]).decode("utf-8")
                   for rel in ("app/package.json", "app/bundle.js")}

    with pytest.raises(ValueError, match="crashAutoSubmit"):
        check_patch_outcomes(stock_files.get, manifest)

    engine = PatchEngine(manifest["patches"])
    patched = {rel: engine.apply_patches_for_path(rel, text.encode("utf-8")).decode("utf-8")
               for rel, text in stock_files.items()}
    res = check_patch_outcomes(patched.get, manifest)
    assert set(res) == {"crash_reports", "snmp_log_line"}
    assert patched["app/bundle.js"].count(HP_NEW_SNMP) >= 1


# ---------------------------------------------------------------------------
# Live: a compiled stand-in inside fake bundles. Never HP Click.

STAND_IN_C = r"""
#include <libgen.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
extern char **environ;

static void mkdirs(const char *p) {
    char buf[2048]; snprintf(buf, sizeof buf, "%s", p);
    for (char *s = buf + 1; *s; s++) if (*s == '/') { *s = 0; mkdir(buf, 0700); *s = '/'; }
    mkdir(buf, 0700);
}
static void logline(const char *name, const char *line) {
    const char *t = getenv("TMPDIR"); char dir[2048], path[2300];
    snprintf(dir, sizeof dir, "%s/HP/HP Click/logs", t ? t : "/nonexistent");
    mkdirs(dir);
    snprintf(path, sizeof path, "%s/%s", dir, name);
    FILE *f = fopen(path, "a"); if (f) { fprintf(f, "%s\n", line); fclose(f); }
}
int main(int argc, char **argv) {
    const char *mode = getenv("CG_FAKE_MODE"); if (!mode) mode = "sleep";
    const char *rec = getenv("CG_FAKE_RECORD");
    if (rec && strcmp(mode, "child") && strcmp(mode, "sleep")) {
        FILE *f = fopen(rec, "w");
        if (f) { fprintf(f, "%s\n%s\n", argc > 1 ? argv[1] : "", getenv("TMPDIR") ? getenv("TMPDIR") : ""); fclose(f); }
    }
    if (!strcmp(mode, "ok")) {
        char buf[2048]; snprintf(buf, sizeof buf, "%s", argv[0]);
        char helper[2300]; snprintf(helper, sizeof helper, "%s/JDFPrintProcessor", dirname(buf));
        setenv("CG_FAKE_MODE", "child", 1);
        pid_t pid = 0; char *cargv[] = {helper, NULL};
        int rc = posix_spawn(&pid, helper, NULL, NULL, cargv, environ);
        if (rec) { FILE *f = fopen(rec, "a"); if (f) { fprintf(f, "%d %d\n", rc, (int)pid); fclose(f); } }
        logline("HP Click.log", "DJRIP_DJCS: successful initialization =  \"1\"");
    } else if (!strcmp(mode, "syntax")) {
        logline("HP Click App.main.log", "debug: console-message: Uncaught SyntaxError: Unexpected token export");
    } else if (!strcmp(mode, "exit")) {
        fprintf(stderr, "stand-in: could not start\n"); return 3;
    } else if (!strcmp(mode, "stubborn")) {
        signal(SIGTERM, SIG_IGN);
    }
    sleep(60);
    return 0;
}
"""


@pytest.fixture(scope="module")
def stand_in():
    clang = shutil.which("clang")
    if not clang:
        pytest.skip("clang not available to build the stand-in process")
    d = tempfile.mkdtemp(prefix="cg-standin-")
    src = os.path.join(d, "standin.c")
    exe = os.path.join(d, "standin")
    with open(src, "w") as f:
        f.write(STAND_IN_C)
    r = subprocess.run([clang, "-O0", "-o", exe, src], capture_output=True, text=True)
    if r.returncode != 0:
        shutil.rmtree(d, ignore_errors=True)
        pytest.skip("could not compile the stand-in: " + r.stderr)
    yield exe
    shutil.rmtree(d, ignore_errors=True)


def _fake_bundle(root, name, stand_in):
    app = os.path.join(root, name)
    macos = os.path.join(app, "Contents", "MacOS")
    os.makedirs(macos)
    for exe in ("HPClickExe", "JDFPrintProcessor"):
        shutil.copy2(stand_in, os.path.join(macos, exe))
    return app


def _start(app, mode):
    env = dict(os.environ, CG_FAKE_MODE=mode)
    return subprocess.Popen([os.path.join(app, "Contents", "MacOS", "HPClickExe")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _stop(*procs):
    for p in procs:
        if p.poll() is None:
            p.kill()
        p.wait(timeout=5)


@pytest.fixture
def bundles(tmp_path, stand_in):
    root = str(tmp_path)
    target = _fake_bundle(root, "HP Click (Apple Silicon).app", stand_in)
    sibling = _fake_bundle(root, "HP Click (Apple Silicon) 2.app", stand_in)
    yield target, sibling
    # Belt and braces: nothing of ours may outlive the test.
    for app in (target, sibling):
        for pid, _ in processes_inside(app):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def test_live_kill_spares_the_sibling(bundles):
    target, sibling = bundles
    mine, theirs = _start(target, "sleep"), _start(sibling, "sleep")
    try:
        time.sleep(0.3)
        assert [p for p, _ in processes_inside(target)] == [mine.pid]
        assert kill_hpclick_processes(target, timeout=5) is True
        mine.wait(timeout=5)
        assert theirs.poll() is None, "a process outside the target was stopped"
    finally:
        _stop(mine, theirs)


def test_live_kill_escalates_for_a_process_that_ignores_sigterm(bundles):
    target, _sibling = bundles
    p = _start(target, "stubborn")
    try:
        time.sleep(0.3)
        assert kill_hpclick_processes(target, timeout=2) is True
        assert p.wait(timeout=5) == -signal.SIGKILL
    finally:
        _stop(p)


def _hp_log_snapshot():
    d = os.path.join(tempfile.gettempdir(), "HP", "HP Click", "logs")
    try:
        return {n: os.stat(os.path.join(d, n)).st_size for n in os.listdir(d)}
    except OSError:
        return {}


def test_live_smoke_launch_is_private_and_stops_only_its_own(bundles, tmp_path, monkeypatch):
    target, sibling = bundles
    record = str(tmp_path / "record.txt")
    # Already open before the launch: the user's, and must survive it.
    users_own = _start(target, "sleep")
    theirs = _start(sibling, "sleep")
    hp_logs_before = _hp_log_snapshot()
    try:
        time.sleep(0.3)
        monkeypatch.setenv("CG_FAKE_MODE", "ok")
        monkeypatch.setenv("CG_FAKE_RECORD", record)
        res = smoke_launch(target, {"required_dylibs": []}, timeout_s=20, grace_s=0.5, poll_s=0.1)
        assert res["ok"], res
        assert res["message"].startswith("PASSED")
        assert res["cleanup"] is None

        with open(record) as f:
            udd_arg, tmpdir, spawned = f.read().splitlines()[:3]
        rc, helper_pid = map(int, spawned.split())
        assert rc == 0 and helper_pid > 0, "the stand-in did not start its JDFPrintProcessor"
        with pytest.raises(ProcessLookupError):
            os.kill(helper_pid, 0)      # the orphaned helper was stopped too
        assert tmpdir.startswith(verify.SMOKE_TMP_BASE + "/cg-smoke-")
        assert udd_arg == f"--user-data-dir={tmpdir.rstrip('/')}/user-data"
        assert not os.path.exists(tmpdir), "the private temp folder was not removed"

        # Its own processes, the spawned JDFPrintProcessor included, are gone;
        # the user's and the sibling's are not.
        left = {p for p, _ in processes_inside(target)}
        assert left == {users_own.pid}
        assert users_own.poll() is None and theirs.poll() is None

        # HP's own logs: nothing removed, nothing shortened.
        after = _hp_log_snapshot()
        for name, size in hp_logs_before.items():
            assert name in after and after[name] >= size, f"HP log {name} was removed or shortened"
    finally:
        _stop(users_own, theirs)


def test_live_smoke_launch_spares_a_copy_opened_while_it_runs(bundles, monkeypatch):
    """The one the pid snapshot could not do. The owner opens the copy after the
    launch has started -- realistic, because the wizard has just put it in
    /Applications -- and it must still be running afterwards."""
    target, _sibling = bundles
    late = []
    timer = threading.Timer(0.4, lambda: late.append(_start(target, "sleep")))
    monkeypatch.setenv("CG_FAKE_MODE", "ok")
    timer.start()
    try:
        res = smoke_launch(target, {"required_dylibs": []}, timeout_s=20, grace_s=1.5, poll_s=0.1)
        timer.join()
        assert res["ok"], res
        assert late, "the stand-in for the owner's copy never started"
        theirs = late[0]
        assert theirs.poll() is None, "a copy opened during the launch was stopped"
        # Theirs is all that is left: the launch's own processes are gone.
        assert [p for p, _ in processes_inside(target)] == [theirs.pid]
    finally:
        timer.cancel()
        _stop(*late)


def test_live_smoke_launch_refuses_a_path_that_is_not_an_app(tmp_path):
    """Before the temporary folder is made, so nothing is left behind and the
    bundle is not left unsealed halfway through verification."""
    before = set(os.listdir(verify.SMOKE_TMP_BASE))
    with pytest.raises(ValueError, match="Not an app bundle"):
        smoke_launch(str(tmp_path / "output-folder"), {})
    new = {n for n in set(os.listdir(verify.SMOKE_TMP_BASE)) - before if n.startswith("cg-smoke-")}
    assert new == set(), f"a temporary folder was left behind: {new}"


# The build's own refusal to delete a running copy lives here because this is
# where the stand-in bundles are. build.py's last step deletes whatever is at
# the output path; agent.py's check runs a minute earlier, so this is the one
# that catches a copy opened while the build ran.

def test_live_build_refuses_to_delete_a_running_copy(bundles):
    from clickgraft.build import OutputInUseError, refuse_if_open
    target, _sibling = bundles
    theirs = _start(target, "sleep")
    try:
        time.sleep(0.3)
        with pytest.raises(OutputInUseError) as e:
            refuse_if_open(target)
        assert str(theirs.pid) in str(e.value)
        assert "nothing has been replaced" in str(e.value)
        assert e.value.pids == [theirs.pid] and e.value.output == target
        assert theirs.poll() is None, "the build must not stop it either"
    finally:
        _stop(theirs)
    # Nothing running from it: the build goes ahead.
    assert refuse_if_open(target) is None


def test_build_ignores_an_output_path_that_cannot_hold_a_process(tmp_path):
    from clickgraft.build import refuse_if_open
    # --out takes any path; processes_inside refuses a non-.app one rather than
    # match on a prefix, and that must not stop the build.
    assert refuse_if_open(str(tmp_path / "output-folder")) is None


def test_live_smoke_launch_fails_on_an_error_signature(bundles, monkeypatch):
    target, _sibling = bundles
    monkeypatch.setenv("CG_FAKE_MODE", "syntax")
    res = smoke_launch(target, {}, timeout_s=20, grace_s=0.5, poll_s=0.1)
    try:
        assert not res["ok"]
        assert "SyntaxError" in res["message"] and "HP Click App.main.log" in res["message"]
        assert processes_inside(target) == []
        kept = res["message"].rsplit("logs are in ", 1)[1].rstrip(".")
        assert os.path.exists(os.path.join(kept, "HP", "HP Click", "logs", "HP Click App.main.log"))
        assert not os.path.exists(os.path.join(kept, "user-data"))
    finally:
        kept = res["message"].rsplit("logs are in ", 1)[-1].rstrip(".")
        if kept.startswith(verify.SMOKE_TMP_BASE + "/cg-smoke-"):
            shutil.rmtree(kept, ignore_errors=True)


def test_live_smoke_launch_fails_fast_when_the_app_quits(bundles, monkeypatch):
    target, _sibling = bundles
    monkeypatch.setenv("CG_FAKE_MODE", "exit")
    t0 = time.time()
    res = smoke_launch(target, {}, timeout_s=30, grace_s=0.5, poll_s=0.1)
    try:
        assert not res["ok"]
        assert time.time() - t0 < 10, "waited out the budget for an app that had quit"
        assert "exit code 3" in res["message"]
        assert "stand-in: could not start" in res["message"]
    finally:
        kept = res["message"].rsplit("logs are in ", 1)[-1].rstrip(".")
        if kept.startswith(verify.SMOKE_TMP_BASE + "/cg-smoke-"):
            shutil.rmtree(kept, ignore_errors=True)


def test_live_smoke_launch_times_out_without_a_milestone(bundles, monkeypatch):
    target, _sibling = bundles
    monkeypatch.setenv("CG_FAKE_MODE", "quiet")
    res = smoke_launch(target, {}, timeout_s=1.5, grace_s=0.5, poll_s=0.1)
    try:
        assert not res["ok"]
        assert "did not report successful initialization" in res["message"]
        assert processes_inside(target) == []
    finally:
        kept = res["message"].rsplit("logs are in ", 1)[-1].rstrip(".")
        if kept.startswith(verify.SMOKE_TMP_BASE + "/cg-smoke-"):
            shutil.rmtree(kept, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
