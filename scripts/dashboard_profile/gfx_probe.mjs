// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Does the software-rendering switch actually switch anything?
//
// WEBGL_debug_renderer_info names the device WebGL was given and Firefox
// sanitises it, so it reports the same string with WebRender's software backend
// forced as without. about:support carries what the compositor really is. This
// reads it, so "software" in a report is a recorded fact rather than a flag
// that was passed.
//
//   node scripts/dashboard_profile/gfx_probe.mjs

import { firefox } from 'playwright';

const PREFS = {
  'gfx.webrender.software': true,
  'gfx.webrender.software.opengl': false,
  'layers.acceleration.disabled': true,
};

const results = [];
for (const software of [false, true]) {
  const instance = await firefox.launch({
    headless: false,
    ...(software ? { firefoxUserPrefs: PREFS } : {}),
  });
  const page = await instance.newPage();
  let reachable = true;
  try {
    await page.goto('about:support', { waitUntil: 'domcontentloaded', timeout: 20000 });
    await page.waitForTimeout(2500);
  } catch (error) {
    // Playwright cannot drive Firefox's privileged pages. Recorded rather than
    // worked around: a software-rendering claim that cannot be verified is not
    // a measurement, and this file exists to say so out loud.
    reachable = false;
  }
  const graphics = reachable ? await page.evaluate(() => {
    const wanted = [
      'Compositing', 'WebRender', 'Window Protocol', 'Target Frame Rate',
      'GPU #1', 'Driver Version', 'HW_COMPOSITING', 'WEBRENDER',
    ];
    const rows = [...document.querySelectorAll('tr')];
    const out = {};
    for (const row of rows) {
      const cells = [...row.querySelectorAll('th, td')].map((c) => c.textContent.trim());
      if (cells.length < 2) continue;
      if (wanted.some((w) => cells[0].startsWith(w))) out[cells[0]] = cells.slice(1).join(' | ').slice(0, 160);
    }
    return out;
  }) : null;
  const webgl = await page.evaluate(() => {
    try {
      const probe = document.createElement('canvas');
      const gl = probe.getContext('webgl2') || probe.getContext('webgl');
      if (!gl) return null;
      const dbg = gl.getExtension('WEBGL_debug_renderer_info');
      return dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
    } catch { return null; }
  }).catch(() => null);
  await instance.close();
  results.push({ software, prefs: software ? PREFS : {}, graphics, webgl, reachable });
  process.stderr.write(
    `software=${software} aboutSupportReachable=${reachable} webgl=${webgl}\n`,
  );
}

const verified = results.every((r) => r.reachable);
process.stdout.write(JSON.stringify({
  kind: 'firefox-gfx-probe',
  verified,
  note: verified
    ? 'read from about:support, not inferred from a WebGL device string'
    : 'about:support could not be opened through Playwright, so the compositor '
      + 'could not be read. The WebGL string below is identical in both modes '
      + 'and proves nothing either way: the software-rendering axis is '
      + 'UNVERIFIED and must not be reported as a software measurement.',
  results,
}, null, 2) + '\n');
