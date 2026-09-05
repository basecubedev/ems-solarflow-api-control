// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Attribution profiler for the dashboard.
//
// The existing benchmark answers "how fast is the page". This answers "what is
// the page doing", which is the question left over when a frame rate says
// everything is fine and a user says it is not. It works by wrapping the entry
// points through which a page can consume its main thread -- event listeners,
// timers, animation frames, observers -- and charging the time to whoever was
// called. Nothing here is browser-specific, so the same numbers can be taken on
// a Mac, which is the platform the symptom was reported on and the one that
// cannot be measured here.
//
// scripts/dashboard_profile/profile_bench.py owns the scenarios.

import { chromium, firefox } from 'playwright';

const config = JSON.parse(process.argv[2]);
const {
  url,
  neighbourUrl = null,
  view = 'aggregated',
  animation = 'normal',
  transport = 'sse',
  durationMs = 10000,
  settleMs = 2500,
  browser = 'chromium',
  gpu = 'headed',
  software = false,
  deepReads = false,
  cpuThrottle = 1,
  foreground = 'dashboard',
  dashboardOpen = true,
  extraJs = '',
  navigationTimeoutMs = 90000,
} = config;

// Installed before any page script. Every wrapper keeps the original callable
// reachable so removeEventListener still matches, and the sampler timers use
// the pre-patch functions so the profiler never charges itself.
const INSTRUMENT = () => {
  const work = Object.create(null);
  const bench = {
    work,
    mutations: 0,
    mutationTargets: Object.create(null),
    frameDeltas: [],
    lags: [],
    taskGaps: [],
    longTasks: [],
    fetches: 0,
    fetchWallMs: 0,
    started: 0,
  };
  window.__prof = bench;

  const now = () => performance.now();
  const account = (name, startedAt) => {
    const spent = now() - startedAt;
    const slot = work[name] || (work[name] = { calls: 0, ms: 0, max: 0 });
    slot.calls += 1;
    slot.ms += spent;
    if (spent > slot.max) slot.max = spent;
  };
  const wrap = (name, fn) => {
    if (typeof fn !== 'function') return fn;
    return function wrapped(...args) {
      const startedAt = now();
      try {
        return fn.apply(this, args);
      } finally {
        account(name, startedAt);
      }
    };
  };

  const rawSetTimeout = window.setTimeout.bind(window);
  const rawSetInterval = window.setInterval.bind(window);
  const rawRaf = window.requestAnimationFrame
    ? window.requestAnimationFrame.bind(window)
    : null;
  const RawMutationObserver = window.MutationObserver;

  const listenerWrappers = new WeakMap();
  const originalAdd = EventTarget.prototype.addEventListener;
  const originalRemove = EventTarget.prototype.removeEventListener;
  EventTarget.prototype.addEventListener = function (type, listener, options) {
    if (typeof listener === 'function') {
      let wrapped = listenerWrappers.get(listener);
      if (!wrapped) {
        wrapped = wrap('listener:' + type, listener);
        listenerWrappers.set(listener, wrapped);
      }
      return originalAdd.call(this, type, wrapped, options);
    }
    return originalAdd.call(this, type, listener, options);
  };
  EventTarget.prototype.removeEventListener = function (type, listener, options) {
    const wrapped = typeof listener === 'function' ? listenerWrappers.get(listener) : null;
    return originalRemove.call(this, type, wrapped || listener, options);
  };

  // EventSource handlers are usually assigned, not added, so the accessor has
  // to be patched too or the whole snapshot path is invisible.
  if (window.EventSource) {
    for (const prop of ['onmessage', 'onopen', 'onerror']) {
      const descriptor = Object.getOwnPropertyDescriptor(EventSource.prototype, prop);
      if (!descriptor || !descriptor.set) continue;
      Object.defineProperty(EventSource.prototype, prop, {
        configurable: true,
        enumerable: descriptor.enumerable,
        get: descriptor.get,
        set(fn) { descriptor.set.call(this, wrap('sse:' + prop, fn)); },
      });
    }
  }

  window.setTimeout = function (fn, ms, ...rest) {
    return rawSetTimeout(typeof fn === 'function' ? wrap('setTimeout', fn) : fn, ms, ...rest);
  };
  window.setInterval = function (fn, ms, ...rest) {
    return rawSetInterval(typeof fn === 'function' ? wrap('setInterval', fn) : fn, ms, ...rest);
  };
  if (rawRaf) {
    window.requestAnimationFrame = function (cb) {
      return rawRaf(typeof cb === 'function' ? wrap('requestAnimationFrame', cb) : cb);
    };
  }

  for (const [name, Original] of [
    ['ResizeObserver', window.ResizeObserver],
    ['MutationObserver', window.MutationObserver],
    ['IntersectionObserver', window.IntersectionObserver],
  ]) {
    if (typeof Original !== 'function') continue;
    const Patched = function (callback, ...rest) {
      return new Original(wrap(name, callback), ...rest);
    };
    Patched.prototype = Original.prototype;
    window[name] = Patched;
  }

  // Opt-in: charge the layout-forcing reads themselves. A read that flushes
  // pending style and layout costs whatever has accumulated since the last
  // flush, so the same code can be cheap or expensive depending only on what
  // else has run. Off by default because wrapping these perturbs them.
  if (window.__profDeepReads) {
    const rectOriginal = Element.prototype.getBoundingClientRect;
    Element.prototype.getBoundingClientRect = function (...args) {
      const startedAt = now();
      try { return rectOriginal.apply(this, args); }
      finally { account('read:getBoundingClientRect', startedAt); }
    };
    const styleOriginal = window.getComputedStyle.bind(window);
    window.getComputedStyle = function (...args) {
      const startedAt = now();
      try { return styleOriginal(...args); }
      finally { account('read:getComputedStyle', startedAt); }
    };
    for (const [proto, prop] of [
      [HTMLElement.prototype, 'offsetHeight'],
      [HTMLElement.prototype, 'offsetWidth'],
      [Element.prototype, 'clientHeight'],
      [Element.prototype, 'clientWidth'],
    ]) {
      const descriptor = Object.getOwnPropertyDescriptor(proto, prop);
      if (!descriptor || !descriptor.get) continue;
      Object.defineProperty(proto, prop, {
        configurable: true,
        enumerable: descriptor.enumerable,
        get() {
          const startedAt = now();
          try { return descriptor.get.call(this); }
          finally { account('read:' + prop, startedAt); }
        },
      });
    }
  }

  if (typeof window.fetch === 'function') {
    const rawFetch = window.fetch.bind(window);
    window.fetch = function (...args) {
      const startedAt = now();
      bench.fetches += 1;
      return rawFetch(...args).finally(() => {
        bench.fetchWallMs += now() - startedAt;
      });
    };
  }

  const observeMutations = () => {
    if (!document.documentElement || typeof RawMutationObserver !== 'function') return;
    new RawMutationObserver((records) => {
      for (const record of records) {
        bench.mutations += 1 + record.addedNodes.length + record.removedNodes.length;
        let node = record.target;
        if (node && node.nodeType === 3) node = node.parentElement;
        let key = 'unknown';
        try {
          const owner = node && node.closest ? node.closest('[id]') : null;
          key = owner && owner.id ? owner.id : (node && node.nodeName) || 'unknown';
        } catch { key = 'unknown'; }
        bench.mutationTargets[key] = (bench.mutationTargets[key] || 0) + 1;
      }
    }).observe(document.documentElement, {
      subtree: true, childList: true, attributes: true, characterData: true,
    });
  };
  if (document.documentElement) observeMutations();
  else originalAdd.call(document, 'DOMContentLoaded', observeMutations, { once: true });

  if (rawRaf) {
    let previous = now();
    const frame = (stamp) => {
      bench.frameDeltas.push(stamp - previous);
      previous = stamp;
      rawRaf(frame);
    };
    rawRaf(frame);
  }

  const step = 50;
  let expected = now() + step;
  rawSetInterval(() => {
    const stamp = now();
    bench.lags.push(Math.max(0, stamp - expected));
    expected = stamp + step;
  }, step);

  // A self-rescheduling zero timeout runs at roughly 250 Hz once nested, so a
  // gap much larger than its clamp is a task that held the thread. This is the
  // engine-neutral stand-in for the long-task observer Firefox does not have.
  let lastTick = now();
  const tick = () => {
    const stamp = now();
    const gap = stamp - lastTick;
    bench.taskGaps.push(gap);
    lastTick = stamp;
    rawSetTimeout(tick, 0);
  };
  rawSetTimeout(tick, 0);

  try {
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) bench.longTasks.push(entry.duration);
    }).observe({ entryTypes: ['longtask'] });
  } catch {
    // Chromium only; taskGaps covers the same ground everywhere else.
  }
};

