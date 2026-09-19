"""
clickgraft.verify — Automated verification suite for built Apple Silicon HP Click bundles.
Audits architectures, rpaths, hardcoded paths, code signatures, ASAR integrity, and smoke-launch logs.
Target: Python 3.9+ (Standard Library only)
"""

import hashlib
import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import tempfile
import time
from clickgraft.asar import AsarArchive
from clickgraft.macho import get_archs, get_load_dylibs, is_macho


def _bundle_prefixes(app_path):
    """The path prefixes a process's command line must start with to be inside
    app_path: as given, and with symlinks resolved (/tmp is /private/tmp)."""
    path = os.path.abspath(app_path)
    # A prefix of "/" would select every process on the Mac. Only an .app
    # bundle is ever a target, so refuse anything else rather than guess.
    if not os.path.basename(path).endswith(".app"):
        raise ValueError(f"Not an app bundle, so no processes can be matched to it: {app_path}")
    return tuple(sorted({path + "/", os.path.realpath(path) + "/"}))


def _command_inside(command, prefixes, real_bundle):
    """True when the executable at the start of command lies inside the bundle."""
    if command.startswith(prefixes):
        return True
    # The same folder spelt another way. Seen 19 Sep 2026: a copy started as
    # /private/tmp/.../X.app/Contents/MacOS/HPClickExe ran its
    # chrome_crashpad_handler as /tmp/.../X.app/..., which no prefix of the
    # path as given matches. So resolve the command's leading path up to each
    # "<bundle name>/" in it and compare that. Only a clean absolute path
    # counts (no "..", ".", "//"), so arguments cannot walk back into the bundle.
    name = os.path.basename(real_bundle) + "/"
    at = command.find(name)
    while at != -1:
        lead = command[:at + len(name) - 1]
        if (lead.startswith("/") and os.path.normpath(lead) == lead
                and os.path.realpath(lead) == real_bundle):
            return True
        at = command.find(name, at + 1)
    return False


def processes_inside(app_path, ps_output=None):
    """[(pid, command)] for every process whose executable lies inside app_path.

    A plain string prefix on `ps -axww -o pid=,command=`: the command starts
    with the executable's path, so ".../X.app/Contents/MacOS/HPClickExe"
    matches X.app while "/Applications/HP Click.app/..." and a sibling
    ".../X 2.app/..." do not. No regex, because bundle names are full of
    metacharacters -- "(Apple Silicon)" is a group to pgrep -- and no
    splitting on spaces, because the names are full of those too. Our own
    process is never included: `clickgraft verify --app <path>` has the path
    in its arguments, and pkill -f used to match it.

    ps_output is for tests; left out, the live process table is read.
    """
    prefixes = _bundle_prefixes(app_path)
    real_bundle = os.path.realpath(os.path.abspath(app_path))
    if ps_output is None:
        ps_output = subprocess.run(["ps", "-axww", "-o", "pid=,command="],
                                   capture_output=True, text=True, errors="replace").stdout
    me = os.getpid()
    found = []
    for line in ps_output.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid, command = int(parts[0]), parts[1]
        if pid != me and _command_inside(command, prefixes, real_bundle):
            found.append((pid, command))
    return found


def _ps_table():
    """`ps -o pid=,ppid=,command=` text, with each process's environment
    appended to its command line where the kernel will show it.

    -E is what makes a process recognisable by the environment it was started
    with. macOS hides the environment of restricted binaries -- measured
    20 Sep 2026: /bin/sleep and /bin/zsh show none, a binary compiled into
    /private/tmp shows all of it -- so it is one way to match a process and
    never the only one. Falls back to the plain table if -E is refused.

    This table holds the environment of every process this account is running.
    Nothing from it is logged or reported: the only text matched against is the
    launch's own folder name, and everything in verify.py uses the pids alone.
    """
    for args in (["ps", "-axwwE", "-o", "pid=,ppid=,command="],
                 ["ps", "-axww", "-o", "pid=,ppid=,command="]):
        r = subprocess.run(args, capture_output=True, text=True, errors="replace")
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout
    return ""


