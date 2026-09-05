// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Checks the production flow tile renderer against the CSS animation it
// replaces. Three things have to hold, and each has been broken at least once
// during development:
//
//   * the renderer switches on and the picture moves,
//   * no CSS animation is left running inside the flow SVG -- which is the
//     whole point of the change, and is invisible in a screenshot,
//   * with the renderer forced off, the page still animates by itself.
//
//   node scripts/flow_tiles_check.mjs <dashboardUrl> <outDir> [browser]

import { chromium, firefox } from 'playwright';
import { mkdirSync, writeFileSync } from 'node:fs';

const url = process.argv[2];
const outDir = process.argv[3];
const engineName = process.argv[4] || 'chromium';
const engine = engineName === 'firefox' ? firefox : chromium;

mkdirSync(outDir, { recursive: true });
const instance = await engine.launch({ headless: true });
const context = await instance.newContext({ viewport: { width: 1440, height: 900 } });

async function inspect(view, forceOff) {
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(String(error)));
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  if (forceOff) {
    // Force the decline path. getComputedStyle is what the renderer reads the
    // whole appearance from, and removing it is the narrowest way to make it
    // refuse without changing anything else about the page.
    await page.addInitScript(() => {
      Object.defineProperty(window, 'getComputedStyle', {
        value: undefined,
        configurable: true,
      });
    });
  }
  await page.goto(url, { waitUntil: 'load' });
  await page.waitForTimeout(2500);
  await page.evaluate((target) => {
    if (typeof setFlowView === 'function') setFlowView(target, false);
  }, view);
  await page.waitForTimeout(1500);

  const state = await page.evaluate(() => ({
    active: document.body.classList.contains('flow-tiles-active'),
    layers: document.querySelectorAll('.flow-tile-layer:not([hidden])').length,
    tiles: document.querySelectorAll('.flow-tile').length,
    animations: document.getAnimations()
      .filter((a) => a.playState === 'running')
      .map((a) => a.animationName)
      .reduce((acc, name) => {
        acc[name] = (acc[name] || 0) + 1;
        return acc;
      }, {}),
  }));

  const target = view === 'devices' ? '#deviceFlowView' : '.flow-wrap';
  const first = await page.locator(target).screenshot();
  await page.waitForTimeout(430);
  const second = await page.locator(target).screenshot();
  const tag = `${engineName}-${view}-${forceOff ? 'css' : 'tiles'}`;
  writeFileSync(`${outDir}/${tag}.png`, second);

  await page.close();
  return { view, rendererOff: forceOff, ...state, moves: Buffer.compare(first, second) !== 0, errors };
}

const results = [];
for (const view of ['aggregated', 'devices']) {
  results.push(await inspect(view, false));
  results.push(await inspect(view, true));
}

await context.close();
await instance.close();
process.stdout.write(JSON.stringify(results, null, 2) + '\n');
