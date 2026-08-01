#!/usr/bin/env python3
"""
A/B startup cost, v2 -- driven by the app's own log markers instead of a CPU
plateau, so both builds are compared at the SAME lifecycle stage.

v1 was wrong: it stopped each build when CPU went flat for 3s, which caught the
arm64 build after printer discovery but the Intel build while it was still in
splash. It also let the stock Intel build download HPClick-4.8.118 mid-run.

The renderer log (/tmp/HP/HP Click/logs/HP Click.log) carries millisecond
timestamps, so phase timings come from the app itself. The run ends when the app
reaches its steady no-printer UI state:

    printer-service: isPrintable - no printer or roll selected

which is the app having started, initialised DjCore, drawn its window, looked
for a printer and concluded there isn't one.
"""
import os
import re
import shutil
import subprocess
import sys
import time

LOGDIR = "/tmp/HP/HP Click/logs"
RENDER_LOG = f"{LOGDIR}/HP Click.log"
MAIN_LOG = f"{LOGDIR}/HP Click App.main.log"
TERMINAL = "isPrintable - no printer or roll selected"
CAP = 240.0
LOG_QUIET = 12.0   # log silent this long == startup work finished

PHASES = [
    ("djcore_init_start", "--> Initializing DjCore"),
    ("djcore_ready",      "successful initialization"),
    ("first_printer_svc", "printer-service:"),
    ("no_printer",        TERMINAL),
]


def pids(bundle):
    out = subprocess.run(["pgrep", "-f", bundle], capture_output=True, text=True).stdout
    return [int(x) for x in out.split()]


def sample(bundle):
    p = pids(bundle)
    if not p:
        return 0.0, 0.0
    out = subprocess.run(["ps", "-o", "rss=,cputime=", "-p", ",".join(map(str, p))],
                         capture_output=True, text=True).stdout
    cpu = rss = 0.0
    for line in out.strip().splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        rss += int(parts[0]) / 1024.0
        m = re.match(r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)$", parts[1])
        if m:
            d, h, mm, ss = m.groups()
            cpu += int(d or 0) * 86400 + int(h or 0) * 3600 + int(mm) * 60 + float(ss)
    return cpu, rss


def kill_all(bundle):
    # Both builds share bundle id com.hp.hpclick, so they share Electron's
    # single-instance lock: a surviving process from EITHER build makes the
    # next launch hit requestSingleInstanceLock() == false and exit(0)
    # silently. Sweep every HP Click process, not just this bundle's.
    for pat in ("HPClickExe", "HP Click Helper", "JDFPrintProcessor",
                "chrome_crashpad_handler", bundle):
        subprocess.run(["pkill", "-f", pat], capture_output=True)
    for _ in range(40):
        if not (pids(bundle) or pids("HPClickExe")):
            break
        time.sleep(0.25)
    else:
        for pat in ("HPClickExe", bundle):
            subprocess.run(["pkill", "-9", "-f", pat], capture_output=True)
    time.sleep(3.0)   # let the single-instance lock actually release


def newest_ts(blob, wall0):
    """Elapsed seconds to the most recent log line written by this run."""
    best = None
    for line in reversed(blob.splitlines()):
        m = LINE_TS.match(line)
        if not m:
            continue
        try:
            ts = time.mktime(time.strptime(f"{m.group(1)} {m.group(2)[:8]}",
                                           "%Y-%m-%d %H:%M:%S")) + float(m.group(2)[8:])
        except ValueError:
            continue
        if ts >= wall0 - 0.5:
            best = ts - wall0
        break
    return best


LINE_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}), (\d{2}:\d{2}:\d{2}\.\d{3}), ")


def parse_marks(blob, wall0):
    """Elapsed seconds from launch to each phase, from the log's own stamps.

    Lines older than wall0 belong to an earlier run and are skipped.
    """
    marks = {}
    for line in blob.splitlines():
        m = LINE_TS.match(line)
        if not m:
            continue
        try:
            ts = time.mktime(time.strptime(f"{m.group(1)} {m.group(2)[:8]}",
                                           "%Y-%m-%d %H:%M:%S"))
            ts += float(m.group(2)[8:])
        except ValueError:
            continue
        if ts < wall0 - 0.5:
            continue
        for name, needle in PHASES:
            if name not in marks and needle in line:
                marks[name] = ts - wall0
    return marks


