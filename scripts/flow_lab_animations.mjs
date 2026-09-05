// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Lists every animation the dashboard is actually running, per view. Guessing
// which CSS rules animate from the stylesheet is how an isolation experiment
// ends up removing one of two costs and reporting the wrong conclusion;
// document.getAnimations() asks the engine instead.
//
//   node scripts/flow_lab_animations.mjs <dashboardUrl> [browser]

import { chromium, firefox } from 'playwright';

const url = process.argv[2];
const engine = process.argv[3] === 'firefox' ? firefox : chromium;

const instance = await engine.launch({ headless: true });
const context = await instance.newContext({ viewport: { width: 1440, height: 900 } });
const page = await context.newPage();
await page.goto(url, { waitUntil: 'load' });
await page.waitForTimeout(2500);

const report = {};
for (const view of ['aggregated', 'devices', 'control', 'energy']) {
  await page.evaluate((target) => {
    if (typeof setFlowView === 'function') setFlowView(target, false);
  }, view);
  await page.waitForTimeout(1500);
  report[view] = await page.evaluate(() => {
    const describe = (element) => {
      if (!element || !element.tagName) return '(none)';
      const classes = String(element.getAttribute('class') || '')
        .split(/\s+/).filter(Boolean).join('.');
      return element.tagName.toLowerCase() + (classes ? '.' + classes : '');
    };
    const counts = {};
    for (const animation of document.getAnimations()) {
      if (animation.playState !== 'running') continue;
      const name = animation.animationName || (animation.effect && animation.effect.getKeyframes
        ? '(transition/other)' : '(unknown)');
      const target = animation.effect && animation.effect.target;
      const key = name + '  on  ' + describe(target);
      counts[key] = (counts[key] || 0) + 1;
    }
    return counts;
  });
}

await context.close();
await instance.close();
process.stdout.write(JSON.stringify(report, null, 2) + '\n');
