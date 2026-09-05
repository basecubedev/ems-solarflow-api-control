// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Screenshots for the energy pipe study: one strip per candidate across the
// magnitude range, plus the gallery states the study has to show.
//
//   node scripts/flow_pipe_study/pipe_shots.mjs <baseUrl> <outDir> [browser] [mode]

import { chromium, firefox } from 'playwright';
import { mkdirSync, writeFileSync } from 'node:fs';

const base = process.argv[2];
const outDir = process.argv[3];
const engineName = process.argv[4] || 'chromium';
const mode = process.argv[5] || 'all';
const engine = engineName === 'firefox' ? firefox : chromium;

const CANDIDATES = [
  'capsule', 'rect', 'radius-el', 'gradient-capsule', 'repeating',
  'tokens', 'core', 'pulse', 'minimal', 'arrow',
  'plasma', 'comet', 'particles', 'wave',
];
const WATTS = [0, 40, 170, 690, 2000, 3000];

mkdirSync(outDir, { recursive: true });

const instance = await engine.launch(
  process.env.PIPE_HEADED ? { headless: false } : { headless: true },
);
const context = await instance.newContext({ viewport: { width: 1440, height: 980 } });

async function shot(url, file, selector = '#stage') {
  const page = await context.newPage();
  await page.goto(url, { waitUntil: 'load' });
  await page.waitForFunction(() => window.__lab && window.__lab.ready, null, { timeout: 20000 });
  await page.waitForTimeout(700);
  const buffer = await page.locator(selector).screenshot({ type: 'jpeg', quality: 82 });
  writeFileSync(`${outDir}/${file}`, buffer);
  await page.close();
  process.stderr.write(`  ${file}\n`);
}

if (mode === 'all' || mode === 'candidates') {
  for (const candidate of CANDIDATES) {
    for (const watts of WATTS) {
      const query = new URLSearchParams({
        candidate,
        scenario: 'aggregate',
        flows: '4',
        watts: String(watts),
        speeds: 'mixed',
        reverse: 'none',
        tokens: '4',
        motion: 'on',
      });
      await shot(`${base}/index.html?${query}`, `cand-${candidate}-${watts}w.jpg`);
    }
  }
  for (const candidate of CANDIDATES) {
    const dense = new URLSearchParams({
      candidate, scenario: 'devices', devices: '8', watts: 'mixed',
      speeds: 'mixed', tokens: '4', motion: 'on',
    });
    await shot(`${base}/index.html?${dense}`, `dense-${candidate}-8dev.jpg`);
  }
}

if (mode === 'all' || mode === 'shapes') {
  for (const shape of ['short', 'long', 'mixed']) {
    for (const candidate of CANDIDATES) {
      const query = new URLSearchParams({
        candidate, scenario: 'aggregate', flows: '4', watts: '690',
        speeds: 'mixed', reverse: 'none', tokens: '4', motion: 'on', shape,
      });
      await shot(`${base}/index.html?${query}`, `shape-${shape}-${candidate}.jpg`);
    }
  }
  for (const candidate of CANDIDATES) {
    const query = new URLSearchParams({
      candidate, scenario: 'aggregate', flows: '4', watts: '-690',
      speeds: 'mixed', reverse: 'none', tokens: '4', motion: 'on',
    });
    await shot(`${base}/index.html?${query}`, `reverse-${candidate}.jpg`);
  }
}

if (mode === 'all' || mode === 'gallery') {
  const states = [
    { label: 'low', power: 1 },
    { label: 'medium', power: 4 },
    { label: 'high', power: 8 },
    { label: 'zero', power: 0 },
    { label: 'reversed', power: 5, direction: 'all' },
    { label: 'aggregate', power: 5, scenario: 'aggregate' },
    { label: 'devices8', power: 5, scenario: 'devices', devices: '8' },
  ];
  for (const state of states) {
    const page = await context.newPage();
    await page.goto(`${base}/gallery.html`, { waitUntil: 'load' });
    await page.waitForFunction(() => window.__gallery && window.__gallery.ready, null, { timeout: 20000 });
    await page.evaluate(async (s) => {
      const set = (id, value) => {
        const el = document.getElementById(id);
        el.value = value;
        el.dispatchEvent(new Event(id === 'power' ? 'input' : 'change', { bubbles: true }));
      };
      if (s.scenario) set('scenario', s.scenario);
      if (s.devices) set('devices', s.devices);
      if (s.direction) set('direction', s.direction);
      set('power', String(s.power));
    }, state);
    await page.waitForTimeout(800);
    const buffer = await page.screenshot({ type: 'jpeg', quality: 80, fullPage: true });
    writeFileSync(`${outDir}/gallery-${state.label}.jpg`, buffer);
    process.stderr.write(`  gallery-${state.label}.jpg\n`);
    await page.close();
  }
}

await context.close();
await instance.close();
process.stdout.write(JSON.stringify({ outDir, browser: engineName, mode }) + '\n');
