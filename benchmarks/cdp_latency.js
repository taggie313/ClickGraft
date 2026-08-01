/*
 * Renderer main-thread responsiveness over CDP.
 *
 * Measures the thing behind "still waiting for button clicks to register":
 *
 *   ping_ms    round trip of a trivial Runtime.evaluate. It runs ON the
 *              renderer main thread, so the RTT includes however long the
 *              main thread is already blocked. This is the queueing delay a
 *              real click waits through.
 *   input_ms   a real Input.dispatchMouseEvent (mouseMoved -- deliberately
 *              NOT a click, so nothing in the UI is activated) timed until a
 *              capture-phase listener in the page fires. Exercises the full
 *              browser input pipeline: hit-test, routing, main-thread dispatch.
 *   longtask   PerformanceObserver 'longtask' entries: main-thread blocks
 *              >50ms. Total blocking time is what makes a UI feel dead.
 *
 * Samples are bucketed into an early window (during startup churn) and a late
 * window (after the app has settled), because those are different experiences.
 *
 * usage: node cdp_latency.js <port> <totalSeconds> <earlyEndsAt> <lateStartsAt>
 */
const CDP = require('chrome-remote-interface');

const PORT = +process.argv[2];
const TOTAL = +(process.argv[3] || 120);
const EARLY_END = +(process.argv[4] || 30);
const LATE_START = +(process.argv[5] || 90);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const pct = (a, p) => {
  if (!a.length) return null;
  const s = [...a].sort((x, y) => x - y);
  return s[Math.min(s.length - 1, Math.floor((p / 100) * s.length))];
};

(async () => {
  let target = null;
  const deadline = Date.now() + 240000;
  while (Date.now() < deadline && !target) {
    try {
      const list = await CDP.List({ port: PORT });
      target = list.find((t) => t.type === 'page' && t.url.startsWith('file://') && t.title);
    } catch (e) { /* endpoint not up yet */ }
    if (!target) await sleep(200);
  }
  if (!target) { console.log(JSON.stringify({ error: 'no page target' })); process.exit(1); }

  const client = await CDP({ port: PORT, target });
  const { Runtime, Input } = client;
  await Runtime.enable();

  await Runtime.evaluate({ expression: `
    window.__m = { long: [] };
    try {
      new PerformanceObserver((l) => {
        for (const e of l.getEntries()) window.__m.long.push(e.duration);
      }).observe({ entryTypes: ['longtask'] });
    } catch (e) { window.__m.longtaskUnsupported = true; }
  ` });

  const t0 = Date.now();
  const ping = { early: [], late: [] };
  const input = { early: [], late: [] };
  let longEarly = null;

  while ((Date.now() - t0) / 1000 < TOTAL) {
    const el = (Date.now() - t0) / 1000;
    const bucket = el <= EARLY_END ? 'early' : (el >= LATE_START ? 'late' : null);

    const p0 = process.hrtime.bigint();
    try { await Runtime.evaluate({ expression: '0' }); } catch (e) { break; }
    const pingMs = Number(process.hrtime.bigint() - p0) / 1e6;
    if (bucket) ping[bucket].push(pingMs);

    // Real input event through the browser pipeline, every 4th sample.
    // Shift keydown: routed without hit-testing and harmless to app state.
    // (mouseMoved was tried first and pinned at ~5000ms regardless of build --
    // a delivery artifact of an unfocused window, not a latency measurement.)
    if (bucket && ping[bucket].length % 4 === 0) {
      try {
        await Runtime.evaluate({ expression:
          `window.__kv = new Promise((r) => window.addEventListener(
             'keydown', () => r(1), { once: true, capture: true }))` });
        const i0 = process.hrtime.bigint();
        await Input.dispatchKeyEvent({
          type: 'rawKeyDown', windowsVirtualKeyCode: 16, key: 'Shift', code: 'ShiftLeft',
        });
        await Runtime.evaluate({ expression: 'window.__kv', awaitPromise: true, timeout: 20000 });
        input[bucket].push(Number(process.hrtime.bigint() - i0) / 1e6);
      } catch (e) { /* renderer busy or gone */ }
    }

    if (longEarly === null && el > EARLY_END) {
      try {
        const r = await Runtime.evaluate({ expression: 'JSON.stringify(window.__m.long)', returnByValue: true });
        longEarly = JSON.parse(r.result.value || '[]');
      } catch (e) { longEarly = []; }
    }
    await sleep(250);
  }

  let longAll = [];
  try {
    const r = await Runtime.evaluate({ expression: 'JSON.stringify(window.__m.long)', returnByValue: true });
    longAll = JSON.parse(r.result.value || '[]');
  } catch (e) { /* gone */ }
  const longLate = longAll.slice((longEarly || []).length);

  const sum = (a) => a.reduce((x, y) => x + y, 0);
  const win = (name, pings, inputs, longs) => ({
    window: name,
    ping_p50: pct(pings, 50), ping_p95: pct(pings, 95), ping_max: pings.length ? Math.max(...pings) : null,
    input_p50: pct(inputs, 50), input_p95: pct(inputs, 95), input_max: inputs.length ? Math.max(...inputs) : null,
    longtask_count: longs.length,
    longtask_total_ms: Math.round(sum(longs)),
    longtask_max_ms: longs.length ? Math.round(Math.max(...longs)) : 0,
    samples: pings.length, input_samples: inputs.length,
  });

  console.log(JSON.stringify({
    early: win('early', ping.early, input.early, longEarly || []),
    late: win('late', ping.late, input.late, longLate),
  }));
  await client.close();
  process.exit(0);
})().catch((e) => { console.log(JSON.stringify({ error: String(e) })); process.exit(1); });
