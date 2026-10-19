// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Documentation screenshots of the Modern dashboard for docs/user/dashboard/.
//
// Not part of the normal suite: it writes into the tracked PNGs under
// docs/assets/screenshots/dashboard/, so playwright.dashboard.config.ts leaves
// it out unless EMS_DASHBOARD_CAPTURE_DOCS is set. Classic's pages are captured
// by scripts/capture_dashboard_docs.py; Modern's are here because a page that
// renders its history after the load event needs a wait for an element, which
// a Firefox --screenshot cannot express. Run it with
//
//   EMS_DASHBOARD_CAPTURE_DOCS=1 npx playwright test \
//     --config=playwright.dashboard.config.ts --project=chromium capture-docs
//
// Every capture here has a row in that directory's README.md.
//
// The preview's synthetic day is the last 24 hours, whatever the clock says, so
// a capture just after midnight would document an empty chart. The page's clock
// is pinned to 18:30 today and the synthetic day is laid on today and on
// yesterday from midnight, yesterday at 85 %, so every capture shows the same
// day and the same comparison.
import { expect, test } from "@playwright/test";
import { mkdirSync } from "node:fs";

const SHOTS = "docs/assets/screenshots/dashboard";
const SHOT_TIME = new Date();
SHOT_TIME.setHours(18, 30, 0, 0);

async function pinTheDay(page) {
  await page.clock.install({ time: SHOT_TIME });
  const now = Math.floor(SHOT_TIME.getTime() / 1000);
  const midnight = Math.floor(new Date(SHOT_TIME.getFullYear(), SHOT_TIME.getMonth(), SHOT_TIME.getDate()).getTime() / 1000);
  const yesterday = Math.floor(new Date(SHOT_TIME.getFullYear(), SHOT_TIME.getMonth(), SHOT_TIME.getDate() - 1).getTime() / 1000);
  await page.route("**/api/history/series**", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const first = body.time[0];
    const time = [];
    const series = Object.fromEntries(Object.keys(body.series).map((id) => [id, []]));
    for (const [start, share] of [[yesterday, 0.85], [midnight, 1]]) {
      body.time.forEach((stamp, index) => {
        const at = start + (stamp - first);
        if (at > now) return;
        time.push(at);
        for (const id of Object.keys(series)) {
          const value = body.series[id][index];
          series[id].push(value === null ? null : value * share);
        }
      });
    }
    await route.fulfill({ response, json: { ...body, time, series } });
  });
}

async function openOverview(page) {
  await pinTheDay(page);
  await page.goto("/modern/#/overview", { waitUntil: "domcontentloaded" });
  await expect(page.getByTestId("kpi-status")).toHaveAttribute("data-level", "ok");
  await expect(page.locator(".chart canvas").first()).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
}

async function shootWholePage(page, name) {
  const height = await page.evaluate(() => document.documentElement.scrollHeight);
  await page.setViewportSize({ width: 1440, height });
  await expect(page.locator(".chart canvas").first()).toBeVisible();
  await page.screenshot({ path: `${SHOTS}/${name}.png` });
}

test.describe("Modern documentation captures @docs", () => {
  test.skip(({ browserName }) => browserName !== "chromium", "one engine, one set of images");

  test.beforeAll(() => {
    mkdirSync(SHOTS, { recursive: true });
  });

  test("dashboard-modern-overview", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await openOverview(page);
    await shootWholePage(page, "dashboard-modern-overview");
  });

  test("dashboard-modern-midnight-devices", async ({ page }) => {
    await page.addInitScript(() => {
      window.localStorage.setItem("ems-modern.theme", "midnight");
      window.localStorage.setItem("ems-modern.lang", "de");
      window.localStorage.setItem("ems-modern.flow", "devices");
    });
    await page.setViewportSize({ width: 1440, height: 900 });
    await openOverview(page);
    await expect(page.getByTestId("energy-flow")).toHaveAttribute("data-mode", "devices");
    await shootWholePage(page, "dashboard-modern-midnight-devices");
  });

  test("dashboard-modern-phone", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await openOverview(page);
    await expect(page.locator(".bottom-nav")).toBeVisible();
    await page.screenshot({ path: `${SHOTS}/dashboard-modern-phone.png` });
  });
});
