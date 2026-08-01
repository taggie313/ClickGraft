#!/usr/bin/env python3
"""Drives cdp_latency.js against both builds and prints a comparison.

Launches each build with --remote-debugging-port, hands the port to the Node
CDP client, and tears everything down between runs (both builds share bundle id
com.hp.hpclick, so a survivor from either one makes the next launch exit via
requestSingleInstanceLock).
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from perf_ab2 import kill_all, sample  # noqa: E402

SC = ("/private/tmp/claude-501/-Users-taggie-JoshCode-HPClickRepack/"
      "74d0dedf-8678-415f-8151-72966c072e2f/scratchpad")
JS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cdp_latency.js")

TOTAL, EARLY_END, LATE_START = 130, 30, 95
RUNS = 2


def run_once(bundle, insert_libs, port):
    kill_all(bundle)
    res = f"{bundle}/Contents/Resources/app/appData/macx"
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": os.path.expanduser("~"),
           "DYLD_FRAMEWORK_PATH": f"{res}/Frameworks",
           "DYLD_LIBRARY_PATH": f"{res}/lib"}
    if insert_libs:
        env["DYLD_INSERT_LIBRARIES"] = (
            f"{res}/lib/libunistring.5.dylib:{res}/lib/libidn2.0.dylib")

    app = subprocess.Popen(
        [f"{bundle}/Contents/MacOS/HPClickExe", f"--remote-debugging-port={port}"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        nenv = dict(os.environ)
        nenv["NODE_PATH"] = f"{SC}/node_modules"   # client installed in scratch
        out = subprocess.run(
            ["node", JS, str(port), str(TOTAL), str(EARLY_END), str(LATE_START)],
            capture_output=True, text=True, env=nenv,
            timeout=TOTAL + 260).stdout.strip()
        data = json.loads(out.splitlines()[-1]) if out else {"error": "no output"}
    except Exception as e:                                    # noqa: BLE001
        data = {"error": str(e)}
    cpu, _ = sample(bundle)
    data["cpu_s_at_end"] = cpu
    kill_all(bundle)
    try:
        app.wait(timeout=5)
    except subprocess.TimeoutExpired:
        app.kill()
    return data


def bench(label, bundle, insert_libs, port):
    print(f"\n=== {label} ===", flush=True)
    rows = []
    for i in range(RUNS):
        d = run_once(bundle, insert_libs, port + i)
        rows.append(d)
        if "error" in d:
            print(f"  run {i+1}: ERROR {d['error']}", flush=True)
            continue
        for w in ("early", "late"):
            x = d[w]
            print(f"  run {i+1} {w:<5}: ping p50={x['ping_p50']:7.1f} p95={x['ping_p95']:8.1f} "
                  f"max={x['ping_max']:8.1f} | input p50={x['input_p50'] or -1:7.1f} "
                  f"p95={x['input_p95'] or -1:8.1f} | longtasks={x['longtask_count']:4d} "
                  f"blocking={x['longtask_total_ms']:6d}ms", flush=True)
    return rows


def med(rows, w, k):
    v = sorted(r[w][k] for r in rows if "error" not in r and r[w].get(k) is not None)
    return v[len(v) // 2] if v else None


if __name__ == "__main__":
    arm = bench("arm64 repack", "/Applications/HP Click.app", True, 9300)
    intel = bench("x86_64 stock under Rosetta", f"{SC}/IntelWork", False, 9400)

    print("\n" + "=" * 78)
    print(f"{'metric':<34}{'arm64':>12}{'x86_64/Rosetta':>18}{'ratio':>12}")
    print("=" * 78)
    for w in ("early", "late"):
        print(f"-- {w} window --")
        for k, lbl in [("ping_p50", "main-thread ping p50 (ms)"),
                       ("ping_p95", "main-thread ping p95 (ms)"),
                       ("ping_max", "main-thread ping max (ms)"),
                       ("input_p50", "input event latency p50 (ms)"),
                       ("input_p95", "input event latency p95 (ms)"),
                       ("longtask_count", "long tasks (>50ms)"),
                       ("longtask_total_ms", "total blocking time (ms)")]:
            a, b = med(arm, w, k), med(intel, w, k)
            if a is None or b is None:
                print(f"  {lbl:<32}{str(a):>12}{str(b):>18}{'n/a':>12}")
            else:
                r = f"{b/a:.2f}x" if a else "n/a"
                print(f"  {lbl:<32}{a:>12.1f}{b:>18.1f}{r:>12}")
    json.dump({"arm64": arm, "x86_64": intel}, open(f"{SC}/latency_ab.json", "w"), indent=2)