def read(path):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def run_once(bundle, insert_libs, label):
    kill_all(bundle)
    for f in (RENDER_LOG, MAIN_LOG):
        try:
            os.remove(f)
        except OSError:
            pass

    res = f"{bundle}/Contents/Resources/app/appData/macx"
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": os.path.expanduser("~"),
           "DYLD_FRAMEWORK_PATH": f"{res}/Frameworks",
           "DYLD_LIBRARY_PATH": f"{res}/lib"}
    if insert_libs:
        env["DYLD_INSERT_LIBRARIES"] = (
            f"{res}/lib/libunistring.5.dylib:{res}/lib/libidn2.0.dylib")

    t0, wall0 = time.monotonic(), time.time()
    proc = subprocess.Popen([f"{bundle}/Contents/MacOS/HPClickExe"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    marks, peak_rss = {}, 0.0
    last_line_at = None          # elapsed at the newest log line seen
    last_change = time.monotonic()
    prev_len = -1
    while time.monotonic() - t0 < CAP:
        blob = read(RENDER_LOG)
        if len(blob) != prev_len:
            prev_len = len(blob)
            last_change = time.monotonic()
            marks = parse_marks(blob, wall0)
            if marks:
                last_line_at = max(marks.values())
            tail = newest_ts(blob, wall0)
            if tail is not None:
                last_line_at = tail
        _, rss = sample(bundle)
        peak_rss = max(peak_rss, rss)
        if last_line_at is not None and time.monotonic() - last_change > LOG_QUIET:
            break
        time.sleep(0.25)

    total = time.monotonic() - t0
    cpu, _ = sample(bundle)
    marks = parse_marks(read(RENDER_LOG), wall0)
    updated = "update-downloaded" in read(MAIN_LOG)
    kill_all(bundle)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    r = {"marks": marks, "cpu_s": cpu, "rss_mb": peak_rss, "quiet_at": last_line_at,
         "total_s": total, "reached": last_line_at is not None,
         "updater_downloaded": updated}
    print(f"  {label}: log_quiet_at={last_line_at if last_line_at else -1:6.2f}s "
          f"djcore={marks.get('djcore_ready',-1):6.2f}s "
          f"cpu={cpu:6.2f}s rss={peak_rss:5.0f}MB "
          f"{'[UPDATER DOWNLOADED]' if updated else ''}", flush=True)
    return r


def bench(label, bundle, insert_libs, runs=3):
    print(f"\n=== {label} ===", flush=True)
    return [run_once(bundle, insert_libs, f"run {i+1}") for i in range(runs)]


def med(rows, key, sub=None):
    vals = [(r["marks"].get(sub) if sub else r[key]) for r in rows]
    vals = sorted(v for v in vals if isinstance(v, (int, float)))
    return vals[len(vals) // 2] if vals else None


if __name__ == "__main__":
    SC = ("/private/tmp/claude-501/-Users-taggie-JoshCode-HPClickRepack/"
          "74d0dedf-8678-415f-8151-72966c072e2f/scratchpad")
    arm = bench("arm64 repack", "/Applications/HP Click.app", True)
    # Copy of the pristine backup, updater bypassed to match the arm64 build.
    # Kept out of .app form so macOS App Management never locks it.
    intel = bench("x86_64 stock under Rosetta", f"{SC}/IntelWork", False)

    print("\n" + "=" * 70)
    print(f"{'metric':<26}{'arm64':>12}{'x86_64/Rosetta':>18}{'ratio':>12}")
    print("=" * 70)
    rows = [("time to log quiescence", "quiet_at", None),
            ("time to DjCore ready", "marks", "djcore_ready"),
            ("time to printer svc", "marks", "first_printer_svc"),
            ("time to 'no printer'", "marks", "no_printer"),
            ("CPU-seconds burned", "cpu_s", None),
            ("peak RSS (MB)", "rss_mb", None)]
    for name, key, sub in rows:
        a, b = med(arm, key, sub), med(intel, key, sub)
        if a is None or b is None:
            print(f"{name:<26}{str(a):>12}{str(b):>18}{'n/a':>12}")
        else:
            print(f"{name:<26}{a:>12.2f}{b:>18.2f}{b/a if a else 0:>11.2f}x")
    print("\nreached terminal marker: "
          f"arm64 {sum(r['reached'] for r in arm)}/{len(arm)}, "
          f"intel {sum(r['reached'] for r in intel)}/{len(intel)}")
    print("intel runs that downloaded an update: "
          f"{sum(r['updater_downloaded'] for r in intel)}/{len(intel)}")