const RESET = () => {
  const bench = window.__prof;
  for (const key of Object.keys(bench.work)) delete bench.work[key];
  for (const key of Object.keys(bench.mutationTargets)) delete bench.mutationTargets[key];
  bench.mutations = 0;
  bench.frameDeltas.length = 0;
  bench.lags.length = 0;
  bench.taskGaps.length = 0;
  bench.longTasks.length = 0;
  bench.fetches = 0;
  bench.fetchWallMs = 0;
  bench.started = performance.now();
};

const SUMMARIZE = () => {
  const bench = window.__prof;
  const quantile = (values, q) => {
    if (!values.length) return null;
    const sorted = [...values].sort((a, b) => a - b);
    return sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * q))];
  };
  const mean = (values) =>
    values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;
  const elapsed = performance.now() - bench.started;
  const frames = bench.frameDeltas.slice(5);
  const gaps = bench.taskGaps.slice(5);
  const blocking = gaps.filter((g) => g > 12);

  const work = Object.entries(bench.work)
    .map(([name, slot]) => ({
      name,
      calls: slot.calls,
      ms: Number(slot.ms.toFixed(1)),
      maxMs: Number(slot.max.toFixed(1)),
      shareOfWall: Number((slot.ms / elapsed).toFixed(4)),
    }))
    .sort((a, b) => b.ms - a.ms);
  const totalWorkMs = work.reduce((a, w) => a + w.ms, 0);

  const targets = Object.entries(bench.mutationTargets)
    .map(([name, count]) => ({ name, count }))
    .sort((a, b) => b.count - a.count)
    .slice(0, 12);

  return {
    elapsedMs: Number(elapsed.toFixed(0)),
    fps: frames.length ? 1000 / mean(frames) : null,
    frameP95Ms: quantile(frames, 0.95),
    lagMeanMs: mean(bench.lags),
    lagP95Ms: quantile(bench.lags, 0.95),
    lagMaxMs: bench.lags.length ? Math.max(...bench.lags) : null,
    taskGapP95Ms: quantile(gaps, 0.95),
    taskGapMaxMs: gaps.length ? Math.max(...gaps) : null,
    blockingTasks: blocking.length,
    blockingMs: Number(blocking.reduce((a, b) => a + b, 0).toFixed(1)),
    longTasks: bench.longTasks.length,
    longTaskTotalMs: Number(bench.longTasks.reduce((a, b) => a + b, 0).toFixed(1)),
    mutations: bench.mutations,
    mutationTargets: targets,
    fetches: bench.fetches,
    fetchWallMs: Number(bench.fetchWallMs.toFixed(1)),
    domNodes: document.getElementsByTagName('*').length,
    attributedWorkMs: Number(totalWorkMs.toFixed(1)),
    attributedShareOfWall: Number((totalWorkMs / elapsed).toFixed(4)),
    work: work.slice(0, 20),
    memoryMb: performance.memory && performance.memory.usedJSHeapSize
      ? Number((performance.memory.usedJSHeapSize / (1024 * 1024)).toFixed(1))
      : null,
    animationsRunning: (() => {
      try { return document.getAnimations ? document.getAnimations().length : null; }
      catch { return null; }
    })(),
    hidden: document.hidden,
  };
};

