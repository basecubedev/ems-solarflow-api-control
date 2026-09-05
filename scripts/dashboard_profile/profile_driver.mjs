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
  cdpMetrics = false,
  trace = false,
  cycleViews = null,
  cycleIntervalMs = 2000,
  sampleMs = 0,
  gc = false,
  compositorProbe = null,
} = config;

// Chromium's own counters for the two stages the page cannot see from inside:
// style recalculation and layout. Taken as a delta across the measurement
// window, so what is reported is the work of this run and not of the load.
const CDP_METRICS = [
  'RecalcStyleCount', 'RecalcStyleDuration',
  'LayoutCount', 'LayoutDuration',
  'ScriptDuration', 'TaskDuration',
  'Nodes', 'JSEventListeners', 'Documents', 'JSHeapUsedSize',
];

// Names the renderer uses for the stages after script. Paint and RasterTask
// being zero is what separates a compositor-carried animation from one that
// repaints; Commit counts the frames the compositor actually took.
const TRACE_CATEGORIES = [
  'blink', 'cc', 'devtools.timeline',
  'disabled-by-default-devtools.timeline',
  'disabled-by-default-devtools.timeline.frame',
  'blink.animations',
  'disabled-by-default-blink.animations',
];

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
    // Cumulative for the lifetime of the page, deliberately not cleared by
    // RESET: a leak is a count that keeps climbing across view changes, which
    // is invisible if the counter restarts with every measurement window.
    live: {
      listeners: Object.create(null),
      listenerAdds: 0,
      listenerRemoves: 0,
      intervalsCreated: 0,
      intervalsCleared: 0,
      observersCreated: Object.create(null),
      observersDisconnected: Object.create(null),
      eventSourcesOpened: 0,
      eventSourcesClosed: 0,
    },
    samples: [],
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
  const countListener = (type, delta) => {
    bench.live.listeners[type] = (bench.live.listeners[type] || 0) + delta;
    if (delta > 0) bench.live.listenerAdds += 1;
    else bench.live.listenerRemoves += 1;
  };
  EventTarget.prototype.addEventListener = function (type, listener, options) {
    countListener(type, 1);
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
    countListener(type, -1);
    const wrapped = typeof listener === 'function' ? listenerWrappers.get(listener) : null;
    return originalRemove.call(this, type, wrapped || listener, options);
  };

  // EventSource handlers are usually assigned, not added, so the accessor has
  // to be patched too or the whole snapshot path is invisible.
  if (window.EventSource) {
    const RawEventSource = window.EventSource;
    const PatchedEventSource = function (...args) {
      bench.live.eventSourcesOpened += 1;
      return new RawEventSource(...args);
    };
    PatchedEventSource.prototype = RawEventSource.prototype;
    for (const key of ['CONNECTING', 'OPEN', 'CLOSED']) {
      PatchedEventSource[key] = RawEventSource[key];
    }
    const rawClose = RawEventSource.prototype.close;
    if (typeof rawClose === 'function') {
      RawEventSource.prototype.close = function (...rest) {
        bench.live.eventSourcesClosed += 1;
        return rawClose.apply(this, rest);
      };
    }
    window.EventSource = PatchedEventSource;
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
    bench.live.intervalsCreated += 1;
    return rawSetInterval(typeof fn === 'function' ? wrap('setInterval', fn) : fn, ms, ...rest);
  };
  const rawClearInterval = window.clearInterval.bind(window);
  window.clearInterval = function (handle) {
    bench.live.intervalsCleared += 1;
    return rawClearInterval(handle);
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
      bench.live.observersCreated[name] = (bench.live.observersCreated[name] || 0) + 1;
      return new Original(wrap(name, callback), ...rest);
    };
    Patched.prototype = Original.prototype;
    const rawDisconnect = Original.prototype.disconnect;
    if (typeof rawDisconnect === 'function') {
      Original.prototype.disconnect = function (...rest) {
        bench.live.observersDisconnected[name] =
          (bench.live.observersDisconnected[name] || 0) + 1;
        return rawDisconnect.apply(this, rest);
      };
    }
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

  // The samplers below run for as long as the page does. Bounded, because a
  // thirty-minute run at 144 Hz and 250 Hz would otherwise accumulate hundreds
  // of thousands of numbers inside the page being measured.
  const SAMPLE_CAP = 20000;
  const record = (into, value) => {
    if (into.length >= SAMPLE_CAP) into.shift();
    into.push(value);
  };

  if (rawRaf) {
    let previous = now();
    const frame = (stamp) => {
      record(bench.frameDeltas, stamp - previous);
      previous = stamp;
      rawRaf(frame);
    };
    rawRaf(frame);
  }

  const step = 50;
  let expected = now() + step;
  rawSetInterval(() => {
    const stamp = now();
    record(bench.lags, Math.max(0, stamp - expected));
    expected = stamp + step;
  }, step);

  // A self-rescheduling zero timeout runs at roughly 250 Hz once nested, so a
  // gap much larger than its clamp is a task that held the thread. This is the
  // engine-neutral stand-in for the long-task observer Firefox does not have.
  let lastTick = now();
  const tick = () => {
    const stamp = now();
    const gap = stamp - lastTick;
    record(bench.taskGaps, gap);
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
  // Not Math.max(...values): that passes every element as an argument, and a
  // thirty-minute run holds tens of thousands of them.
  const largest = (values) => {
    let top = null;
    for (const value of values) if (top === null || value > top) top = value;
    return top;
  };
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
    lagMaxMs: largest(bench.lags),
    taskGapP95Ms: quantile(gaps, 0.95),
    taskGapMaxMs: largest(gaps),
    blockingTasks: blocking.length,
    blockingMs: Number(blocking.reduce((a, b) => a + b, 0).toFixed(1)),
    longTasks: bench.longTasks.length,
    longTaskTotalMs: Number(bench.longTasks.reduce((a, b) => a + b, 0).toFixed(1)),
    mutations: bench.mutations,
    mutationTargets: targets,
    fetches: bench.fetches,
    fetchWallMs: Number(bench.fetchWallMs.toFixed(1)),
    domNodes: document.getElementsByTagName('*').length,
    domNodesByView: (() => {
      const per = {};
      for (const id of ['flowSvg', 'deviceFlowView', 'controlExplainView',
        'energyStatsView', 'analyticsView', 'diagnoseView', 'logsView',
        'maintenanceView', 'deviceGrid', 'rulesList']) {
        const el = document.getElementById(id);
        per[id] = el ? el.getElementsByTagName('*').length : null;
      }
      return per;
    })(),
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
    live: {
      listenerAdds: bench.live.listenerAdds,
      listenerRemoves: bench.live.listenerRemoves,
      listenersOutstanding: Object.entries(bench.live.listeners)
        .filter(([, n]) => n !== 0)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 12)
        .map(([type, count]) => ({ type, count })),
      intervalsCreated: bench.live.intervalsCreated,
      intervalsCleared: bench.live.intervalsCleared,
      observersCreated: { ...bench.live.observersCreated },
      observersDisconnected: { ...bench.live.observersDisconnected },
      eventSourcesOpened: bench.live.eventSourcesOpened,
      eventSourcesClosed: bench.live.eventSourcesClosed,
    },
    samples: bench.samples,
  };
};

