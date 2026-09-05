// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Correctness gate for the energy pipe study. Nothing here is a performance
// measurement: it exists because a candidate that silently fails to render, or
// whose animation never moves, benchmarks beautifully.
//
// For every candidate and every axis of the study it checks that the scene has
// ink, that consecutive frames differ while the animation runs, that they stop
// differing when it is turned off, and that the number of animated elements is
// the number the construction is supposed to produce. It also re-derives the
// magnitude mapping from dashboard/static/app.js so the study cannot drift away
// from the production ladder it claims to reproduce.
//
//   node scripts/flow_pipe_study/pipe_verify.mjs <baseUrl> [browser]

import { chromium, firefox } from 'playwright';
import { readFileSync } from 'node:fs';

const base = process.argv[2];
const engineName = process.argv[3] || 'chromium';
const engine = engineName === 'firefox' ? firefox : chromium;

const FLOWS = 4;
const SEGMENTS = 3;

const CASES = [
  { candidate: 'capsule', animated: FLOWS * SEGMENTS, painted: FLOWS * SEGMENTS * 2 },
  { candidate: 'rect', animated: FLOWS * SEGMENTS, painted: FLOWS * SEGMENTS * 2 },
  { candidate: 'radius-el', animated: FLOWS * SEGMENTS },
  { candidate: 'gradient-capsule', animated: FLOWS * SEGMENTS, painted: FLOWS * SEGMENTS * 2 },
  { candidate: 'repeating', animated: FLOWS * SEGMENTS, painted: FLOWS * SEGMENTS * 2 },
  { candidate: 'tokens', tokens: 1, animated: FLOWS * SEGMENTS * 1 },
  { candidate: 'tokens', tokens: 2, animated: FLOWS * SEGMENTS * 2 },
  { candidate: 'tokens', tokens: 4, animated: FLOWS * SEGMENTS * 4 },
  { candidate: 'tokens', tokens: 8, animated: FLOWS * SEGMENTS * 8 },
  { candidate: 'core', animated: FLOWS * SEGMENTS, painted: FLOWS * SEGMENTS * 2 },
  { candidate: 'pulse', animated: FLOWS * SEGMENTS, painted: FLOWS * SEGMENTS * 2 },
  { candidate: 'minimal', animated: FLOWS * SEGMENTS, painted: FLOWS * SEGMENTS * 2 },
  { candidate: 'arrow', animated: FLOWS * SEGMENTS, painted: FLOWS * SEGMENTS * 2 },
  { candidate: 'comet', animated: FLOWS * SEGMENTS, painted: FLOWS * SEGMENTS * 2 },
  { candidate: 'particles', animated: FLOWS * SEGMENTS, painted: FLOWS * SEGMENTS * 2 },
  { candidate: 'wave', animated: FLOWS * SEGMENTS, painted: FLOWS * SEGMENTS * 2 },
  { candidate: 'plasma', animated: FLOWS * SEGMENTS * 2, painted: FLOWS * SEGMENTS * 3 },
  { candidate: 'capsule', glow: 'static', label: 'glow/static', animated: FLOWS * SEGMENTS },
  { candidate: 'capsule', glow: 'texture', label: 'glow/texture', animated: FLOWS * SEGMENTS },
  { candidate: 'capsule', glow: 'blur', label: 'glow/blur', animated: FLOWS * SEGMENTS },
  { candidate: 'capsule', glow: 'filter', label: 'glow/filter', animated: FLOWS * SEGMENTS },
  { candidate: 'capsule', glow: 'blend', label: 'glow/blend', animated: FLOWS * SEGMENTS },
  { candidate: 'capsule', glow: 'layered', label: 'glow/layered', animated: FLOWS * SEGMENTS * 2 },
  { candidate: 'arrow', label: 'arrow/reversed', animated: FLOWS * SEGMENTS,
    painted: FLOWS * SEGMENTS * 2, reverse: 'all' },
  { candidate: 'capsule', tile: 4, animated: FLOWS * SEGMENTS, label: 'capsule/tile4' },
  { candidate: 'capsule', tile: 16, animated: FLOWS * SEGMENTS, label: 'capsule/tile16' },
  { candidate: 'capsule', pad: 24, animated: FLOWS * SEGMENTS, label: 'capsule/pad24' },
  { candidate: 'capsule', pad: 72, animated: FLOWS * SEGMENTS, label: 'capsule/pad72' },
  { candidate: 'capsule', texture: 'rich', animated: FLOWS * SEGMENTS, label: 'capsule/rich' },
];

