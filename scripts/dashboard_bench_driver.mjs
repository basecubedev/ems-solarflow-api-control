// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Playwright half of the dashboard benchmark. Opens N tabs against a running
// preview server, records main-thread pressure inside each page and prints one
// JSON object. scripts/dashboard_bench.py owns the scenario matrix and the
// report; this file only measures what it is told to.
//
// Event-loop lag is the primary metric because it is the one the reported
// symptom is made of and the only one both Chromium and Firefox report the
// same way. Long tasks are Chromium-only and are recorded when available.

import { chromium, firefox } from 'playwright';

const config = JSON.parse(process.argv[2]);
const {
  url,
  tabs = 1,
  view = 'aggregated',
  animation = 'normal',
  transport = 'sse',
  durationMs = 8000,
  browser = 'chromium',
  gpu = 'software',
  backdrop = 'on',
  extraCss = '',
  extraJs = '',
  navigationTimeoutMs = 90000,
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

  // A timer that should fire every 50 ms. Whatever it is late by is time the
  // main thread was not available -- exactly what a user feels as lag.
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
  // The first frames cover page construction, which is not what this measures.
  const frames = bench.frameDeltas.slice(5);
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
  };
};

const NO_BACKDROP_CSS = `
  .metric, .flow-panel, .rules-panel, .chart-panel, .device-card, .energy-stats-panel {
    backdrop-filter: none !important;
  }
`;

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

  await context.route('**/api/ui-config', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ animation_mode: animation }),
    }),
  );

  if (transport === 'polling') {
    // What the third tab from one machine already gets from the real server.
    await context.route('**/api/events', (route) =>
      route.fulfill({
        status: 429,
        contentType: 'application/json',
        body: JSON.stringify({ error: 'sse_connection_limit' }),
      }),
    );
  }

  const pages = [];
  for (let i = 0; i < tabs; i += 1) {
    const page = await context.newPage();
    await page.goto(url, { waitUntil: 'load', timeout: navigationTimeoutMs });
    if (backdrop === 'off') await page.addStyleTag({ content: NO_BACKDROP_CSS });
    if (extraCss) await page.addStyleTag({ content: extraCss });
    if (extraJs) await page.evaluate(extraJs);
    await page.evaluate((target) => {
      if (typeof setFlowView === 'function') setFlowView(target, false);
    }, view);
    pages.push(page);
  }

  // Only the last tab stays foreground, which is what several open tabs means.
  await pages[pages.length - 1].bringToFront();
  await pages[0].waitForTimeout(durationMs);

  const rasterisation = await pages[0].evaluate(RENDERER_PROBE);

  const perTab = [];
  for (const page of pages) {
    perTab.push(await page.evaluate(SUMMARIZE));
  }

  await context.close();
  await instance.close();

  const sum = (key) => perTab.reduce((a, t) => a + (t[key] || 0), 0);
  const worst = (key) =>
    perTab.reduce((a, t) => (t[key] == null ? a : Math.max(a, t[key])), 0);

  process.stdout.write(
    JSON.stringify({
      config: { url, tabs, view, animation, transport, durationMs, browser, gpu, backdrop, extraCss, extraJs },
      rasterisation,
      perTab,
      totals: {
        mutations: sum('mutations'),
        longTasks: sum('longTasks'),
        longTaskTotalMs: sum('longTaskTotalMs'),
        worstLagP95Ms: worst('lagP95Ms'),
        worstLagMaxMs: worst('lagMaxMs'),
        worstFrameP95Ms: worst('frameP95Ms'),
        foregroundFps: perTab[perTab.length - 1].fps,
      },
    }),
  );
}

main().catch((error) => {
  process.stderr.write(String(error && error.stack ? error.stack : error));
  process.exit(1);
});
