// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Playwright half of the flow rendering lab benchmark. Opens N tabs on the lab
// page, records main-thread pressure in each, and -- on Chromium only -- can
// take a DevTools trace so the claim "this animates on the compositor" is
// checked rather than assumed.
//
// scripts/flow_lab_bench.py owns the scenario matrix. This file measures one
// scenario and prints one JSON object.

import { chromium, firefox } from 'playwright';

const config = JSON.parse(process.argv[2]);
const {
  url,
  tabs = 1,
  durationMs = 8000,
  browser = 'chromium',
  gpu = 'software',
  navigationTimeoutMs = 90000,
  trace = false,
  settleMs = 1200,
} = config;

const INSTRUMENT = () => {
  const bench = {
    mutations: 0,
    frameDeltas: [],
    lags: [],
    longTasks: [],
    started: performance.now(),
  };
  window.__bench = bench;

  const observeMutations = () => {
    const target = document.documentElement;
    if (!target) return;
    new MutationObserver((records) => {
      for (const record of records) {
        bench.mutations += 1 + record.addedNodes.length + record.removedNodes.length;
      }
    }).observe(target, {
      subtree: true,
      childList: true,
      attributes: true,
      characterData: true,
    });
  };

  if (document.documentElement) observeMutations();
  else document.addEventListener('DOMContentLoaded', observeMutations, { once: true });

  let previous = performance.now();
  const frame = (now) => {
    bench.frameDeltas.push(now - previous);
    previous = now;
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);

  const step = 50;
  let expected = performance.now() + step;
  setInterval(() => {
    const now = performance.now();
    bench.lags.push(Math.max(0, now - expected));
    expected = now + step;
  }, step);

  try {
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) bench.longTasks.push(entry.duration);
    }).observe({ entryTypes: ['longtask'] });
  } catch {
    // Firefox has no longtask observer; the lag sampler covers the same ground.
  }
};

// The measurement window starts after the page has settled, so page
// construction is not charged to the animation.
const RESET = () => {
  const bench = window.__bench;
  bench.mutations = 0;
  bench.frameDeltas.length = 0;
  bench.lags.length = 0;
  bench.longTasks.length = 0;
  bench.started = performance.now();
};

const SUMMARIZE = () => {
  const bench = window.__bench;
  const quantile = (values, q) => {
    if (!values.length) return null;
    const sorted = [...values].sort((a, b) => a - b);
    return sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * q))];
  };
  const mean = (values) =>
    values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;
  const elapsed = performance.now() - bench.started;
  const frames = bench.frameDeltas.slice(2);
  return {
    elapsedMs: elapsed,
    mutations: bench.mutations,
    frames: frames.length,
    fps: frames.length ? 1000 / mean(frames) : null,
    frameMeanMs: mean(frames),
    frameP95Ms: quantile(frames, 0.95),
    lagSamples: bench.lags.length,
    lagMeanMs: mean(bench.lags),
    lagP95Ms: quantile(bench.lags, 0.95),
    lagMaxMs: bench.lags.length ? Math.max(...bench.lags) : null,
    longTasks: bench.longTasks.length,
    longTaskTotalMs: bench.longTasks.reduce((a, b) => a + b, 0),
    lab: window.__lab || null,
    memoryMb:
      performance.memory && performance.memory.usedJSHeapSize
        ? performance.memory.usedJSHeapSize / (1024 * 1024)
        : null,
  };
};

const TRACE_CATEGORIES = [
  'blink',
  'cc',
  'devtools.timeline',
  'disabled-by-default-devtools.timeline',
  'disabled-by-default-devtools.timeline.frame',
];

// Names worth counting: whether the animation forces the main thread through
// style/layout/paint again, or stays in the compositor.
const TRACE_NAMES = [
  'UpdateLayoutTree',
  'Layout',
  'PrePaint',
  'Paint',
  'UpdateLayerTree',
  'RasterTask',
  'CompositeLayers',
  'Commit',
  'DrawFrame',
  'ScheduledActionExecute',
];