function urlFor(spec, extra = {}) {
  const query = new URLSearchParams({
    candidate: spec.candidate,
    scenario: 'aggregate',
    flows: String(FLOWS),
    watts: 'mixed',
    speeds: 'single',
    motion: 'on',
    tokens: String(spec.tokens ?? 2),
    tile: String(spec.tile ?? 1),
    pad: String(spec.pad ?? 0),
    texture: spec.texture ?? 'simple',
    glow: spec.glow ?? 'none',
    ...(spec.reverse ? { reverse: spec.reverse } : {}),
    ...extra,
  });
  return `${base}/index.html?${query}`;
}

const instance = await engine.launch({ headless: true });
const context = await instance.newContext({ viewport: { width: 1440, height: 900 } });
const differ = await context.newPage();
await differ.goto('about:blank');

async function diff(a, b) {
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
    let ink = 0;
    let total = 0;
    for (let i = 0; i < pa.length; i += 4) {
      const d = Math.abs(pa[i] - pb[i]) + Math.abs(pa[i + 1] - pb[i + 1])
        + Math.abs(pa[i + 2] - pb[i + 2]);
      total += d;
      if (d > 12) differing += 1;
      if (pa[i] + pa[i + 1] + pa[i + 2] > 150) ink += 1;
    }
    const pixels = width * height;
    return {
      differingFraction: differing / pixels,
      inkFraction: ink / pixels,
      meanChannelDelta: total / (pixels * 3),
    };
  }, [a.toString('base64'), b.toString('base64')]);
}

async function shootPair(url, waitMs) {
  const page = await context.newPage();
  await page.goto(url, { waitUntil: 'load' });
  await page.waitForFunction(() => window.__lab && window.__lab.ready, null, { timeout: 20000 });
  await page.waitForTimeout(500);
  const first = await page.locator('#stage').screenshot();
  await page.waitForTimeout(waitMs);
  const second = await page.locator('#stage').screenshot();
  const lab = await page.evaluate(() => window.__lab);
  await page.close();
  return { first, second, lab };
}

function productionMagnitude() {
  const source = readFileSync(new URL('../../dashboard/static/app.js', import.meta.url), 'utf8');
  const number = (name) => {
    const match = source.match(new RegExp(`const ${name} = ([0-9.]+);`));
    return match ? Number(match[1]) : null;
  };
  const ladder = source.match(/const FLOW_SCALE_LADDER = \[([^\]]+)\]/);
  return {
    idle: number('FLOW_RIBBON_IDLE_W'),
    min: number('FLOW_RIBBON_MIN_W'),
    max: number('FLOW_RIBBON_MAX_W'),
    ceiling: number('FLOW_RIBBON_CEILING_W'),
    ladder: ladder ? ladder[1].split(',').map((v) => Number(v.trim())) : null,
  };
}

const results = [];
for (const spec of CASES) {
  const label = spec.label || (spec.tokens ? `${spec.candidate}/${spec.tokens}` : spec.candidate);
  const moving = await shootPair(urlFor(spec), 420);
  const still = await shootPair(urlFor(spec, { motion: 'off' }), 420);
  const movingDiff = await diff(moving.first, moving.second);
  const stillDiff = await diff(still.first, still.second);

  const checks = {
    renders: movingDiff.inkFraction > 0.001,
    moves: movingDiff.differingFraction > 0.0005,
    stops: stillDiff.differingFraction < 0.00005,
    animatedElementsExpected: spec.animated,
    animatedElementsSeen: moving.lab.animatedElements,
    animatedElementsMatch: moving.lab.animatedElements === spec.animated,
    cssAnimationsSeen: moving.lab.cssAnimations,
    cssAnimationsAreDeclarative: moving.lab.cssAnimations === moving.lab.animatedElements,
  };
  if (spec.painted !== undefined) {
    checks.paintedElementsExpected = spec.painted;
    checks.paintedElementsSeen = moving.lab.paintedElements;
    checks.paintedElementsMatch = moving.lab.paintedElements === spec.painted;
  }
  checks.pass = checks.renders && checks.moves && checks.stops
    && checks.animatedElementsMatch && checks.cssAnimationsAreDeclarative
    && (checks.paintedElementsMatch !== false);

  results.push({
    label,
    candidate: spec.candidate,
    inkFraction: Number(movingDiff.inkFraction.toFixed(5)),
    movingDifferingFraction: Number(movingDiff.differingFraction.toFixed(5)),
    stillDifferingFraction: Number(stillDiff.differingFraction.toFixed(6)),
    paintedElements: moving.lab.paintedElements,
    checks,
  });
  process.stderr.write(`${checks.pass ? 'OK  ' : 'FAIL'} ${label}\n`);
}

