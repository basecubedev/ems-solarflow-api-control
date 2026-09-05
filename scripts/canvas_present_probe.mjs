// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Does a canvas that presents once per frame cost the real dashboard anything?
//
// In the isolated lab a WebGL renderer is the fastest technique measured: flat
// 60 fps from 12 to 100 flows in both engines, one draw call, sub-millisecond
// main-thread lag. On the real dashboard in HEADLESS Firefox the same loop
// drops the page to 2-5 fps whatever the API, size, placement or stacking.
//
// That result cannot be trusted on its own, because headless Firefox does not
// GPU-composite page content on this host -- it reports an NVIDIA device for
// WebGL while compositing the page on the CPU. A canvas presenting into a
// software-composited page is exactly the case that would collapse for reasons
// that say nothing about real hardware. This probe runs the same experiment
// across every rasterisation path so the two explanations can be told apart.
//
// A draw counter is reported beside the frame rate: a loop that stopped running
// would otherwise look like a loop that costs nothing, which is the specific
// error this project has made before.
//
//   node scripts/canvas_present_probe.mjs <url> [seconds]

import { chromium, firefox } from 'playwright';

const url = process.argv[2];
const seconds = Number(process.argv[3] || 6);

const INSTALL = (kind) => {
  window.__probe = { draws: 0, frames: 0, started: performance.now() };
  const tick = () => {
    window.__probe.frames += 1;
    window.__probe.raf = requestAnimationFrame(tick);
  };
  window.__probe.raf = requestAnimationFrame(tick);
  if (kind === 'none') return { ok: true };

  const svg = document.getElementById('flowSvg');
  const host = (svg && svg.parentNode) || document.body;
  const rect = svg ? svg.getBoundingClientRect() : { width: 400, height: 200 };
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(rect.width));
  canvas.height = Math.max(1, Math.round(rect.height));
  canvas.style.cssText =
    `position:absolute;left:0;top:0;pointer-events:none;width:${rect.width}px;` +
    `height:${rect.height}px;background:none;border:none;box-shadow:none;min-height:0`;
  host.appendChild(canvas);

  if (kind === 'canvas2d') {
    const ctx = canvas.getContext('2d');
    if (!ctx) return { error: 'no 2d' };
    const loop = () => {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = 'rgba(56,213,255,0.5)';
      ctx.fillRect((window.__probe.draws * 3) % canvas.width, 10, 40, 4);
      window.__probe.draws += 1;
      requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
    return { ok: true };
  }

  const gl = canvas.getContext('webgl2', { alpha: true, premultipliedAlpha: true });
  if (!gl) return { error: 'no webgl2' };
  const loop = () => {
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    window.__probe.draws += 1;
    requestAnimationFrame(loop);
  };
  requestAnimationFrame(loop);
  return { ok: true };
};

function launchOptions(engineName, mode) {
  if (mode === 'headed') return { headless: false };
  if (mode === 'gpu' && engineName === 'chromium') {
    return {
      headless: true,
      args: ['--use-gl=angle', '--use-angle=gl', '--enable-gpu', '--ignore-gpu-blocklist'],
    };
  }
  return { headless: true };
}

const CASES = [
  ['chromium', chromium, 'software'],
  ['chromium', chromium, 'gpu'],
  ['chromium', chromium, 'headed'],
  ['firefox', firefox, 'software'],
  ['firefox', firefox, 'headed'],
];

const out = [];
for (const [name, engine, mode] of CASES) {
  let browser;
  try {
    browser = await engine.launch({ ...launchOptions(name, mode), timeout: 60000 });
  } catch (error) {
    out.push({ browser: name, path: mode, error: String(error).split('\n')[0] });
    continue;
  }
  for (const kind of ['none', 'canvas2d', 'webgl']) {
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await context.newPage();
    try {
      await page.goto(url, { waitUntil: 'load', timeout: 60000 });
      await page.waitForTimeout(2500);
      const installed = await page.evaluate(INSTALL, kind);
      if (installed.error) {
        out.push({ browser: name, path: mode, kind, error: installed.error });
        await context.close();
        continue;
      }
      await page.waitForTimeout(500);
      const before = await page.evaluate(() => ({ ...window.__probe, at: performance.now() }));
      await page.waitForTimeout(seconds * 1000);
      const after = await page.evaluate(() => ({ ...window.__probe, at: performance.now() }));
      const elapsed = (after.at - before.at) / 1000;
      const renderer = await page.evaluate(() => {
        try {
          const c = document.createElement('canvas');
          const g = c.getContext('webgl2') || c.getContext('webgl');
          const d = g && g.getExtension('WEBGL_debug_renderer_info');
          return d ? g.getParameter(d.UNMASKED_RENDERER_WEBGL) : null;
        } catch { return null; }
      });
      out.push({
        browser: name,
        path: mode,
        kind,
        fps: +((after.frames - before.frames) / elapsed).toFixed(1),
        drawsPerSecond: +((after.draws - before.draws) / elapsed).toFixed(1),
        renderer,
      });
    } catch (error) {
      out.push({ browser: name, path: mode, kind, error: String(error).split('\n')[0] });
    }
    await context.close();
  }
  await browser.close();
}
process.stdout.write(JSON.stringify({ url, seconds, results: out }, null, 2) + '\n');
