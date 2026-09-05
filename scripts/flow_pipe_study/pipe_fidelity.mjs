// SPDX-License-Identifier: AGPL-3.0-or-later
//
// How far each pipe construction is from the one in production.
//
// With the animation paused every candidate stops at a defined phase, so the
// comparison is reproducible. A large number is not a failure: the study
// explicitly allows a candidate to look better than today. Read it beside the
// gallery, never alone.
//
//   node scripts/flow_pipe_study/pipe_fidelity.mjs <baseUrl> <outDir> [browser]

import { chromium, firefox } from 'playwright';
import { mkdirSync, writeFileSync } from 'node:fs';

const base = process.argv[2];
const outDir = process.argv[3];
const engineName = process.argv[4] || 'chromium';
const engine = engineName === 'firefox' ? firefox : chromium;

const CONTROL = 'capsule';
const CANDIDATES = [
  ['rect', {}],
  ['radius-el', {}],
  ['gradient-capsule', {}],
  ['repeating', {}],
  ['tokens/1', { candidate: 'tokens', tokens: '1' }],
  ['tokens/2', { candidate: 'tokens', tokens: '2' }],
  ['tokens/4', { candidate: 'tokens', tokens: '4' }],
  ['tokens/8', { candidate: 'tokens', tokens: '8' }],
  ['core', {}],
  ['pulse', {}],
  ['minimal', {}],
  ['arrow', {}],
  ['plasma', {}],
  ['comet', {}],
  ['particles', {}],
  ['wave', {}],
];
const WATTS = ['170', '690', '3000'];

mkdirSync(outDir, { recursive: true });

const instance = await engine.launch({ headless: true });
const context = await instance.newContext({ viewport: { width: 1440, height: 900 } });
const differ = await context.newPage();
await differ.goto('about:blank');

function url(label, extra, watts) {
  const candidate = extra.candidate || label;
  const params = new URLSearchParams({
    candidate,
    scenario: 'aggregate',
    flows: '4',
    watts,
    speeds: 'single',
    reverse: 'none',
    motion: 'off',
    tokens: extra.tokens || '4',
    tile: '1',
    pad: '0',
    texture: 'simple',
  });
  return `${base}/index.html?${params}`;
}

async function shoot(target, file) {
  const page = await context.newPage();
  await page.goto(target, { waitUntil: 'load' });
  await page.waitForFunction(() => window.__lab && window.__lab.ready, null, { timeout: 20000 });
  await page.waitForTimeout(400);
  const buffer = await page.locator('#stage').screenshot();
  if (file) writeFileSync(`${outDir}/${file}`, buffer);
  await page.close();
  return buffer;
}

async function compare(a, b) {
  return differ.evaluate(async ([first, second]) => {
    const load = (data) => new Promise((resolve, reject) => {
      const image = new Image();
      image.onload = () => resolve(image);
      image.onerror = reject;
      image.src = 'data:image/png;base64,' + data;
    });
    const one = await load(first);
    const two = await load(second);
    const width = Math.min(one.width, two.width);
    const height = Math.min(one.height, two.height);
    const read = (image) => {
      const canvas = document.createElement('canvas');
      canvas.width = width;
      canvas.height = height;
      const ctx = canvas.getContext('2d', { willReadFrequently: true });
      ctx.drawImage(image, 0, 0);
      return ctx.getImageData(0, 0, width, height).data;
    };
    const pa = read(one);
    const pb = read(two);
    let differing = 0;
    let total = 0;
    let inkA = 0;
    let inkB = 0;
    for (let i = 0; i < pa.length; i += 4) {
      const d = Math.abs(pa[i] - pb[i]) + Math.abs(pa[i + 1] - pb[i + 1])
        + Math.abs(pa[i + 2] - pb[i + 2]);
      total += d;
      if (d > 12) differing += 1;
      if (pa[i] + pa[i + 1] + pa[i + 2] > 150) inkA += 1;
      if (pb[i] + pb[i + 1] + pb[i + 2] > 150) inkB += 1;
    }
    const pixels = width * height;
    return {
      differingFraction: differing / pixels,
      meanChannelDelta: total / (pixels * 3),
      inkFractionControl: inkA / pixels,
      inkFractionCandidate: inkB / pixels,
    };
  }, [a.toString('base64'), b.toString('base64')]);
}

const results = [];
for (const watts of WATTS) {
  const control = await shoot(url(CONTROL, {}, watts), `${engineName}-${watts}w-capsule.png`);
  for (const [label, extra] of CANDIDATES) {
    const file = `${engineName}-${watts}w-${label.replace('/', '')}.png`;
    const shot = await shoot(url(label, extra, watts), file);
    const metrics = await compare(control, shot);
    results.push({
      watts: Number(watts),
      candidate: label,
      differingFraction: Number(metrics.differingFraction.toFixed(5)),
      meanChannelDelta: Number(metrics.meanChannelDelta.toFixed(3)),
      inkFractionControl: Number(metrics.inkFractionControl.toFixed(5)),
      inkFractionCandidate: Number(metrics.inkFractionCandidate.toFixed(5)),
      inkRatio: Number((metrics.inkFractionCandidate / (metrics.inkFractionControl || 1)).toFixed(3)),
    });
    process.stderr.write(
      `  ${watts}W ${label.padEnd(18)} diff ${(metrics.differingFraction * 100).toFixed(2)}% `
      + `mean ${metrics.meanChannelDelta.toFixed(2)}/255 ink x${(metrics.inkFractionCandidate / (metrics.inkFractionControl || 1)).toFixed(2)}\n`,
    );
  }
}

await context.close();
await instance.close();
process.stdout.write(JSON.stringify({
  kind: 'energy-pipe-fidelity',
  browser: engineName,
  note: 'headless; a still-frame comparison of appearance, not a performance measurement',
  control: CONTROL,
  results,
}, null, 2) + '\n');