async function tracedCounts(page, ms) {
  const session = await page.context().newCDPSession(page);
  const events = [];
  session.on('Tracing.dataCollected', ({ value }) => events.push(...value));
  const finished = new Promise((resolve) => session.once('Tracing.tracingComplete', resolve));
  await session.send('Tracing.start', {
    traceConfig: { includedCategories: TRACE_CATEGORIES, recordMode: 'recordAsMuchAsPossible' },
    transferMode: 'ReportEvents',
  });
  await page.waitForTimeout(ms);
  await session.send('Tracing.end');
  await finished;

  const counts = {};
  const durations = {};
  for (const event of events) {
    if (!TRACE_NAMES.includes(event.name)) continue;
    if (event.ph !== 'X' && event.ph !== 'B') continue;
    counts[event.name] = (counts[event.name] || 0) + 1;
    if (typeof event.dur === 'number') {
      durations[event.name] = (durations[event.name] || 0) + event.dur / 1000;
    }
  }
  await session.detach().catch(() => {});
  return { windowMs: ms, counts, totalMs: durations, events: events.length };
}

// Which rasterisation path the run actually used. "software" is Chromium's
// default headless ANGLE/SwiftShader; "gpu" asks Chromium for the real device;
// "headed" opens a real window on $DISPLAY. Firefox reaches the GPU either way,
// so the flag only changes whether a window appears. Every report records the
// renderer string it observed, because a number measured on SwiftShader and one
// measured on a GPU are not comparable and must never be filed as if they were.
function launchOptions(engineName, mode) {
  if (mode === 'headed') return { headless: false };
  if (mode === 'gpu' && engineName === 'chromium') {
    return {
      headless: true,
      args: [
        '--use-gl=angle',
        '--use-angle=gl',
        '--enable-gpu',
        '--ignore-gpu-blocklist',
      ],
    };
  }
  return { headless: true };
}

const RENDERER_PROBE = () => {
  try {
    const probe = document.createElement('canvas');
    const gl = probe.getContext('webgl2') || probe.getContext('webgl');
    if (!gl) return { renderer: null, webgl: false };
    const dbg = gl.getExtension('WEBGL_debug_renderer_info');
    return {
      webgl: true,
      renderer: dbg
        ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL)
        : gl.getParameter(gl.RENDERER),
    };
  } catch (error) {
    return { renderer: null, webgl: false, error: String(error) };
  }
};

async function main() {
  const engine = browser === 'firefox' ? firefox : chromium;
  const instance = await engine.launch(launchOptions(browser, gpu));
  const context = await instance.newContext({ viewport: { width: 1440, height: 900 } });
  await context.addInitScript(INSTRUMENT);

  const pages = [];
  for (let i = 0; i < tabs; i += 1) {
    const page = await context.newPage();
    await page.goto(url, { waitUntil: 'load', timeout: navigationTimeoutMs });
    await page.waitForFunction(() => window.__lab && window.__lab.ready, null, { timeout: 30000 });
    pages.push(page);
  }

  await pages[pages.length - 1].bringToFront();
  await pages[0].waitForTimeout(settleMs);
  for (const page of pages) await page.evaluate(RESET);

  let traceResult = null;
  if (trace && browser === 'chromium') {
    traceResult = await tracedCounts(pages[pages.length - 1], durationMs);
  } else {
    await pages[0].waitForTimeout(durationMs);
  }

  const rasterisation = await pages[0].evaluate(RENDERER_PROBE);

  const perTab = [];
  for (const page of pages) perTab.push(await page.evaluate(SUMMARIZE));

  await context.close();
  await instance.close();

  const sum = (key) => perTab.reduce((a, t) => a + (t[key] || 0), 0);
  const worst = (key) =>
    perTab.reduce((a, t) => (t[key] == null ? a : Math.max(a, t[key])), 0);

  process.stdout.write(
    JSON.stringify({
      config: { url, tabs, durationMs, browser, gpu, trace },
      rasterisation,
      perTab,
      trace: traceResult,
      totals: {
        mutations: sum('mutations'),
        longTasks: sum('longTasks'),
        longTaskTotalMs: sum('longTaskTotalMs'),
        worstLagP95Ms: worst('lagP95Ms'),
        worstLagMaxMs: worst('lagMaxMs'),
        worstFrameP95Ms: worst('frameP95Ms'),
        foregroundFps: perTab[perTab.length - 1].fps,
        meanFps:
          perTab.reduce((a, t) => a + (t.fps || 0), 0) / (perTab.length || 1),
      },
    }),
  );
}

main().catch((error) => {
  process.stderr.write(String(error && error.stack ? error.stack : error));
  process.exit(1);
});