// Taken repeatedly during a long run. Everything here is a level, not a rate:
// a leak shows as a level that never comes back down, which a single
// before/after pair cannot distinguish from a page that simply grew once.
const SAMPLE = () => {
  const bench = window.__prof;
  const sample = {
    atMs: Number((performance.now() - bench.started).toFixed(0)),
    domNodes: document.getElementsByTagName('*').length,
    controlNodes: (() => {
      const el = document.getElementById('controlExplainView');
      return el ? el.getElementsByTagName('*').length : null;
    })(),
    listenersOutstanding: bench.live.listenerAdds - bench.live.listenerRemoves,
    intervalsOutstanding: bench.live.intervalsCreated - bench.live.intervalsCleared,
    observersCreated: Object.values(bench.live.observersCreated)
      .reduce((a, b) => a + b, 0),
    eventSourcesOutstanding:
      bench.live.eventSourcesOpened - bench.live.eventSourcesClosed,
    mutations: bench.mutations,
    animationsRunning: (() => {
      try { return document.getAnimations ? document.getAnimations().length : null; }
      catch { return null; }
    })(),
    heapMb: performance.memory && performance.memory.usedJSHeapSize
      ? Number((performance.memory.usedJSHeapSize / (1024 * 1024)).toFixed(1))
      : null,
  };
  bench.samples.push(sample);
  return sample;
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

// The decisive question about an animation, asked directly: does it keep moving
// while the main thread cannot run? Only the compositor can do that. Named as
// the experiment that would settle it in the pipe study's remaining
// uncertainties, and never run there -- and never on the real dashboard.
const COMPOSITOR_PROBE = (selectors) => {
  // Every selector is read on both sides of ONE block, so a control injected by
  // the treatment is measured under exactly the conditions the real element is.
  // Without a control this probe cannot tell "not composited" from "composited
  // and unreadable while the main thread is stopped" -- which is the calibration
  // the pipe study said this experiment needed.
  const wanted = String(selectors).split(',').map((s) => s.trim()).filter(Boolean);
  const targets = wanted.map((selector) => ({
    selector, node: document.querySelector(selector),
  }));
  const read = (node) => {
    if (!node) return null;
    const value = getComputedStyle(node).transform;
    const match = /matrix\(([^)]*)\)/.exec(value);
    return match ? Number(match[1].split(',')[4]) : null;
  };
  const before = targets.map((t) => read(t.node));
  const startedAt = performance.now();
  // A busy wait, deliberately: a timer would let the main thread run.
  while (performance.now() - startedAt < 600) { /* hold the thread */ }
  const after = targets.map((t) => read(t.node));
  const blockedMs = Number((performance.now() - startedAt).toFixed(0));
  return targets.map((t, index) => ({
    selector: t.selector,
    found: Boolean(t.node),
    before: before[index],
    after: after[index],
    blockedMs,
    movedWhileBlocked:
      before[index] !== null && after[index] !== null
      && before[index] !== after[index],
  }));
};

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

  const readMetrics = async (session) => {
    if (!session) return null;
    const { metrics } = await session.send('Performance.getMetrics');
    const out = {};
    for (const metric of metrics) {
      if (CDP_METRICS.includes(metric.name)) out[metric.name] = metric.value;
    }
    return out;
  };
  const diffMetrics = (before, after) => {
    if (!before || !after) return null;
    const out = {};
    for (const key of Object.keys(after)) {
      // Levels stay levels; only the counters and the accumulating durations
      // are meaningful as a difference.
      out[key] = ['Nodes', 'JSEventListeners', 'Documents', 'JSHeapUsedSize'].includes(key)
        ? { before: before[key], after: after[key], delta: after[key] - before[key] }
        : Number(((after[key] || 0) - (before[key] || 0)).toFixed(4));
    }
    return out;
  };

  let extraJsResult = null;
  let dashboard = null;
  if (dashboardOpen) {
    dashboard = await context.newPage();
    await dashboard.goto(url, { waitUntil: 'load', timeout: navigationTimeoutMs });
    // The steady state of a real dashboard, reached deterministically. The
    // control panel is built by the auth and runtime fetches rather than by the
    // view, so whether it exists is a race between those and the first
    // snapshot -- and it is worth ~1350 nodes at four devices, which made the
    // same scenario report two different DOM sizes run to run.
    await dashboard
      .waitForFunction(() => {
        const slot = window.__prof && window.__prof.work['listener:telemetry'];
        return Boolean(slot && slot.calls >= 1);
      }, null, { timeout: 30000 })
      .catch(() => {});
    // Whether the control panel exists is decided by a race between the boot
    // fetches and the first snapshot, and it is worth ~1350 nodes at four
    // devices -- enough to make one scenario report two different DOM sizes.
    // The auth refresh runs on a sixty-second interval and builds it either
    // way, so calling it once puts the page in the state it reaches on its own
    // within a minute, instead of waiting one out per case.
    await dashboard.evaluate(() => {
      if (typeof loadAuthStatus === 'function') loadAuthStatus();
    });
    await dashboard
      .waitForFunction(() => Boolean(document.getElementById('controlExplainMount')),
        null, { timeout: 15000 })
      .catch(() => {});
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

  const target = dashboard || neighbour;
  let session = null;
  if ((cdpMetrics || trace || gc) && browser === 'chromium' && target) {
    session = await context.newCDPSession(target);
    if (cdpMetrics) await session.send('Performance.enable');
  }
  const collectGarbage = async () => {
    if (!session || !gc) return;
    await session.send('HeapProfiler.collectGarbage').catch(() => {});
  };
  await collectGarbage();

  for (const page of [dashboard, neighbour]) {
    if (page) await page.evaluate(RESET);
  }
  const metricsBefore = cdpMetrics ? await readMetrics(session) : null;

  const traceEvents = [];
  let traceDone = null;
  if (trace && session) {
    session.on('Tracing.dataCollected', ({ value }) => traceEvents.push(...value));
    traceDone = new Promise((resolve) => session.once('Tracing.tracingComplete', resolve));
    await session.send('Tracing.start', {
      traceConfig: { includedCategories: TRACE_CATEGORIES, recordMode: 'recordAsMuchAsPossible' },
      transferMode: 'ReportEvents',
    });
  }

  // The measurement window. A plain wait when nothing else is asked for, so the
  // existing matrices behave exactly as before; otherwise a stepped wait that
  // rotates the view and takes levels as it goes.
  const cycle = Array.isArray(cycleViews) && cycleViews.length ? cycleViews : null;
  const viewChanges = [];
  const samples = [];
  if (!cycle && !sampleMs) {
    await (front || neighbour).waitForTimeout(durationMs);
  } else {
    const step = Math.max(50, Math.min(sampleMs || cycleIntervalMs, cycleIntervalMs || durationMs));
    let waited = 0;
    let nextCycleAt = cycle ? cycleIntervalMs : Infinity;
    let nextSampleAt = sampleMs || Infinity;
    let cycleIndex = 0;
    while (waited < durationMs) {
      const slice = Math.min(step, durationMs - waited);
      await (front || neighbour).waitForTimeout(slice);
      waited += slice;
      if (cycle && dashboard && waited >= nextCycleAt) {
        const next = cycle[cycleIndex % cycle.length];
        cycleIndex += 1;
        const changed = await dashboard.evaluate((wanted) => {
          if (typeof setFlowView !== 'function') return null;
          const startedAt = performance.now();
          setFlowView(wanted, false);
          return Number((performance.now() - startedAt).toFixed(1));
        }, next);
        viewChanges.push({ atMs: waited, view: next, switchMs: changed });
        nextCycleAt = waited + cycleIntervalMs;
      }
      if (sampleMs && waited >= nextSampleAt) {
        await collectGarbage();
        const sample = await (dashboard || neighbour).evaluate(SAMPLE);
        if (session && cdpMetrics) sample.engine = await readMetrics(session);
        samples.push(sample);
        nextSampleAt = waited + sampleMs;
      }
    }
  }

  let traceSummary = null;
  if (trace && session) {
    await session.send('Tracing.end');
    await traceDone;
    const counts = {};
    const durations = {};
    // Why an animation is not on the compositor, in the renderer's own words.
    // Without this the answer is an inference from a number going down.
    const compositeFailures = new Set();
    for (const event of traceEvents) {
      if (event.ph !== 'X' && event.ph !== 'B') continue;
      counts[event.name] = (counts[event.name] || 0) + 1;
      if (typeof event.dur === 'number') {
        durations[event.name] = (durations[event.name] || 0) + event.dur / 1000;
      }
      const data = event.args && (event.args.data || event.args);
      if (data && data.compositeFailed !== undefined) {
        compositeFailures.add('compositeFailed=' + String(data.compositeFailed));
      }
      if (data && data.unsupportedProperties) {
        compositeFailures.add(JSON.stringify(data.unsupportedProperties));
      }
    }
    const pick = (name) => ({
      count: counts[name] || 0,
      ms: Number((durations[name] || 0).toFixed(1)),
    });
    traceSummary = {
      frames: counts.Commit || counts.DrawFrame || 0,
      updateLayoutTree: pick('UpdateLayoutTree'),
      layout: pick('Layout'),
      prePaint: pick('PrePaint'),
      paint: pick('Paint'),
      rasterTask: pick('RasterTask'),
      commit: pick('Commit'),
      compositeFailures: [...compositeFailures],
      note: 'UpdateLayoutTree counts an animation tick as well as an '
        + 'invalidation; read it against RecalcStyleCount from '
        + 'Performance.getMetrics before calling it a style recalculation.',
    };
  }
  const metricsAfter = cdpMetrics ? await readMetrics(session) : null;
  await collectGarbage();
  const metricsSettled = cdpMetrics && gc ? await readMetrics(session) : null;

  const compositor = compositorProbe
    ? await (dashboard || neighbour).evaluate(COMPOSITOR_PROBE, compositorProbe)
    : null;
  const rasterisation = await (dashboard || neighbour).evaluate(RENDERER_PROBE);
  const result = {
    config: {
      url, view, animation, transport, durationMs, browser, gpu, software,
      deepReads, cpuThrottle, foreground, dashboardOpen,
      cdpMetrics, trace, cycleViews, cycleIntervalMs, sampleMs, gc, compositorProbe,
      neighbour: Boolean(neighbour), extraJsResult,
    },
    rasterisation,
    compositor,
    engine: diffMetrics(metricsBefore, metricsAfter),
    engineAfterGc: metricsSettled,
    trace: traceSummary,
    viewChanges: viewChanges.length ? viewChanges : null,
    samples: samples.length ? samples : null,
    dashboard: dashboard ? await dashboard.evaluate(SUMMARIZE) : null,
    neighbour: neighbour ? await neighbour.evaluate(SUMMARIZE) : null,
  };
  if (session) await session.detach().catch(() => {});

  await context.close();
  await instance.close();
  process.stdout.write(JSON.stringify(result));
}

main().catch((error) => {
  process.stderr.write(String(error && error.stack ? error.stack : error));
  process.exit(1);
});
