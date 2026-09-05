// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Verifies the flow lab before anything is measured with it: every renderer
// must build without a page error, and consecutive screenshots must differ --
// otherwise the "animation" under test is not running and the benchmark would
// be measuring nothing. A previous investigation reported a 13x win from an
// experiment that never executed; this is the guard against repeating it.
//
//   node scripts/flow_lab_verify.mjs <baseUrl> <screenshotDir> [browser]

import { chromium, firefox } from 'playwright';
import { mkdirSync } from 'node:fs';

// Each entry is a label and the query that selects it. The metaphors are all
// the same mechanism as dom-tiles, but a metaphor whose background never moves
// would benchmark as gloriously fast, so each one is proved to move here
// before any number taken from it is believed.
const RENDERERS = [
  ['dashoffset', 'renderer=dashoffset'],
  ['svg-transform', 'renderer=svg-transform'],
  ['svg-pattern', 'renderer=svg-pattern'],
  ['svg-mask', 'renderer=svg-mask'],
  ['dom-tiles', 'renderer=dom-tiles'],
  ['motion-path', 'renderer=motion-path'],
  ['canvas', 'renderer=canvas'],
  ['canvas-bloom', 'renderer=canvas-bloom'],
  ['canvas-worker', 'renderer=canvas-worker'],
  ['webgl', 'renderer=webgl'],
  ['tiles-capsule', 'renderer=dom-tiles&metaphor=capsule'],
  ['tiles-particles', 'renderer=dom-tiles&metaphor=particles'],
  ['tiles-comet', 'renderer=dom-tiles&metaphor=comet'],
  ['tiles-chevron', 'renderer=dom-tiles&metaphor=chevron'],
  ['tiles-pulse', 'renderer=dom-tiles&metaphor=pulse'],
  ['tiles-sweep', 'renderer=dom-tiles&metaphor=sweep'],
  ['none', 'renderer=none'],
];

const base = process.argv[2];
const outDir = process.argv[3];
const engineName = process.argv[4] || 'chromium';
const flows = process.argv[5] || '4';
const engine = engineName === 'firefox' ? firefox : chromium;

mkdirSync(outDir, { recursive: true });

const instance = await engine.launch({ headless: true });
const context = await instance.newContext({ viewport: { width: 1440, height: 900 } });
const report = [];

for (const [renderer, query] of RENDERERS) {
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(String(error)));
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  await page.goto(
    `${base}/index.html?${query}&flows=${flows}&active=0.75`,
    { waitUntil: 'load' },
  );
  await page.waitForFunction(() => window.__lab && window.__lab.ready, null, { timeout: 20000 });
  const lab = await page.evaluate(() => window.__lab);

  const first = await page.locator('#stage').screenshot();
  await page.waitForTimeout(450);
  const second = await page.locator('#stage').screenshot();
  await page.locator('#stage').screenshot({ path: `${outDir}/${engineName}-${renderer}.png` });

  // The same page with motion off must be still, which proves the comparison
  // is between "moving" and "not moving" and not between two static pages.
  const still = await context.newPage();
  await still.goto(
    `${base}/index.html?${query}&flows=${flows}&active=0.75&motion=off`,
    { waitUntil: 'load' },
  );
  await still.waitForFunction(() => window.__lab && window.__lab.ready, null, { timeout: 20000 });
  const stillFirst = await still.locator('#stage').screenshot();
  await still.waitForTimeout(450);
  const stillSecond = await still.locator('#stage').screenshot();
  await still.locator('#stage').screenshot({ path: `${outDir}/${engineName}-${renderer}-off.png` });
  await still.close();

  report.push({
    renderer,
    moves: Buffer.compare(first, second) !== 0,
    stillWhenOff: Buffer.compare(stillFirst, stillSecond) === 0,
    svgElements: lab.svgElements,
    domElements: lab.overlayElements,
    errors,
    webgl: lab.webgl,
  });
  await page.close();
}

await context.close();
await instance.close();
process.stdout.write(JSON.stringify(report, null, 2) + '\n');
