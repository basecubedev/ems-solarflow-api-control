// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Is the transform animation actually on the compositor?
//
// The pipe study's trace counts one style recalculation per frame for an
// animation that should never touch the main thread, and switching to literal
// keyframes halves the cost without removing the count. Before any of that can
// be read as "not composited", the signal itself has to be calibrated: a bare
// div doing nothing but a transform is the control, and if that also produces a
// recalculation per frame then the counter measures bookkeeping rather than
// main-thread animation.
//
// Two independent signals per variant:
//   - style recalculations per frame, from the trace
//   - whether the layer keeps moving while the main thread is blocked, which
//     only a compositor-driven animation can do
//
//   node scripts/flow_pipe_study/pipe_composited.mjs [headed]

import { chromium } from 'playwright';

const headed = process.argv[2] === 'headed';
const COUNT = 36;
const DURATION_MS = 4000;

const VARIANTS = [
  { id: 'bare-css-literal', label: 'plain div, CSS keyframes, literal px' },
  { id: 'bare-css-var', label: 'plain div, CSS keyframes, var()' },
  { id: 'bare-waapi', label: 'plain div, Web Animations API' },
  { id: 'clipped-css-var', label: 'inside overflow:hidden, CSS keyframes, var()' },
  { id: 'clipped-waapi', label: 'inside overflow:hidden, Web Animations API' },
  { id: 'bg-waapi', label: 'background-image tile, Web Animations API' },
  { id: 'bg-css-var', label: 'background-image tile, CSS keyframes, var()' },
  { id: 'opacity-waapi', label: 'opacity instead of transform, Web Animations API' },
  { id: 'none', label: 'nothing animated (floor)' },
];

const PAGE = (variant, count) => `<!doctype html><meta charset="utf-8">
<style>
  body { margin:0; background:#04101c; }
  .clip { position:absolute; overflow:hidden; width:300px; height:20px; }
  .m { position:absolute; width:300px; height:20px; background:#38d5ff;
       will-change: transform; }
  .m.bg { background: linear-gradient(to right,#38d5ff 0 34px,rgba(56,213,255,0) 34px 52px) 0 0/52px 20px repeat-x; }
  .lit { animation: litMove 1.4s linear infinite; }
  .vr  { animation: varMove 1.4s linear infinite; }
  @keyframes litMove { to { transform: translate3d(52px,0,0); } }
  @keyframes varMove { to { transform: translate3d(var(--step),0,0); } }
</style>
<div id="host"></div>
<script>
const variant = ${JSON.stringify(variant)};
const count = ${count};
const host = document.getElementById('host');
for (let i = 0; i < count; i += 1) {
  const y = 4 + i * 22;
  let target;
  if (variant.startsWith('clipped')) {
    const clip = document.createElement('div');
    clip.className = 'clip';
    clip.style.top = y + 'px';
    clip.style.left = '10px';
    host.appendChild(clip);
    target = document.createElement('div');
    target.className = 'm';
    clip.appendChild(target);
  } else {
    target = document.createElement('div');
    target.className = 'm';
    target.style.top = y + 'px';
    target.style.left = '10px';
    host.appendChild(target);
  }
  if (variant.startsWith('bg')) target.classList.add('bg');
  target.style.setProperty('--step', '52px');
  if (variant.endsWith('css-literal')) target.classList.add('lit');
  else if (variant.endsWith('css-var')) target.classList.add('vr');
  else if (variant === 'opacity-waapi') {
    target.animate([{ opacity: 1 }, { opacity: 0.2 }],
      { duration: 1400, iterations: Infinity, easing: 'linear' });
  } else if (variant.endsWith('waapi')) {
    target.animate(
      [{ transform: 'translate3d(0px,0,0)' }, { transform: 'translate3d(52px,0,0)' }],
      { duration: 1400, iterations: Infinity, easing: 'linear' });
  }
}
window.__ready = true;
window.__block = (ms) => { const t = performance.now(); while (performance.now() - t < ms) {} };
</script>`;

