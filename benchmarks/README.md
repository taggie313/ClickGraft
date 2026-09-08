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

**Control the updater.** The stock build asks HP for an update on every launch
and, if one is offered, downloads the whole application — 572 MB
(572,397,601 bytes, measured against HPClick-4.8.117.zip on HP's server), not
the ~200 MB this file used to claim. That dominates any CPU comparison if left enabled.

It does not necessarily *install*: HP's ShipIt cannot launch on these bundles
(its rpaths do not resolve `@rpath/Mantle.framework/Mantle`, verified on
4.8.117, 4.8.118 and 4.10.42), so the download can complete and go nowhere. The
bandwidth is spent either way, which is the part that matters here.

Report run-to-run range, not just a median: the Intel build ranged 54–109 s
across three runs, and that variance is itself the user-visible symptom.