def launched_processes(app_path, mark, root_pid=None, ps_output=None):
    """[(pid, command)] for the processes ONE launch of app_path started: those
    inside the bundle that carry that launch's mark, or descend from the
    process it started.

    "Inside the bundle, and not running before we started" is a different set,
    and the difference belongs to the user. The wizard puts the new copy in
    /Applications at 98% and the smoke launch can run for 90s after that, so
    the owner opening the copy in that window is ordinary; so is their
    already-open copy spawning a renderer or a JDFPrintProcessor of its own.
    A snapshot of pids taken before the launch counts every one of those as
    ours. Everything selected here carries something only this launch has.

    `mark` is the basename of the launch's private temporary folder. It is in
    --user-data-dir= and crashpad's --database= on the Chromium side, in
    JDFPrintProcessor's -tmpdir, and in TMPDIR in the environment of anything
    started from the copy. The basename and not the whole path, because a
    child can spell /private/tmp as /tmp -- seen 19 Sep 2026 doing exactly
    that with the bundle path.

    root_pid picks up a process that carries no mark, and the mark picks up
    one that outlived root_pid and was reparented to launchd, which HP's
    JDFPrintProcessor does. Anything neither test recognises is left running:
    a stray process of ours costs less than stopping one of the owner's.

    ps_output is for tests: `ps -axwwE -o pid=,ppid=,command=` text.
    """
    prefixes = _bundle_prefixes(app_path)
    real_bundle = os.path.realpath(os.path.abspath(app_path))
    if ps_output is None:
        ps_output = _ps_table()
    rows = []
    for line in ps_output.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        rows.append((int(parts[0]), int(parts[1]), parts[2]))
    parent = {pid: ppid for pid, ppid, _cmd in rows}

    def descends_from_root(pid):
        seen = set()
        while pid and pid not in seen:
            if pid == root_pid:
                return True
            seen.add(pid)
            pid = parent.get(pid)
        return False

    me = os.getpid()
    found = []
    for pid, _ppid, command in rows:
        if pid == me or not _command_inside(command, prefixes, real_bundle):
            continue
        if (mark and mark in command) or (root_pid and descends_from_root(pid)):
            found.append((pid, command))
    return found


def kill_hpclick_processes(target_app_path=None, timeout=15.0, mark=None, root_pid=None):
    """Stop processes running from inside target_app_path, and WAIT for them to
    actually exit. Nothing outside that bundle is ever signalled.

    Until 1.5.8 this ran `pkill -x HPClickExe`, `pkill -f "HP Click Helper"`
    and `pkill -x JDFPrintProcessor`: every HP Click on the Mac, the user's
    own stock copy included, even mid-print, and the wizard's verify step ran
    it after every build. ClickGraft may only stop what it started. With no
    target there is nothing it may stop, so this returns at once.

    With `mark` or `root_pid`, only that launch's own processes are signalled
    (launched_processes). Without them every process inside the bundle is, so
    only a caller that owns the copy outright may leave them out.

    Firing a signal and returning immediately is not enough. The main process
    spawns JDFPrintProcessor asynchronously, so a single sweep races with it:
    the sweep can complete, JDFPrintProcessor can spawn a moment later, and it
    then outlives everything as an orphan -- observed surviving after its own
    app bundle had been deleted, still holding a Qt local socket. So this
    sweeps repeatedly until nothing matches, escalating to SIGKILL, and callers
    that go on to delete the bundle are not pulling it out from under a live
    process.

    Returns True once nothing is left, False if something outlived timeout.
    """
    if not target_app_path:
        return True

    def victims_now():
        if mark or root_pid:
            return [pid for pid, _cmd in launched_processes(target_app_path, mark, root_pid)]
        return [pid for pid, _cmd in processes_inside(target_app_path)]

    deadline = time.time() + timeout
    sig = signal.SIGTERM
    while True:
        victims = victims_now()
        if not victims:
            return True
        if time.time() > deadline:
            return False
        if time.time() > deadline - timeout / 2:
            sig = signal.SIGKILL
        for pid in victims:
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(0.4)


class LogTail:
    """What is appended to one file after this object is made. Read-only.

    The smoke launch used to delete HP's "HP Click App.main.log" and
    "HP Click.log" so that whatever it read afterwards was its own. Those are
    the user's logs, and HP's support asks for them. This only ever opens the
    file for reading, from where it ended when the tail was made.

    If the file is replaced or shrinks, reading restarts at the top of the new
    one: HP's logger-service.js renames the current log to a timestamped name
    at startup (renameLogs) and starts a fresh file.
    """

    def __init__(self, path):
        self.path = path
        self.text = ""
        st = self._stat()
        self._ino = st.st_ino if st else None
        self._pos = st.st_size if st else 0

    def _stat(self):
        try:
            return os.stat(self.path)
        except OSError:
            return None

    def read_new(self):
        """Read what has been appended since the last call; also kept in .text."""
        st = self._stat()
        if st is None:
            return ""
        if st.st_ino != self._ino or st.st_size < self._pos:
            self._ino, self._pos = st.st_ino, 0
        if st.st_size <= self._pos:
            return ""
        try:
            with open(self.path, "rb") as f:
                f.seek(self._pos)
                data = f.read()
        except OSError:
            return ""
        self._pos += len(data)
        chunk = data.decode("utf-8", "ignore")
        self.text += chunk
        return chunk


