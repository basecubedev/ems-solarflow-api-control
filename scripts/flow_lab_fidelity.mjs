// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Visual correctness check for the flow rendering lab.
//
// With `motion=off` every CSS-driven renderer is paused at time zero, and each
// one is built so that time zero is the same dash phase. The candidates are
// therefore directly comparable pixel by pixel against the production
// technique, and "looks close enough" becomes a number instead of an opinion.
//
//   node scripts/flow_lab_fidelity.mjs <baseUrl> <outDir> [browser] [flows]

import { chromium, firefox } from 'playwright';
import { mkdirSync, writeFileSync } from 'node:fs';

// Distance from the shipped design. This study explicitly permits a candidate
// to look BETTER than today, so a large number here is a question to answer,
// not a failure -- read it beside scripts/flow_lab_gallery.mjs rather than
// alone. Metaphors are included because their whole point is to differ.
const CANDIDATES = [
  'svg-transform', 'svg-pattern', 'svg-mask', 'dom-tiles', 'motion-path',
  'canvas', 'canvas-bloom', 'canvas-worker', 'webgl',
];
const METAPHORS = ['capsule', 'particles', 'comet', 'chevron', 'pulse', 'sweep'];
const CONTROL = 'dashoffset';

const base = process.argv[2];
const outDir = process.argv[3];
const engineName = process.argv[4] || 'chromium';
const flows = process.argv[5] || '4';
const engine = engineName === 'firefox' ? firefox : chromium;

mkdirSync(outDir, { recursive: true });

const instance = await engine.launch({ headless: true });
const context = await instance.newContext({ viewport: { width: 1440, height: 900 } });

async function shoot(renderer) {
  const page = await context.newPage();
  await page.goto(
    `${base}/index.html?renderer=${renderer}&flows=${flows}&active=0.75&motion=off`,
    { waitUntil: 'load' },
  );
  await page.waitForFunction(() => window.__lab && window.__lab.ready, null, { timeout: 20000 });
  await page.waitForTimeout(400);
  const buffer = await page.locator('#stage').screenshot();
  await page.close();
  return buffer;
}

// The diff runs inside a page because a canvas is the only PNG decoder
// available here without adding a dependency.
const differ = await context.newPage();
await differ.goto('about:blank');

async function compare(a, b) {
  return differ.evaluate(async ([first, second]) => {
    const load = (data) =>
      new Promise((resolve, reject) => {
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
    let strongly = 0;
    let total = 0;
    let inkA = 0;
    let inkB = 0;
    for (let i = 0; i < pa.length; i += 4) {
      const d =
        Math.abs(pa[i] - pb[i]) + Math.abs(pa[i + 1] - pb[i + 1]) + Math.abs(pa[i + 2] - pb[i + 2]);
      total += d;
      if (d > 12) differing += 1;
      if (d > 96) strongly += 1;
      if (pa[i] + pa[i + 1] + pa[i + 2] > 120) inkA += 1;
      if (pb[i] + pb[i + 1] + pb[i + 2] > 120) inkB += 1;
    }
    const pixels = width * height;
    return {
      width,
      height,
      pixels,
      differingFraction: differing / pixels,
      stronglyDifferingFraction: strongly / pixels,
      meanChannelDelta: total / (pixels * 3),
      inkFractionControl: inkA / pixels,
      inkFractionCandidate: inkB / pixels,
    };
  }, [a.toString('base64'), b.toString('base64')]);
}

const control = await shoot(CONTROL);
writeFileSync(`${outDir}/${engineName}-fidelity-${CONTROL}.png`, control);

const results = [];
for (const renderer of CANDIDATES) {
  const shot = await shoot(renderer);
  writeFileSync(`${outDir}/${engineName}-fidelity-${renderer}.png`, shot);
  const metrics = await compare(control, shot);
  results.push({ renderer, ...metrics });
}
for (const metaphor of METAPHORS) {
  const label = `tiles-${metaphor}`;
  const shot = await shoot(`dom-tiles&metaphor=${metaphor}`);
  writeFileSync(`${outDir}/${engineName}-fidelity-${label}.png`, shot);
  const metrics = await compare(control, shot);
  results.push({ renderer: label, ...metrics });
}

await context.close();
await instance.close();
process.stdout.write(JSON.stringify({ browser: engineName, flows: Number(flows), control: CONTROL, results }, null, 2) + '\n');