const appearanceNeutral = [];
{
  const reference = await shootPair(urlFor({ candidate: 'capsule' }, { motion: 'off' }), 100);
  for (const spec of [
    { candidate: 'capsule', tile: 4, label: 'tile=4' },
    { candidate: 'capsule', tile: 16, label: 'tile=16' },
    { candidate: 'capsule', pad: 24, label: 'pad=24' },
    { candidate: 'capsule', pad: 72, label: 'pad=72' },
  ]) {
    const shot = await shootPair(urlFor(spec, { motion: 'off' }), 100);
    const delta = await diff(reference.first, shot.first);
    // Rasterising a wider source SVG to the same strip leaves subpixel noise, so
    // the test is "no visible difference", not "identical bytes".
    const neutral = delta.differingFraction < 0.005 && delta.meanChannelDelta < 0.5;
    appearanceNeutral.push({
      axis: spec.label,
      differingFraction: Number(delta.differingFraction.toFixed(6)),
      meanChannelDelta: Number(delta.meanChannelDelta.toFixed(4)),
      visuallyIdentical: neutral,
    });
    process.stderr.write(
      `${neutral ? 'OK  ' : 'FAIL'} appearance-neutral ${spec.label} `
      + `(${(delta.differingFraction * 100).toFixed(2)}% px, mean ${delta.meanChannelDelta.toFixed(3)}/255)\n`,
    );
  }
}

const production = productionMagnitude();
const studyMagnitude = await (async () => {
  const page = await context.newPage();
  await page.goto(urlFor({ candidate: 'capsule' }, { flows: '1', watts: '3000' }), { waitUntil: 'load' });
  await page.waitForFunction(() => window.__lab && window.__lab.ready, null, { timeout: 20000 });
  const values = await page.evaluate(() => ({
    ribbon: window.PipeStudy.RIBBON,
    ladder: window.PipeStudy.SCALE_LADDER,
    samples: window.PipeStudy.WATT_SAMPLES.map((w) => ({
      watts: w,
      width: window.PipeStudy.ribbonWidth(w, window.PipeStudy.scaleReference(3000), true),
    })),
  }));
  await page.close();
  return values;
})();

const magnitudeMatches =
  production.idle === studyMagnitude.ribbon.idle &&
  production.min === studyMagnitude.ribbon.min &&
  production.max === studyMagnitude.ribbon.max &&
  production.ceiling === studyMagnitude.ribbon.ceiling &&
  JSON.stringify(production.ladder) === JSON.stringify(studyMagnitude.ladder);
process.stderr.write(`${magnitudeMatches ? 'OK  ' : 'FAIL'} magnitude mapping matches production\n`);

await context.close();
await instance.close();

const failed = results.filter((r) => !r.checks.pass).length
  + appearanceNeutral.filter((a) => !a.visuallyIdentical).length
  + (magnitudeMatches ? 0 : 1);

process.stdout.write(JSON.stringify({
  kind: 'energy-pipe-verify',
  browser: engineName,
  note: 'headless; this is a correctness gate, not a performance measurement',
  flows: FLOWS,
  results,
  appearanceNeutral,
  magnitude: { production, study: studyMagnitude.ribbon, ladder: studyMagnitude.ladder, matches: magnitudeMatches },
  failed,
}, null, 2) + '\n');
process.exit(failed ? 1 : 0);
