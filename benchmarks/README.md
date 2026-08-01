# Benchmarks

The harness behind the numbers in the top-level README. Both builds must be
present; both are driven identically.

| File | Measures |
|---|---|
| `perf_ab2.py` | Startup cost — time to log quiescence, CPU-seconds, peak RSS. |
| `latency_ab.py` + `cdp_latency.js` | Interaction latency over the Chrome DevTools Protocol: main-thread ping RTT, real input-event latency, and long-task blocking time. Needs `npm install chrome-remote-interface`. |

## Two traps these encode

**Do not stop a run when CPU goes flat.** That measures "this process paused",
not "this process finished", and on a slow build a mid-startup pause is
indistinguishable from being done — it silently compares different amounts of
work. An earlier version did exactly this and produced numbers that flattered
the native build. These stop on quiescence of the app's own timestamped log
instead.

**Control the updater.** The stock build downloads a ~200 MB update on launch,
which dominates any CPU comparison if left enabled.

Report run-to-run range, not just a median: the Intel build ranged 54–109 s
across three runs, and that variance is itself the user-visible symptom.
