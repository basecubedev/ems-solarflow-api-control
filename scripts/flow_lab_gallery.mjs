// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Visual evidence for the flow rendering lab.
//
// flow_lab_fidelity.mjs answers "how far is this from the current design", which
// is the wrong question when a candidate is allowed to look better. This script
// answers "what does it actually look like, moving, in the states the product
// has": it captures each renderer across several scenarios and several instants
// so the character of the motion is visible and not just a single frozen frame.
//
// Screenshots are taken with motion ON and spaced in wall-clock time, so they
// are deliberately NOT pixel-comparable between renderers. Use them to judge
// clarity, density and glow; use flow_lab_fidelity.mjs when a number is wanted.
//
//   node scripts/flow_lab_gallery.mjs <baseUrl> <outDir> [browser] [renderers]

import { chromium, firefox } from 'playwright';
import { mkdirSync, writeFileSync } from 'node:fs';

const base = process.argv[2];
const outDir = process.argv[3];
const engineName = process.argv[4] || 'chromium';
const renderers = (process.argv[5] || 'dashoffset,dom-tiles,canvas')
  .split(',')
  .map((name) => name.trim())
  .filter(Boolean);

const engine = engineName === 'firefox' ? firefox : chromium;

// The scenarios the product actually has to survive, not a single happy path.
const SCENARIOS = [
  { key: 'aggregate', flows: 4, active: 1, speeds: 'mixed', note: 'Aggregate view, all flows active, mixed speeds' },
  { key: 'partial', flows: 4, active: 0.5, speeds: 'mixed', note: 'Half the flows idle - active vs inactive must be legible' },
  { key: 'devices-4', flows: 12, active: 0.75, speeds: 'mixed', note: 'Devices view, realistic complexity' },
  { key: 'devices-8', flows: 24, active: 0.75, speeds: 'mixed', note: 'Devices view, eight devices' },
  { key: 'stress', flows: 50, active: 1, speeds: 'mixed', note: 'Stress case' },
  { key: 'single-speed', flows: 4, active: 1, speeds: 'single', note: 'One speed everywhere - isolates speed as a channel' },
];

// Three instants roughly a third of a dash period apart. Enough to see whether
// the motion reads as continuous flow or as a strobing pattern.
const INSTANTS_MS = [500, 960, 1420];

mkdirSync(outDir, { recursive: true });

const instance = await engine.launch({ headless: true });
const context = await instance.newContext({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 1,
});

const manifest = [];

for (const renderer of renderers) {
  for (const scenario of SCENARIOS) {
    const page = await context.newPage();
    const url =
      `${base}/index.html?renderer=${renderer}&flows=${scenario.flows}` +
      `&active=${scenario.active}&speeds=${scenario.speeds}&motion=on`;
    let ok = true;
    let failure = null;
    try {
      await page.goto(url, { waitUntil: 'load', timeout: 45000 });
      await page.waitForFunction(() => window.__lab && window.__lab.ready, null, { timeout: 25000 });
    } catch (error) {
      ok = false;
      failure = String(error).split('\n')[0];
    }
    if (ok) {
      let previous = 0;
      for (const at of INSTANTS_MS) {
        await page.waitForTimeout(at - previous);
        previous = at;
        const name = `${engineName}-${renderer}-${scenario.key}-t${at}.png`;
        try {
          const buffer = await page.locator('#stage').screenshot();
          writeFileSync(`${outDir}/${name}`, buffer);
          manifest.push({ renderer, scenario: scenario.key, note: scenario.note, atMs: at, file: name });
        } catch (error) {
          manifest.push({ renderer, scenario: scenario.key, atMs: at, error: String(error).split('\n')[0] });
        }
      }
    } else {
      manifest.push({ renderer, scenario: scenario.key, error: failure });
    }
    await page.close();
  }
  process.stderr.write(`captured ${renderer}\n`);
}

await context.close();
await instance.close();

// A contact sheet, because comparing 100 PNGs in a file manager is not review.
const byScenario = new Map();
for (const entry of manifest) {
  if (!byScenario.has(entry.scenario)) byScenario.set(entry.scenario, []);
  byScenario.get(entry.scenario).push(entry);
}
let html = `<!doctype html><meta charset="utf-8"><title>Flow gallery - ${engineName}</title>
<style>
 body{background:#11151c;color:#dfe6f0;font:14px system-ui,sans-serif;margin:24px}
 h2{margin:32px 0 4px;font-size:18px} p.note{margin:0 0 12px;color:#8fa0b8}
 .row{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:8px}
 figure{margin:0} figcaption{color:#8fa0b8;font-size:12px;margin-top:4px}
 img{max-width:440px;border:1px solid #2a3342;border-radius:6px;display:block}
 .err{color:#ff8f8f}
</style>
<h1>Flow rendering gallery - ${engineName}</h1>
<p class="note">Motion is ON and frames are spaced in wall-clock time, so frames are not pixel-comparable between renderers. Judge clarity, density, direction and glow.</p>`;
for (const [scenario, entries] of byScenario) {
  const note = (entries.find((e) => e.note) || {}).note || '';
  html += `<h2>${scenario}</h2><p class="note">${note}</p>`;
  for (const renderer of renderers) {
    const shots = entries.filter((e) => e.renderer === renderer);
    html += `<div class="row">`;
    for (const shot of shots) {
      html += shot.error
        ? `<figure><figcaption class="err">${renderer}: ${shot.error}</figcaption></figure>`
        : `<figure><img src="${shot.file}" loading="lazy"><figcaption>${renderer} @ ${shot.atMs}ms</figcaption></figure>`;
    }
    html += `</div>`;
  }
}
writeFileSync(`${outDir}/gallery-${engineName}.html`, html);
process.stdout.write(JSON.stringify({ browser: engineName, renderers, scenarios: SCENARIOS.map((s) => s.key), manifest }, null, 2) + '\n');
