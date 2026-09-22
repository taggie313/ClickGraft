#!/usr/bin/env python3
"""Drives cdp_latency.js against both builds and prints a comparison.

Launches each build with --remote-debugging-port, hands the port to the Node
CDP client, and tears everything down between runs (both builds share bundle id
com.hp.hpclick, so a survivor from either one makes the next launch exit via
requestSingleInstanceLock).
"""
import json
import os
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import arguments, prepared_pair, launch
from perf_ab2 import sample, read, parse_marks, LOGDIR_REL

JS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cdp_latency.js")
TOTAL, EARLY_END, LATE_START = 130, 30, 95


def run_once(bundle, manifest):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    with launch(bundle, manifest, [f"--remote-debugging-port={port}",
                                   "--remote-debugging-address=127.0.0.1"]) as (app, folder, mark):
        wall0 = time.time()
        try:
            result = subprocess.run(
                ["node", JS, str(port), str(TOTAL), str(EARLY_END), str(LATE_START)],
                capture_output=True, text=True, timeout=TOTAL + 260, check=True)
            data = json.loads(result.stdout.strip().splitlines()[-1])
            logdir = os.path.join(folder, LOGDIR_REL)
            marks = parse_marks(read(os.path.join(logdir, "HP Click.log")), wall0)
            if app.poll() is not None or 'no_printer' not in marks or 'djcore_ready' not in marks:
                raise ValueError('App exited or never reached the startup milestone')
            if 'update-downloaded' in read(os.path.join(logdir, 'HP Click App.main.log')):
                raise ValueError('Updater ran; comparison invalid')
            if any(not data.get(window, {}).get('ping_p50') for window in ('early', 'late')):
                raise ValueError('CDP did not return both measurement windows')
            data['cpu_s_at_end'], _ = sample(bundle, mark, app.pid)
            return data
        except Exception as exc:
            return {'error': str(exc)}


if __name__ == '__main__':
    args = arguments('Compare interaction latency in private same-version copies')
    # Resolve the dependency from the maintained benchmark directory, before
    # launching an app. npm ci --prefix benchmarks
    subprocess.run(['node', '-e', "require('chrome-remote-interface')"],
                   cwd=os.path.dirname(JS), check=True)
    with prepared_pair(args.arm, args.intel) as (copies, manifest, provenance):
        results = {arch: [run_once(bundle, manifest) for _ in range(args.runs)]
                   for arch, bundle in copies.items()}
    with open(args.output, 'w') as out:
        json.dump({'inputs': provenance, 'runs': results}, out, indent=2)
    print(json.dumps(results, indent=2))
    sys.exit(1 if any('error' in r for rows in results.values() for r in rows) else 0)