# Built-copy outcomes. The build applies the manifest's ops; these check that
# the result is what the ops were for, because an op can apply cleanly to a key
# nothing reads. That is not hypothetical: every release before 1.5.8 set
# crashAutoSubmit in the ROOT package.json, whose copy of the key HP's crash
# reporter never reads, the build succeeded, and every copy went on trying to
# upload crash dumps, because app/main.js require()s app/package.json (found
# 19 Sep 2026: a 4.8.117 copy logged "auto-submit: true", and both its Crashpad
# dumps were marked upload_count: 1, uploaded: 0 -- an attempt each, not a
# confirmed upload).
CRASH_PACKAGE_JSON = "app/package.json"

# Present in HP's SNMPv3 credential log line in 4.8.x and 4.10.42 bundle.js, and
# gone from 4.11.31, where HP replaced the line with one that logs no values.
SNMP_CREDENTIAL_FRAGMENT = 'authenticationPassword: "+'


def _manifest_patches_snmp_line(manifest):
    for patch in manifest.get("patches", []):
        if patch.get("path") != "app/bundle.js":
            continue
        for op in patch.get("ops", []):
            if op.get("type") == "replace" and SNMP_CREDENTIAL_FRAGMENT in op.get("anchor", ""):
                return True
    return False


def check_patch_outcomes(read_file, manifest):
    """Check the built asar says what the patches were for, not just that
    they applied. read_file(rel_path) returns the file's text, or None when
    the archive has no such file. Returns results entries; raises ValueError.

    Only the ops that exist for every version are assumed. The SNMPv3 line is
    checked where the manifest carries that op, and nothing here depends on
    the constants.js/industries.js ops, which 4.10.42 no longer has.
    """
    results = {}

    pkg_text = read_file(CRASH_PACKAGE_JSON)
    if pkg_text is None:
        raise ValueError(
            f"{CRASH_PACKAGE_JSON} is missing from the built asar. HP's crash "
            f"reporter reads crashAutoSubmit from it, so this build cannot be "
            f"shown to have crash uploads off.")
    try:
        pkg = json.loads(pkg_text)
    except ValueError as exc:
        raise ValueError(f"{CRASH_PACKAGE_JSON} in the built asar is not valid JSON: {exc}") from None
    hp_configs = pkg.get("hp_configs") if isinstance(pkg, dict) else None
    if not isinstance(hp_configs, dict) or "crashAutoSubmit" not in hp_configs:
        found = "no hp_configs.crashAutoSubmit at all"
    else:
        found = f"hp_configs.crashAutoSubmit = {json.dumps(hp_configs['crashAutoSubmit'])}"
    if not (isinstance(hp_configs, dict) and hp_configs.get("crashAutoSubmit") is False):
        raise ValueError(
            f"Crash reports are still set to upload: {CRASH_PACKAGE_JSON} has "
            f"{found}, not false. app/main.js passes that value to "
            f"crashReporter.start as uploadToServer, so this copy would try to "
            f"upload crash dumps to HP's server over plain HTTP.")
    results["crash_reports"] = (
        f"PASSED ({CRASH_PACKAGE_JSON} has hp_configs.crashAutoSubmit false, "
        f"so HP Click's crash reporter starts with uploads off)")

    if _manifest_patches_snmp_line(manifest):
        bundle_text = read_file("app/bundle.js")
        if bundle_text is None:
            raise ValueError("app/bundle.js is missing from the built asar, so the "
                             "SNMPv3 log-line fix cannot be checked.")
        left = bundle_text.count(SNMP_CREDENTIAL_FRAGMENT)
        if left:
            raise ValueError(
                f"app/bundle.js still contains HP's SNMPv3 credential log line "
                f"({left} occurrence(s) of '{SNMP_CREDENTIAL_FRAGMENT}'), which "
                f"writes the user name and both passwords into HP Click's log.")
        results["snmp_log_line"] = (
            "PASSED (app/bundle.js no longer has the line that wrote SNMPv3 "
            "passwords to the log)")

    return results


# The smoke launch's private temporary folder goes here, not under $TMPDIR.
# The copy runs with TMPDIR pointing into it, and HP's native side talks over
# Qt local sockets (RPCLocalServer in DjCoreServicesNative), which are Unix
# sockets, whose paths macOS caps at 104 bytes; /var/folders/<..>/T/ alone is
# 49 of them. Whether those sockets land under TMPDIR is inferred, not seen,
# so keep the prefix short rather than find out from a failed launch.
SMOKE_TMP_BASE = "/private/tmp"

