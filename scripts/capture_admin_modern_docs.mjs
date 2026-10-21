// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Documentation screenshots of the Modern Admin console. It starts the Admin
// docs preview (scripts/serve_admin_docs_preview.py) on a free loopback port,
// which serves the real admin/static/modern/ build with the deterministic demo
// payloads of tests/fixtures/admin_docs/, and photographs a few pages on a
// desktop and on a phone. No hardware, Docker, config.json or password.
//
//   node scripts/capture_admin_modern_docs.mjs
import { spawn } from "node:child_process";
import { createServer } from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const OUT = path.join(ROOT, "docs", "assets", "screenshots", "admin");

// The demo installation's own afternoon, so "started 6 hours ago" and the
// backup ages read the same on every capture.
const DEMO_NOW = new Date("2026-07-05T13:30:00Z");

const SHOTS = [
  { file: "admin-modern-overview.png", hash: "#/overview", viewport: { width: 1440, height: 900 } },
  { file: "admin-modern-backups.png", hash: "#/backups", viewport: { width: 1440, height: 900 } },
  { file: "admin-modern-phone.png", hash: "#/overview", viewport: { width: 390, height: 844 }, phone: true },
];

function freePort() {
  return new Promise((resolve, reject) => {
    const probe = createServer();
    probe.once("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
  });
}

async function waitFor(url) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      if ((await fetch(url)).ok) return;
    } catch {
      // The preview is still starting.
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`the Admin docs preview did not answer at ${url}`);
}

const port = await freePort();
const preview = spawn("python3", [path.join(ROOT, "scripts", "serve_admin_docs_preview.py"), "--port", String(port)], { stdio: "ignore" });
const base = `http://127.0.0.1:${port}`;
const browser = await chromium.launch();
try {
  await waitFor(`${base}/api/admin/auth/status`);
  for (const shot of SHOTS) {
    const context = await browser.newContext({ viewport: shot.viewport, deviceScaleFactor: 1, reducedMotion: "reduce" });
    await context.addInitScript(() => localStorage.setItem("ems-modern.motion", "off"));
    await context.clock.setFixedTime(DEMO_NOW);
    const page = await context.newPage();
    await page.goto(`${base}/modern/${shot.hash}`);
    await page.locator(".app").waitFor();
    await page.locator('[data-testid="health-state"] .dot.ok').waitFor();
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: path.join(OUT, shot.file), fullPage: !shot.phone });
    console.log(`wrote ${path.relative(ROOT, path.join(OUT, shot.file))}`);
    await context.close();
  }
} finally {
  await browser.close();
  preview.kill();
}
