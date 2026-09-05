// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Painted-area probe for the energy pipe study (Chromium only).
//
// At the display's refresh ceiling every candidate reports the same frame rate,
// so the question "does a bigger texture or a bigger layer cost anything?"
// cannot be answered with fps. This reads the compositor's own accounting
// instead: how many layers exist, how large they are, how often each was
// painted, and how much style/layout work the page did.
//
// Firefox exposes no equivalent, and this file does not guess one -- the report
// records those cells as unavailable rather than inferring them.
//
//   node scripts/flow_pipe_study/pipe_layers.mjs <baseUrl> [headed]

import { chromium } from 'playwright';

const base = process.argv[2];
const headed = process.argv[3] === 'headed';

const CASES = [
  { label: 'capsule', query: {} },
  { label: 'capsule/tile4', query: { tile: '4' } },
  { label: 'capsule/tile16', query: { tile: '16' } },
  { label: 'capsule/pad24', query: { pad: '24' } },
  { label: 'capsule/pad72', query: { pad: '72' } },
  { label: 'capsule/rich', query: { texture: 'rich' } },
  { label: 'rect', query: { candidate: 'rect' } },
  { label: 'radius-el', query: { candidate: 'radius-el' } },
  { label: 'gradient-capsule', query: { candidate: 'gradient-capsule' } },
  { label: 'repeating', query: { candidate: 'repeating' } },
  { label: 'tokens/1', query: { candidate: 'tokens', tokens: '1' } },
  { label: 'tokens/4', query: { candidate: 'tokens', tokens: '4' } },
  { label: 'tokens/8', query: { candidate: 'tokens', tokens: '8' } },
  { label: 'core', query: { candidate: 'core' } },
  { label: 'pulse', query: { candidate: 'pulse' } },
  { label: 'minimal', query: { candidate: 'minimal' } },
  { label: 'arrow', query: { candidate: 'arrow' } },
  { label: 'plasma', query: { candidate: 'plasma' } },
  { label: 'comet', query: { candidate: 'comet' } },
  { label: 'particles', query: { candidate: 'particles' } },
  { label: 'wave', query: { candidate: 'wave' } },
  { label: 'capsule/glow-texture', query: { glow: 'texture' } },
  { label: 'capsule/glow-filter', query: { glow: 'filter' } },
  { label: 'capsule/glow-blend', query: { glow: 'blend' } },
  { label: 'capsule/glow-layered', query: { glow: 'layered' } },
  { label: 'capsule/still', query: { motion: 'off' } },
];

function url(query) {
  const params = new URLSearchParams({
    candidate: 'capsule',
    scenario: 'aggregate',
    flows: '12',
    watts: 'mixed',
    speeds: 'single',
    motion: 'on',
    tokens: '4',
    tile: '1',
    pad: '0',
    texture: 'simple',
    glow: 'none',
    ...query,
  });
  return `${base}/index.html?${params}`;
}

const instance = await chromium.launch(
  headed ? { headless: false } : { headless: true },
);
const context = await instance.newContext({ viewport: { width: 1440, height: 900 } });

const results = [];
for (const spec of CASES) {
  const page = await context.newPage();
  const session = await context.newCDPSession(page);
  let latest = null;
  session.on('LayerTree.layerTreeDidChange', ({ layers }) => {
    if (layers && layers.length) latest = layers;
  });
  await session.send('LayerTree.enable');
  await session.send('Performance.enable');
  await page.goto(url(spec.query), { waitUntil: 'load' });
  await page.waitForFunction(() => window.__lab && window.__lab.ready, null, { timeout: 20000 });
  await page.waitForTimeout(2500);

  const metrics = await session.send('Performance.getMetrics');
  const metric = (name) => {
    const found = metrics.metrics.find((m) => m.name === name);
    return found ? found.value : null;
  };
  const dom = await session.send('Memory.getDOMCounters').catch(() => null);
  const lab = await page.evaluate(() => window.__lab);

  const layers = latest || [];
  const content = layers.filter((l) => l.drawsContent);
  const area = content.reduce((sum, l) => sum + l.width * l.height, 0);
  const paintCount = content.reduce((sum, l) => sum + (l.paintCount || 0), 0);
  const biggest = content
    .slice()
    .sort((a, b) => b.width * b.height - a.width * a.height)
    .slice(0, 3)
    .map((l) => ({ w: l.width, h: l.height, paintCount: l.paintCount }));

  results.push({
    label: spec.label,
    animatedElements: lab.animatedElements,
    paintedElements: lab.paintedElements,
    cssAnimations: lab.cssAnimations,
    stagePx: lab.stagePx,
    layers: layers.length,
    contentLayers: content.length,
    contentLayerPx: area,
    contentLayerMb: Number(((area * 4) / (1024 * 1024)).toFixed(2)),
    layerPaintCount: paintCount,
    biggestLayers: biggest,
    recalcStyleCount: metric('RecalcStyleCount'),
    layoutCount: metric('LayoutCount'),
    taskDuration: metric('TaskDuration'),
    scriptDuration: metric('ScriptDuration'),
    layoutDuration: metric('LayoutDuration'),
    recalcStyleDuration: metric('RecalcStyleDuration'),
    nodes: dom ? dom.nodes : null,
  });
  process.stderr.write(
    `  ${spec.label.padEnd(20)} layers ${String(content.length).padStart(4)} `
    + `px ${String(area).padStart(10)} paints ${String(paintCount).padStart(5)}\n`,
  );
  await session.detach().catch(() => {});
  await page.close();
}

await context.close();
await instance.close();

process.stdout.write(JSON.stringify({
  kind: 'energy-pipe-layers',
  browser: 'chromium',
  mode: headed ? 'headed' : 'headless',
  note: 'Chromium compositor accounting. Firefox exposes no equivalent; those cells are unavailable, not inferred.',
  flows: 12,
  results,
}, null, 2) + '\n');