const CATEGORIES = [
  'blink', 'cc', 'devtools.timeline',
  'disabled-by-default-devtools.timeline',
  'disabled-by-default-devtools.timeline.frame',
  'blink.animations',
  'disabled-by-default-blink.animations',
];

const instance = await chromium.launch(headed ? { headless: false } : { headless: true });
const context = await instance.newContext({ viewport: { width: 1280, height: 900 } });

const results = [];
for (const variant of VARIANTS) {
  const page = await context.newPage();
  await page.setContent(PAGE(variant.id, COUNT), { waitUntil: 'load' });
  await page.waitForFunction(() => window.__ready, null, { timeout: 10000 });
  await page.waitForTimeout(700);

  const session = await context.newCDPSession(page);
  const events = [];
  session.on('Tracing.dataCollected', ({ value }) => events.push(...value));
  const done = new Promise((resolve) => session.once('Tracing.tracingComplete', resolve));
  await session.send('Tracing.start', {
    traceConfig: { includedCategories: CATEGORIES, recordMode: 'recordAsMuchAsPossible' },
    transferMode: 'ReportEvents',
  });
  await page.waitForTimeout(DURATION_MS);
  await session.send('Tracing.end');
  await done;

  const counts = {};
  const durations = {};
  const compositeFailures = new Set();
  for (const event of events) {
    if (event.ph !== 'X' && event.ph !== 'B') continue;
    counts[event.name] = (counts[event.name] || 0) + 1;
    if (typeof event.dur === 'number') {
      durations[event.name] = (durations[event.name] || 0) + event.dur / 1000;
    }
    const data = event.args && (event.args.data || event.args);
    if (data && data.compositeFailed !== undefined) {
      compositeFailures.add(String(data.compositeFailed));
    }
    if (data && data.unsupportedProperties) {
      compositeFailures.add(JSON.stringify(data.unsupportedProperties));
    }
  }

  // The decisive one. A compositor-driven animation keeps moving while the main
  // thread is wedged; a main-thread one cannot.
  const moved = await page.evaluate(async () => {
    const target = document.querySelector('.m');
    if (!target) return null;
    const read = () => {
      const value = getComputedStyle(target).transform;
      const match = /matrix\(([^)]*)\)/.exec(value);
      return match ? Number(match[1].split(',')[4]) : null;
    };
    const before = read();
    const stamp = performance.now();
    window.__block(600);
    const after = read();
    return { before, after, blockedMs: performance.now() - stamp };
  });

  await session.detach().catch(() => {});
  await page.close();

  const frames = counts.Commit || counts.DrawFrame || 0;
  results.push({
    variant: variant.id,
    label: variant.label,
    animatedElements: variant.id === 'none' ? 0 : COUNT,
    frames,
    styleRecalcs: counts.UpdateLayoutTree || 0,
    styleMs: Number((durations.UpdateLayoutTree || 0).toFixed(1)),
    styleRecalcsPerFrame: frames ? Number(((counts.UpdateLayoutTree || 0) / frames).toFixed(3)) : null,
    paints: counts.Paint || 0,
    rasterTasks: counts.RasterTask || 0,
    compositeFailureCodes: [...compositeFailures],
    duringMainThreadBlock: moved,
  });
  process.stderr.write(
    `  ${variant.id.padEnd(18)} style/frame ${String(results[results.length - 1].styleRecalcsPerFrame).padStart(6)}`
    + `  styleMs ${String(results[results.length - 1].styleMs).padStart(7)}\n`,
  );
}

await context.close();
await instance.close();
process.stdout.write(JSON.stringify({
  kind: 'energy-pipe-composited',
  browser: 'chromium',
  mode: headed ? 'headed' : 'headless',
  note: 'Calibration for the style-recalculation signal used in the pipe study.',
  animatedElements: COUNT,
  durationMs: DURATION_MS,
  results,
}, null, 2) + '\n');
