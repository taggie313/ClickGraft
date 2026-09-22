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
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from clickgraft import verify
from common import arguments, prepared_pair, launch

# Each run gets its own TMPDIR and --user-data-dir, so HP's logs land inside it
# rather than in the shared /tmp/HP folder both runs and any HP Click the owner
# has open would otherwise share. That also removes the reason this script used
# to sweep every HP Click process on the Mac: with a private profile there is no
# single-instance lock to clear. Both A and B runs get the same treatment, so
# they still stop at the same lifecycle stage.
LOGDIR_REL = "HP/HP Click/logs"
TERMINAL = "isPrintable - no printer or roll selected"
CAP = 240.0
LOG_QUIET = 12.0   # log silent this long == startup work finished

PHASES = [
    ("djcore_init_start", "--> Initializing DjCore"),
    ("djcore_ready",      "successful initialization"),
    ("first_printer_svc", "printer-service:"),
    ("no_printer",        TERMINAL),
]


def sample(bundle, mark, root_pid):
    p = [pid for pid, _ in verify.launched_processes(bundle, mark, root_pid)]
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


def run_once(bundle, manifest, label):
    with launch(bundle, manifest) as (proc, folder, mark):
        render_log = os.path.join(folder, LOGDIR_REL, "HP Click.log")
        main_log = os.path.join(folder, LOGDIR_REL, "HP Click App.main.log")
        t0, wall0 = time.monotonic(), time.time()
        last_change, prev_blob, peak_rss = t0, "", 0.0
        reached = False
        error = "Startup did not reach the terminal marker and settle before the deadline"
        while time.monotonic() - t0 < CAP:
            blob = read(render_log)
            if blob != prev_blob:
                last_change, prev_blob = time.monotonic(), blob
            marks = parse_marks(blob, wall0)
            _, rss = sample(bundle, mark, proc.pid)
            peak_rss = max(peak_rss, rss)
            if proc.poll() is not None:
                error = f"Application exited during startup ({proc.returncode})"
                break
            if "no_printer" in marks and "djcore_ready" in marks and time.monotonic() - last_change >= LOG_QUIET:
                reached = True
                break
            time.sleep(0.25)
        cpu, _ = sample(bundle, mark, proc.pid)
        updated = "update-downloaded" in read(main_log)
        result = {"marks": parse_marks(read(render_log), wall0), "cpu_s": cpu,
                  "rss_mb": peak_rss, "quiet_at": newest_ts(read(render_log), wall0),
                  "total_s": time.monotonic() - t0, "reached": reached,
                  "updater_downloaded": updated}
        if not reached or updated:
            result["error"] = "Updater ran; comparison invalid" if updated else error
        print(f"{label}: {result}", flush=True)
        return result


def med(rows, key, sub=None):
    values = [(row["marks"].get(sub) if sub else row[key]) for row in rows if "error" not in row]
    values = sorted(value for value in values if isinstance(value, (int, float)))
    return values[len(values) // 2] if values else None


if __name__ == "__main__":
    args = arguments("Compare startup using private copies of same-version Intel and arm64 apps")
    with prepared_pair(args.arm, args.intel) as (copies, manifest, provenance):
        results = {arch: [run_once(bundle, manifest, f"{arch} run {i + 1}")
                          for i in range(args.runs)] for arch, bundle in copies.items()}
    with open(args.output, "w") as out:
        json.dump({"inputs": provenance, "runs": results}, out, indent=2)
    sys.exit(1 if any("error" in r for rows in results.values() for r in rows) else 0)