# The two logs the milestone and the error signatures have always been read
# from, now in the launch's own folder rather than HP's.
SMOKE_LOG_NAMES = ("HP Click App.main.log", "HP Click.log")
SMOKE_MILESTONES = ("successful initialization", "DjCoreServices initialized successfully")
SMOKE_FAILURE_SIGNATURES = ("SyntaxError", "Library not loaded", "Symbol not found", "Uncaught Exception")


def _first_line_with(text, needle, limit=240):
    for line in text.splitlines():
        if needle in line:
            line = " ".join(line.split())
            return line if len(line) <= limit else line[:limit - 1] + "…"
    return ""


def smoke_launch(target_app_path, manifest, timeout_s=90.0, grace_s=3.0, poll_s=0.5):
    """Start the built copy once, privately, and watch it initialise.

    Returns {"ok": bool, "message": str, "cleanup": str or None}. It does not
    raise for a failed launch, so the caller can re-seal the bundle first.

    Private means three things, and each one fixes something the old launch
    did to a Mac where HP Click was already open:

    - Its own --user-data-dir. app/main.js calls requestSingleInstanceLock()
      before anything else and exits if another HP Click holds it, and the
      lock is per user-data dir. The old launch cleared the lock by killing
      every HP Click on the Mac. (19 Sep 2026: a 4.11.31 copy started fully
      with --user-data-dir=<tmp> while stock 4.10.42 was running.)
    - Its own TMPDIR, which is also how its processes are recognised. HP's
      logs and its .logging.lock live under
      os.tmpdir() (logger-service.js), and the native side is handed the
      same folder as appTemp, so a second HP Click sharing it writes into
      the first one's logs. Seen 19 Sep 2026: with stock 4.10.42 open since
      09:37, a test copy launched at 09:41 took over .logging.lock, appended
      its lines to the stock app's "HP Click App.main.log", and
      "HP Click.log" was left holding only the copy's lines. With its own
      TMPDIR every line in these logs is the copy's, so there is nothing to
      filter by pid (the main log's lines carry none), and HP's own logs are
      never opened. Checked 19 Sep 2026 on a 4.8.117 copy: the main,
      renderer, initDjCoreServices and DjRipJDFPrintProcessorApp logs all
      landed in the private folder, and HP's log folder was unchanged down
      to inode, size and mtime.
    - It stops only what it started: processes inside the copy that carry
      this launch's own folder name, or that descend from the process it
      started (launched_processes). Anything else inside the copy is the
      owner's, whether it was running before the launch or opened during it.

    The copy's stdout and stderr are captured too. They carry the same
    DJRIP/JAVASCRIPT_DJCS lines as "HP Click.log", plus anything dyld prints
    when a library fails to load.
    """
    # First, because everything below depends on it and it costs nothing:
    # a path that is not an .app has no process of its own to recognise, and
    # this used to raise from the middle of the launch, after the temporary
    # folder was made and with the bundle left unsealed.
    _bundle_prefixes(target_app_path)

    exe_path = os.path.join(target_app_path, "Contents", "MacOS", "HPClickExe")
    lib_dir = os.path.join(target_app_path, "Contents", "Resources", "app", "appData", "macx", "lib")
    fw_dir = os.path.join(target_app_path, "Contents", "Resources", "app", "appData", "macx", "Frameworks")

    smoke_dir = tempfile.mkdtemp(prefix="cg-smoke-", dir=SMOKE_TMP_BASE)
    # What tells this launch's processes apart from the owner's. Unique: only
    # this folder is called this.
    mark = os.path.basename(smoke_dir)
    user_data = os.path.join(smoke_dir, "user-data")
    log_dir = os.path.join(smoke_dir, "HP", "HP Click", "logs")
    out_path = os.path.join(smoke_dir, "stdout-stderr.log")

    env = os.environ.copy()
    env["DYLD_FRAMEWORK_PATH"] = fw_dir
    env["DYLD_LIBRARY_PATH"] = lib_dir
    env["TMPDIR"] = smoke_dir + "/"

    preload_dylibs = []
    for dinfo in manifest.get("required_dylibs", []):
        if dinfo.get("preload") is True:
            preload_dylibs.append(os.path.join(lib_dir, dinfo["name"]))
    if preload_dylibs:
        env["DYLD_INSERT_LIBRARIES"] = ":".join(preload_dylibs)

    tails = [(name, LogTail(os.path.join(log_dir, name))) for name in SMOKE_LOG_NAMES]
    tails.append(("stdout/stderr", LogTail(out_path)))

    ok = False
    message = ""
    cleanup = None
    proc = None
    try:
        with open(out_path, "wb") as out_f:
            proc = subprocess.Popen([exe_path, f"--user-data-dir={user_data}"], env=env,
                                    stdin=subprocess.DEVNULL, stdout=out_f, stderr=subprocess.STDOUT)

        start_t = time.time()
        initialized = False
        reached_at = None
        failures = []            # (signature, source, first line)
        gone_at = None

        # 90s, not 12s. The milestone is a renderer-side event, and the FIRST launch
        # after a fresh HP Click install does first-run work — building its profile,
        # cold caches, no printer configured yet — that a warmed-up launch does not.
        # Measured on an M5 Max: ~8s warm, over 12s cold. A budget that only fits the
        # warm case fails the one launch every new user makes, and reports a working
        # build as broken. With a fresh user-data dir every smoke launch is now that
        # first launch.
        while time.time() - start_t < timeout_s:
            time.sleep(poll_s)

            # A copy that has quit, leaving nothing of it running, will not
            # reach the milestone later, and waiting out the budget only
            # delayed the same verdict. Decided before the read below, so that
            # read sees everything it wrote. (Not only proc: an app that
            # relaunches itself hands over to a new process from the bundle.)
            gone = (proc.poll() is not None
                    and not launched_processes(target_app_path, mark, proc.pid))

            for _name, tail in tails:
                tail.read_new()

            if not initialized and any(m in t.text for _n, t in tails for m in SMOKE_MILESTONES):
                initialized = True
                reached_at = time.time() - start_t

            for fail_sig in SMOKE_FAILURE_SIGNATURES:
                if any(f[0] == fail_sig for f in failures):
                    continue
                for name, tail in tails:
                    if fail_sig in tail.text:
                        failures.append((fail_sig, name, _first_line_with(tail.text, fail_sig)))
                        break

            # Stop as soon as there is an answer. The old loop only broke when it had
            # BOTH a milestone and an error, so a clean run always burned the whole
            # budget and a broken one waited for a success that never came.
            if failures:
                break
            # Keep reading for grace_s after the milestone, to catch late errors.
            if initialized and (time.time() - start_t) - reached_at >= grace_s:
                break
            if gone and not initialized:
                gone_at = time.time() - start_t
                break

        if failures:
            detail = "; ".join(f"{sig} in {src}: {line}" if line else f"{sig} in {src}"
                               for sig, src, line in failures)
            message = (f"Smoke launch FAILED with error signatures in log: "
                       f"{[f[0] for f in failures]}. First seen: {detail}. "
                       f"The launch's own logs are in {smoke_dir}.")
        elif not initialized:
            if gone_at is not None:
                last = [ln for ln in tails[-1][1].text.splitlines() if ln.strip()]
                how = (f"was killed by signal {-proc.returncode}" if proc.returncode < 0
                       else f"quit on its own with exit code {proc.returncode}")
                message = (
                    f"Smoke launch FAILED: the app {how} after {gone_at:.1f}s, "
                    f"without reporting successful initialization."
                    + (f" Its last output was: {' '.join(last[-1].split())[:240]}." if last else "")
                    + f" The launch's own logs are in {smoke_dir}.")
            else:
                message = (
                    f"Smoke launch FAILED: the app did not report successful initialization "
                    f"within {timeout_s:.0f}s. The build itself completed; this is the "
                    f"post-build check. The launch's own logs are in {smoke_dir}.")
        else:
            ok = True
            message = f"PASSED (Initialization milestone reached in {reached_at:.2f}s)"
    finally:
        root_pid = proc.pid if proc is not None else None
        stopped = kill_hpclick_processes(target_app_path, mark=mark, root_pid=root_pid)
        if proc is not None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if not stopped:
            left = [p for p, _c in launched_processes(target_app_path, mark, root_pid)]
            cleanup = (f"WARNING: {len(left)} process(es) from the test launch did not "
                       f"exit within 15s (pids {left}); its temporary folder {smoke_dir} "
                       f"was left in place")
        elif ok or proc is None:
            shutil.rmtree(smoke_dir, ignore_errors=True)
        else:
            # Keep the logs for whoever reads the failure; the profile is noise.
            shutil.rmtree(user_data, ignore_errors=True)

    return {"ok": ok, "message": message, "cleanup": cleanup}