const NEIGHBOUR_PAGE = `<!doctype html><meta charset="utf-8">
<title>neighbour</title>
<style>body{margin:0;background:#101418;color:#cfd8e3;font:14px system-ui}
main{padding:24px}</style>
<main><h1>Neighbour page</h1>
<p>A deliberately trivial page. Its only job is to report how responsive it is
while the dashboard is open beside it.</p></main>`;

function launchOptions(engineName, mode, softwareRendering) {
  const options = mode === 'headed' ? { headless: false } : { headless: true };
  if (engineName === 'firefox' && softwareRendering) {
    // Force WebRender's software backend. Recorded verbatim in the report so
    // "software" is never an inference from a device name.
    options.firefoxUserPrefs = {
      'gfx.webrender.software': true,
      'gfx.webrender.software.opengl': false,
      'layers.acceleration.disabled': true,
    };
  }
  if (engineName === 'chromium' && softwareRendering) {
    options.args = ['--disable-gpu', '--disable-gpu-compositing'];
  } else if (engineName === 'chromium' && mode === 'gpu') {
    options.headless = true;
    options.args = ['--use-gl=angle', '--use-angle=gl', '--enable-gpu', '--ignore-gpu-blocklist'];
  }
  return options;
}

const RENDERER_PROBE = () => {
  const out = { webgl: false, renderer: null };
  try {
    const probe = document.createElement('canvas');
    const gl = probe.getContext('webgl2') || probe.getContext('webgl');
    if (gl) {
      out.webgl = true;
      const dbg = gl.getExtension('WEBGL_debug_renderer_info');
      out.renderer = dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
    }
  } catch (error) {
    out.error = String(error);
  }
  out.note = 'names the device WebGL was given, not the page compositor; '
    + 'it can prove software, never hardware';
  return out;
};

