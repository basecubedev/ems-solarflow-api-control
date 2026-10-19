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
// pinTheDay (pin-the-day.ts) fixes the day every capture shows, so a capture
// just after midnight does not document an empty chart.
import { expect, test } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { pinTheDay } from "./pin-the-day";

const SHOTS = "docs/assets/screenshots/dashboard";
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

  test("dashboard-modern-phone-more", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await openOverview(page);
    await page.getByTestId("nav-more").click();
    const sheet = page.locator("dialog.sheet");
    await expect(sheet).toBeVisible();
    await sheet.evaluate((node) => Promise.all(node.getAnimations().map((animation) => animation.finished)));
    await page.screenshot({ path: `${SHOTS}/dashboard-modern-phone-more.png` });
  });
});