def verify_app_bundle(target_app_path, manifest=None):
    """
    Runs complete verification suite against target_app_path.
    Returns (True, details_dict) on success, or raises ValueError on failure.
    """
    target_app_path = os.path.abspath(target_app_path)
    if not os.path.exists(target_app_path):
        raise ValueError(f"Target app path does not exist: {target_app_path}")

    asar_p = os.path.join(target_app_path, "Contents", "Resources", "app.asar")
    if not os.path.exists(asar_p):
        raise ValueError(f"Target app does not contain Resources/app.asar: {target_app_path}")

    # Identify the manifest for this bundle.
    #
    # NOT by asar SHA-256: the manifest's asar_sha256 fingerprints the *stock
    # source*, and by definition a built bundle's asar has been patched, so it
    # can never match. Looking up on it made `verify --app <built bundle>` --
    # the documented workflow -- fail 100% of the time.
    #
    # Identify by app version read from the target's own asar instead, which is
    # stable across patching. Accept the stock hash too, so verifying an
    # unmodified source bundle still works.
    if manifest is None:
        from clickgraft.manifest import ManifestManager
        mm = ManifestManager()
        archive_for_id = AsarArchive(asar_p)

        app_version = None
        try:
            pkg_node = archive_for_id.get_all_file_nodes().get("package.json")
            if pkg_node is not None:
                pkg = json.loads(archive_for_id.read_file_content(pkg_node).decode("utf-8"))
                app_version = pkg.get("version")
        except Exception:
            app_version = None

        manifest = mm.find_manifest(app_version=app_version) if app_version else None

        if not manifest:
            with open(asar_p, "rb") as f:
                asar_disk_sha256 = hashlib.sha256(f.read()).hexdigest()
            manifest = mm.find_manifest(asar_sha256=asar_disk_sha256)

        if not manifest:
            raise ValueError(
                f"No manifest matches this bundle (app version "
                f"{app_version or 'unreadable'}). Supported versions: "
                f"{', '.join(sorted(mm.manifests)) or 'none loaded'}."
            )

    results = {}

    # 1. Mach-O Architectures Check
    main_exe = os.path.join(target_app_path, "Contents", "MacOS", "HPClickExe")
    exe_archs = get_archs(main_exe)
    if "arm64" not in exe_archs:
        raise ValueError(f"Main executable {main_exe} is not native arm64: {exe_archs}")

    el_fw = os.path.join(target_app_path, "Contents", "Frameworks", "Electron Framework.framework", "Versions", "A", "Electron Framework")
    el_archs = get_archs(el_fw)
    if "arm64" not in el_archs:
        raise ValueError(f"Electron Framework binary is not native arm64: {el_archs}")

    results["architectures"] = "PASSED (Native arm64 Electron runtime)"

    # 2. Full-bundle Mach-O & RPATH Audit
    expected_x86 = set(manifest.get("expected_x86_only", []))
    x86_only_found = []
    homebrew_refs = []

    for root, dirs, files in os.walk(target_app_path):
        for f in files:
            fp = os.path.join(root, f)
            rel_p = os.path.relpath(fp, target_app_path)

            if is_macho(fp):
                archs = get_archs(fp)
                if archs == ["x86_64"]:
                    if rel_p not in expected_x86:
                        x86_only_found.append(rel_p)

                dylibs = get_load_dylibs(fp)
                for dep in dylibs:
                    if dep.startswith("/opt/homebrew") or dep.startswith("/usr/local"):
                        homebrew_refs.append((rel_p, dep))

    # Audit launcher script for hardcoded Homebrew paths
    launcher = os.path.join(target_app_path, "Contents", "MacOS", "HP Click")
    if os.path.exists(launcher):
        with open(launcher, "r", encoding="utf-8", errors="ignore") as lf:
            l_text = lf.read()
        if "/opt/homebrew" in l_text or "/usr/local" in l_text:
            homebrew_refs.append(("Contents/MacOS/HP Click", "Hardcoded Homebrew path in launcher script"))

    if x86_only_found:
        raise ValueError(f"Unexpected x86_64-only Mach-O binaries found in bundle: {x86_only_found}")

    if homebrew_refs:
        raise ValueError(f"Found hardcoded Homebrew dependency paths: {homebrew_refs}")

    results["bundle_audit"] = "PASSED (0 unexpected x86_64 binaries, 0 Homebrew path leaks)"

    # 2b. Unprovided flat-namespace symbols.
    #
    # This exists because of a real crash: importing a PNG killed the app with
    # PC=0x0 and LR inside DjCoreServicesNative. The arm64 slice referenced
    # png_init_filter_functions_neon, nothing in the bundle exported it, and the
    # call bound to null. HP never meets this because they ship an Intel Electron
    # and never load the arm64 slice -- grafting one makes that code live.
    #
    # Only FLAT-namespace undefined symbols can do this. A two-level symbol names
    # its library, so dyld fails loudly at load if it is missing; a flat one is
    # looked up across everything loaded, and resolves to null when nobody has it.
    # That distinction is the whole check: the bundle has ~1100 flat undefined
    # symbols and all but a handful are Adobe C++ resolving between its own
    # sibling dylibs, which is fine.
    #
    # Known-unprovided symbols are listed in the manifest and accepted. Anything
    # NOT on that list is a new time bomb of exactly the kind that already went
    # off once, so it fails the build rather than a customer's print job.
    accepted_missing = set(manifest.get("accepted_unprovided_symbols", []))
    flat_undef, exported = set(), set()
    for root, _dirs, files in os.walk(target_app_path):
        for f in files:
            fp = os.path.join(root, f)
            if not is_macho(fp) or "arm64" not in get_archs(fp):
                continue
            u = subprocess.run(["nm", "-m", "-arch", "arm64", "-u", fp],
                               capture_output=True, text=True)
            for line in u.stdout.splitlines():
                if "dynamically looked up" not in line:
                    continue
                parts = line.split()
                for tok in parts:
                    if tok.startswith("_") and len(tok) > 1:
                        flat_undef.add(tok[1:])
                        break
            d = subprocess.run(["nm", "-arch", "arm64", "-g", "--defined-only", fp],
                               capture_output=True, text=True)
            for line in d.stdout.splitlines():
                cols = line.split()
                if len(cols) >= 3 and cols[2].startswith("_"):
                    exported.add(cols[2][1:])

    unprovided = flat_undef - exported
    unexpected = sorted(unprovided - accepted_missing)
    if unexpected:
        raise ValueError(
            "Flat-namespace symbols that nothing in the bundle provides: "
            f"{unexpected}. These bind to NULL and crash when called -- the same "
            "failure as png_init_filter_functions_neon. Either add a shim, or "
            "record them in the manifest's accepted_unprovided_symbols with a "
            "reason if they are genuinely never reached."
        )
    results["flat_symbols"] = (
        f"PASSED ({len(flat_undef)} flat undefined, {len(unprovided)} unprovided, "
        f"all accepted)"
    )

    # 3. Code Signatures & Entitlements (D7: single call, no --deep)
    cs_res = subprocess.run(["codesign", "--verify", target_app_path], capture_output=True, text=True)
    if cs_res.returncode != 0:
        raise ValueError(f"Code signature verification FAILED for {target_app_path}\nStderr: {cs_res.stderr}")

    results["code_signature"] = "PASSED (Ad-hoc signature valid on disk)"

    # 4. ASAR Integrity & Entry Count Checks
    archive = AsarArchive(asar_p)
    packed_c, unpacked_c = archive.count_entries()

    exp_packed = manifest["asar_entries"]["packed"]
    exp_unpacked = manifest["asar_entries"]["unpacked"]
    if packed_c != exp_packed or unpacked_c != exp_unpacked:
        raise ValueError(f"ASAR entry count mismatch: {packed_c} packed, {unpacked_c} unpacked (expected {exp_packed}/{exp_unpacked})")

    main_plist_p = os.path.join(target_app_path, "Contents", "Info.plist")
    with open(main_plist_p, "rb") as pf:
        plist = plistlib.load(pf)

    header_hash = hashlib.sha256(archive.header_json_bytes).hexdigest()
    plist_hash = plist.get("ElectronAsarIntegrity", {}).get("Resources/app.asar", {}).get("hash")
    if plist_hash != header_hash:
        raise ValueError(f"Info.plist ElectronAsarIntegrity hash mismatch: plist={plist_hash} != header={header_hash}")

    results["asar_integrity"] = "PASSED (Packed/unpacked counts & Info.plist integrity hash verified)"

    # 4b. The two auto-update locks.
    #
    # Both are written by build.py and, until now, neither was ever checked
    # again. A lock that is applied and never verified is a lock you find out
    # about from a user whose patched copy was replaced by HP's Intel build.
    #
    # LOCK ONE, the JS. Every ipcMain registration in app-updater.js is made
    # from inside startup(), so `function startup(e){return;` removes the feed
    # URL, the launch check, the renderer's 24h loop handler, quitAndInstall
    # and the forced-update path in a single edit. Assert the return is there
    # AND that no registration has escaped to module scope, because the first
    # assertion only holds while HP keeps them inside the function.
    updater_path = "app/node/main/app-updater.js"
    nodes = archive.get_all_file_nodes()
    if updater_path not in nodes:
        raise ValueError(
            f"{updater_path} is missing from the built asar. The updater lock is "
            f"applied to that file; if HP has moved or renamed it the patch has "
            f"silently stopped protecting anything.")
    updater_js = archive.read_file_content(nodes[updater_path]).decode("utf-8", "ignore")

    startup_m = re.search(r"function\s+startup\s*\([^)]*\)\s*\{", updater_js)
    if not startup_m:
        raise ValueError(
            f"No startup() function found in {updater_path}. The manifest patch "
            f"anchors on it, so this build is unprotected.")
    body_start = startup_m.end()
    if not re.match(r"\s*return\s*;", updater_js[body_start:body_start + 32]):
        raise ValueError(
            f"The auto-update lock is NOT in place: startup() in {updater_path} "
            f"does not return immediately. HP's updater would configure its feed "
            f"and could replace this patched copy with HP's Intel build.")

    escaped = [m.start() for m in re.finditer(r"ipcMain\s*\.\s*on\s*\(", updater_js)
               if m.start() < body_start]
    if escaped:
        raise ValueError(
            f"{len(escaped)} ipcMain.on registration(s) in {updater_path} sit "
            f"OUTSIDE startup(), at module scope. Returning early from startup() "
            f"does not disable those, so the update path is only partly locked. "
            f"The manifest patch needs revisiting for this version.")

    # LOCK TWO, the ShipIt stub. Only the executable is replaced; the Squirrel
    # DYLIB must survive because Electron Framework links it and the app will
    # not launch without it, so check the files individually rather than
    # asserting anything about the framework as a whole.
    squirrel_root = os.path.join(target_app_path, "Contents", "Frameworks", "Squirrel.framework")
    shipit_total = 0
    shipit_stubbed = 0
    for root, _dirs, files in os.walk(squirrel_root):
        for fn in files:
            if fn != "ShipIt":
                continue
            fp = os.path.join(root, fn)
            if os.path.islink(fp):
                continue
            shipit_total += 1
            try:
                with open(fp, "rb") as shf:
                    head = shf.read(512)
            except OSError as exc:
                raise ValueError(f"Could not read {fp} to check the ShipIt stub: {exc}") from None
            if b"Replaced by ClickGraft" in head:
                shipit_stubbed += 1
            else:
                raise ValueError(
                    f"{fp} is still HP's real ShipIt, not the ClickGraft stub. "
                    f"That binary replaces the whole .app in place, so an update "
                    f"reaching it would overwrite this patched copy.")

    results["update_locks"] = (
        f"PASSED (startup() returns before the updater is configured; "
        f"{shipit_stubbed}/{shipit_total} ShipIt stubbed)"
    )

    # 4c. What the patches were for, read back from the built asar (see
    # check_patch_outcomes). Static, so it runs on an Intel Mac too.
    def _read_built(rel_path):
        node = nodes.get(rel_path)
        if node is None:
            return None
        if node.get("unpacked") is True:
            with open(os.path.join(asar_p + ".unpacked", rel_path), "rb") as uf:
                return uf.read().decode("utf-8", "ignore")
        return archive.read_file_content(node).decode("utf-8", "ignore")

    results.update(check_patch_outcomes(_read_built, manifest))

    # 5. Automated Smoke Launch & Multi-signature Error Detection Test
    #
    # Only possible on Apple Silicon. There is no reverse Rosetta: an Intel Mac
    # cannot execute the arm64 binary we just grafted in, and Popen raises
    # OSError 86 "Bad CPU type in executable". That is not a defect in the
    # copy — a cross-build for another Mac is a supported thing to do — so it
    # is reported as not-checked rather than failed.
    from clickgraft.hostarch import is_apple_silicon
    if not is_apple_silicon():
        results["smoke_launch"] = (
            "SKIPPED (this Mac has an Intel processor and cannot run an Apple "
            "Silicon app, so the copy could not be test-launched here)")
        return True, results

    smoke = smoke_launch(target_app_path, manifest)
    if smoke["cleanup"]:
        results["smoke_cleanup"] = smoke["cleanup"]

    # Adobe's print engine writes font caches (ACRFonts/**/AdobeFnt16.lst) INSIDE
    # the bundle the first time it runs, which is what the smoke launch just did.
    # Those files are not in the signature's resource seal, so `codesign --verify`
    # now reports "a sealed resource is missing or invalid" on a bundle that was
    # valid ninety seconds ago and works perfectly. HP's own shipped app does the
    # same thing on first launch.
    #
    # Re-seal it here so the user is handed a bundle whose signature matches what
    # is actually on disk. This runs after the launch on purpose: sign first and
    # the very next run invalidates it again.
    from clickgraft.signing import sign_bundle
    try:
        sign_bundle(target_app_path)
        results["resealed"] = "PASSED (re-signed after first-run font caches were written)"
    except Exception as exc:                                       # noqa: BLE001
        results["resealed"] = f"WARNING: could not re-sign after smoke launch: {exc}"

    if not smoke["ok"]:
        raise ValueError(smoke["message"])

    results["smoke_launch"] = smoke["message"]

    return True, results