async function main() {
  const engine = browser === 'firefox' ? firefox : chromium;
  const instance = await engine.launch(launchOptions(browser, gpu, software));
  const context = await instance.newContext({ viewport: { width: 1440, height: 900 } });
  if (deepReads) {
    await context.addInitScript(() => { window.__profDeepReads = true; });
  }
  await context.addInitScript(INSTRUMENT);

  await context.route('**/api/ui-config', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ animation_mode: animation }),
    }),
  );
  if (transport === 'polling') {
    await context.route('**/api/events', (route) =>
      route.fulfill({
        status: 429,
        contentType: 'application/json',
        body: JSON.stringify({ error: 'sse_connection_limit' }),
      }),
    );
  }

  // Chromium only, and the reason this axis exists: it slows the main thread by
  // an exact factor, which is where the dashboard's cost was measured to be.
  // Firefox has no equivalent, and its software-rendering prefs could not be
  // confirmed to take effect (see gfx_probe.mjs), so no Firefox result is filed
  // as a slow-machine measurement.
  const throttle = async (page) => {
    if (browser !== 'chromium' || cpuThrottle <= 1) return null;
    const session = await context.newCDPSession(page);
    await session.send('Emulation.setCPUThrottlingRate', { rate: cpuThrottle });
    return session;
  };

  let extraJsResult = null;
  let dashboard = null;
  if (dashboardOpen) {
    dashboard = await context.newPage();
    await dashboard.goto(url, { waitUntil: 'load', timeout: navigationTimeoutMs });
    await dashboard.evaluate((target) => {
      if (typeof setFlowView === 'function') setFlowView(target, false);
    }, view);
    await throttle(dashboard);
    if (extraJs) {
      const applied = await dashboard.evaluate(extraJs);
      // An A/B whose treatment silently did nothing is worse than no A/B.
      if (applied === false || applied === undefined) {
        process.stderr.write('extraJs returned ' + String(applied) + '\n');
      }
      extraJsResult = applied === undefined ? null : applied;
    }
  }

  let neighbour = null;
  if (neighbourUrl || foreground === 'neighbour') {
    neighbour = await context.newPage();
    if (neighbourUrl) await neighbour.goto(neighbourUrl, { waitUntil: 'load' });
    else await neighbour.setContent(NEIGHBOUR_PAGE, { waitUntil: 'load' });
  }

  const front = foreground === 'neighbour' && neighbour ? neighbour : dashboard;
  if (front) await front.bringToFront();

  await (front || neighbour).waitForTimeout(settleMs);
  for (const page of [dashboard, neighbour]) {
    if (page) await page.evaluate(RESET);
  }
  await (front || neighbour).waitForTimeout(durationMs);

  const rasterisation = await (dashboard || neighbour).evaluate(RENDERER_PROBE);
  const result = {
    config: {
      url, view, animation, transport, durationMs, browser, gpu, software,
      deepReads, cpuThrottle, foreground, dashboardOpen,
      neighbour: Boolean(neighbour), extraJsResult,
    },
    rasterisation,
    dashboard: dashboard ? await dashboard.evaluate(SUMMARIZE) : null,
    neighbour: neighbour ? await neighbour.evaluate(SUMMARIZE) : null,
  };

  await context.close();
  await instance.close();
  process.stdout.write(JSON.stringify(result));
}

main().catch((error) => {
  process.stderr.write(String(error && error.stack ? error.stack : error));
  process.exit(1);
});
