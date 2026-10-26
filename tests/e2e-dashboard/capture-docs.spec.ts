// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Every documentation image of the EMS Dashboard, all of them of the Modern
// dashboard: the PNGs under docs/assets/screenshots/dashboard/ that the guides
// in docs/user/dashboard/ embed, and the docs/assets/preview-*.jpg that
// README.md and docs/dashboard.md embed. One test per file, titled with the
// file's name; tests/test_docs_user_guides.py reads those titles as the
// manifest, and both asset READMEs list every file.
//
// Not part of the normal suite: it writes into tracked files, so
// playwright.dashboard.config.ts leaves it out unless EMS_DASHBOARD_CAPTURE_DOCS
// is set. Run it with
//
//   EMS_DASHBOARD_CAPTURE_DOCS=1 npx playwright test \
//     --config=playwright.dashboard.config.ts --project=chromium capture-docs
//
// A guide PNG is the 1440x900 window (390x844 on a phone); a preview JPG is
// the whole page at the same width. The data is the preview server's
// synthetic set in scripts/dashboard_preview_data.py. A capture that needs a
// scenario other than the server's own -- signed in, a password set but signed
// out, a device offline -- names it with the preview's "ems-preview-scenario"
// cookie, which holds for that browser context only; an offline device seen
// signed in, which no scenario has, adds the signed-in answer to
// /api/auth/status as the browser suite does. pinTheDay (pin-the-day.ts)
// fixes the day and the zone every capture shows, and the config starts the
// preview server on the same instant, so a run on any date shows the same
// content and a capture just after midnight does not document an empty chart.
// The browser runs in English, so a native date field reads like the page
// around it. The appearance strips are the same region of the same page
// photographed once per value of one axis and laid side by side on a blank
// page, so nothing but that axis changes.
//
// Never `waitUntil: "networkidle"`: /api/events stays open.
import { expect, test } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { DOCS_SHOT_TIME, DOCS_ZONE, pinTheDay } from "./pin-the-day";

const SHOTS = "docs/assets/screenshots/dashboard";
const PREVIEWS = "docs/assets";
const DESKTOP = { width: 1440, height: 900 };
const PHONE = { width: 390, height: 844 };
const STRIP = { width: 714, height: 560, gap: 12 };
const SCENARIO_COOKIE = "ems-preview-scenario";
const CSRF = { "X-CSRF-Token": "preview-csrf-token" };
const SIGNED_IN = {
  auth_configured: true,
  authenticated: true,
  csrf_token: "preview-csrf-token",
  write_mode_available: true,
  write_mode_active: true,
};
const DEFAULT_PREFS = {
  "ems-modern.lang": "en",
  "ems-modern.theme": "graphite",
  "ems-modern.density": "normal",
  "ems-modern.flow": "system",
  "ems-modern.energyDetail": "basic",
};

async function open(page, baseURL, hash, { scenario = "normal", signedIn = false, viewport = DESKTOP, prefs = {} } = {}) {
  await page.setViewportSize(viewport);
  if (signedIn) await page.route("**/api/auth/status", (route) => route.fulfill({ json: SIGNED_IN }));
  await page.context().addCookies([{ name: SCENARIO_COOKIE, value: scenario, url: baseURL }]);
  await page.addInitScript((stored) => {
    for (const [key, value] of Object.entries(stored)) window.localStorage.setItem(key, value);
  }, { ...DEFAULT_PREFS, ...prefs });
  await pinTheDay(page, DOCS_SHOT_TIME);
  await page.goto(`/${hash}`, { waitUntil: "domcontentloaded" });
  await expect(page.getByTestId("ems-state")).toContainText("EMS active");
  await page.evaluate(() => document.fonts.ready);
}

async function settled(locator) {
  await locator.evaluate((node) => Promise.all(node.getAnimations({ subtree: true }).map((animation) => animation.finished)));
}

async function painted(page) {
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}

async function scrollTo(page, locator, margin = 24) {
  await locator.first().evaluate((node, gap) => {
    const bar = document.querySelector(".topbar")?.getBoundingClientRect().bottom ?? 0;
    window.scrollTo(0, node.getBoundingClientRect().top + window.scrollY - bar - gap);
  }, margin);
  await painted(page);
}

async function shoot(page, name) {
  await painted(page);
  await page.screenshot({ path: `${SHOTS}/${name}.png`, animations: "disabled" });
}

async function preview(page, name) {
  await page.evaluate(() => window.scrollTo(0, 0));
  let height = 0;
  for (let next = DESKTOP.height; next !== height; ) {
    height = next;
    await page.setViewportSize({ width: DESKTOP.width, height });
    await painted(page);
    next = Math.max(DESKTOP.height, await page.evaluate(() => document.documentElement.scrollHeight));
  }
  await page.screenshot({ path: `${PREVIEWS}/${name}.jpg`, type: "jpeg", quality: 85, animations: "disabled" });
}

async function pickAppearance(page, label) {
  await page.getByTestId("appearance-button").click();
  const menu = page.getByTestId("appearance");
  await expect(menu).toBeVisible();
  await menu.getByRole("button", { name: label, exact: true }).click();
  await page.keyboard.press("Escape");
  await expect(menu).toBeHidden();
  await painted(page);
}

async function column(page, x) {
  await page.evaluate(() => window.scrollTo(0, 0));
  await painted(page);
  return page.screenshot({ clip: { x, y: 0, width: STRIP.width, height: STRIP.height }, animations: "disabled" });
}

async function strip(page, name, columns) {
  const sheet = await page.context().newPage();
  try {
    await sheet.setViewportSize({ width: columns.length * STRIP.width + (columns.length - 1) * STRIP.gap, height: STRIP.height });
    const images = columns.map((png) => `<img alt="" src="data:image/png;base64,${png.toString("base64")}">`).join("");
    await sheet.setContent(`<body style="margin:0;display:flex;gap:${STRIP.gap}px;background:#05070b">${images}</body>`);
    await sheet.waitForFunction(() => [...document.images].every((image) => image.complete && image.naturalWidth > 0));
    await sheet.screenshot({ path: `${SHOTS}/${name}.png` });
  } finally {
    await sheet.close();
  }
}

async function clearScheduleRules(request) {
  const overview = await (await request.get("/api/schedule")).json();
  for (const rule of overview.rules || []) {
    const revision = rule.revision ? `?revision=${encodeURIComponent(rule.revision)}` : "";
    await request.delete(`/api/schedule/rules/${encodeURIComponent(rule.id)}${revision}`, { headers: CSRF });
  }
}

async function waitForChart(page) {
  await expect(page.locator(".chart canvas").first()).toBeVisible();
}

test.use({ locale: "en-US", timezoneId: DOCS_ZONE, launchOptions: { env: { ...process.env, LANG: "en_US.UTF-8", LANGUAGE: "en_US", LC_ALL: "en_US.UTF-8" } } });

test.describe("EMS Dashboard documentation captures @docs", () => {
  test.skip(({ browserName }) => browserName !== "chromium", "one engine, one set of images");

  test.beforeAll(() => {
    mkdirSync(SHOTS, { recursive: true });
  });

  test("dashboard-overview", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/overview");
    await expect(page.getByTestId("kpi-status")).toHaveAttribute("data-level", "ok");
    await expect(page.getByTestId("energy-flow")).toHaveAttribute("data-mode", "system");
    await waitForChart(page);
    await shoot(page, "dashboard-overview");
  });

  test("dashboard-overview-today", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/overview");
    await waitForChart(page);
    await expect(page.getByTestId("today-scale").first()).toBeVisible();
    await scrollTo(page, page.getByTestId("today-tiles"));
    await shoot(page, "dashboard-overview-today");
  });

  test("dashboard-overview-per-device", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/overview", { prefs: { "ems-modern.theme": "midnight", "ems-modern.flow": "devices" } });
    await expect(page.getByTestId("energy-flow")).toHaveAttribute("data-mode", "devices");
    await waitForChart(page);
    await shoot(page, "dashboard-overview-per-device");
  });

  test("dashboard-overview-narrow", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/overview", { viewport: PHONE });
    await expect(page.locator(".bottom-nav")).toBeVisible();
    await expect(page.getByTestId("kpi-status")).toHaveAttribute("data-level", "ok");
    await shoot(page, "dashboard-overview-narrow");
  });

  test("dashboard-phone-more", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/overview", { viewport: PHONE });
    await page.getByTestId("nav-more").click();
    const sheet = page.locator("dialog.sheet");
    await expect(sheet).toBeVisible();
    await settled(sheet);
    await shoot(page, "dashboard-phone-more");
  });

  test("dashboard-devices", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/devices");
    await expect(page.getByTestId("device-card")).toHaveCount(2);
    await expect(page.getByTestId("grid-meter")).toBeVisible();
    await shoot(page, "dashboard-devices");
  });

  test("dashboard-devices-details", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/devices");
    const card = page.locator('[data-testid="device-card"][data-device="WR1"]');
    await expect(card).toHaveAttribute("data-state", "online");
    for (const name of ["Firmware status", "Device settings"]) await card.locator("details.more > summary", { hasText: name }).click();
    await expect(card.locator("details.more[open]")).toHaveCount(2);
    await shoot(page, "dashboard-devices-details");
  });

  test("dashboard-devices-offline", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/devices", { scenario: "offline-device" });
    await expect(page.locator('[data-testid="device-card"][data-device="WR2"]')).toHaveAttribute("data-state", "offline");
    await shoot(page, "dashboard-devices-offline");
  });

  test("dashboard-energy", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/energy");
    await expect(page.getByTestId("energy-periods")).toContainText("Last 7 days");
    await shoot(page, "dashboard-energy");
  });

  test("dashboard-energy-expert", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/energy", { prefs: { "ems-modern.energyDetail": "expert" } });
    await page.getByRole("button", { name: /^Sept: / }).click();
    const detail = page.getByTestId("month-detail");
    await expect(detail).toContainText("Grid import");
    await scrollTo(page, page.locator("dl.context"), 12);
    await shoot(page, "dashboard-energy-expert");
  });

  test("dashboard-analytics", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/analytics");
    await waitForChart(page);
    await page.getByTestId("analytics-view-block").getByRole("button", { name: "Grid", exact: true }).last().click();
    await waitForChart(page);
    await shoot(page, "dashboard-analytics");
  });

  test("dashboard-control", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/control", { scenario: "write-mode" });
    await expect(page.getByTestId("runtime-editor")).toBeVisible();
    await expect(page.getByTestId("control-sentence")).toBeVisible();
    await shoot(page, "dashboard-control");
  });

  test("dashboard-control-pipeline", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/control", { scenario: "write-mode" });
    const pipeline = page.locator(".card", { hasText: "How the target is made" });
    await expect(pipeline).toBeVisible();
    await scrollTo(page, pipeline);
    await shoot(page, "dashboard-control-pipeline");
  });

  test("dashboard-control-device", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/control", { scenario: "write-mode" });
    const card = page.locator(".card", { hasText: "Decision per device" });
    const decision = card.locator("details.decision").first();
    await decision.locator("summary").click();
    await expect(decision).toHaveAttribute("open", "");
    await expect(decision.locator(".stage")).toHaveCount(5);
    await scrollTo(page, card);
    await shoot(page, "dashboard-control-device");
  });

  test("dashboard-control-readonly", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/control", { scenario: "auth-readonly" });
    await expect(page.getByText("Read-only. Sign in to change runtime values.")).toBeVisible();
    await shoot(page, "dashboard-control-readonly");
  });

  test("dashboard-sign-in", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/control", { scenario: "auth-readonly" });
    await page.getByTestId("sign-in").click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await dialog.getByLabel("Password").fill("demo-password");
    await settled(dialog);
    await shoot(page, "dashboard-sign-in");
  });

  test("dashboard-schedule", async ({ page, baseURL }) => {
    await clearScheduleRules(page.request);
    try {
      await open(page, baseURL, "#/schedule", { scenario: "write-mode" });
      await page.getByRole("button", { name: "New rule" }).click();
      await page.getByLabel("Name").fill("Evening cap");
      await page.getByRole("spinbutton", { name: "Set to", exact: true }).fill("600");
      await page.getByRole("spinbutton", { name: "Then set to" }).fill("800");
      await expect(page.getByTestId("schedule-preview")).not.toContainText("Checking");
      await expect(page.getByTestId("schedule-save")).toBeEnabled();
      await page.getByTestId("schedule-save").click();
      await expect(page.locator("[data-rule]").filter({ hasText: "Evening cap" })).toBeVisible();
      await shoot(page, "dashboard-schedule");
    } finally {
      await clearScheduleRules(page.request);
    }
  });

  test("dashboard-diagnose", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/diagnose", { scenario: "write-mode" });
    await page.locator('[data-profile="hardware"]').click();
    await expect(page.locator(".card", { hasText: "Root causes" }).first()).toBeVisible();
    await shoot(page, "dashboard-diagnose");
  });

  test("dashboard-logs", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/logs", { scenario: "write-mode" });
    await expect(page.getByTestId("logs-output")).toContainText("control_cycle");
    await shoot(page, "dashboard-logs");
  });

  test("dashboard-maintenance", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/maintenance", { scenario: "write-mode" });
    await expect(page.locator("table.data tbody tr")).toHaveCount(3);
    await shoot(page, "dashboard-maintenance");
  });

  test("dashboard-appearance-menu", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/overview");
    await waitForChart(page);
    await page.getByTestId("appearance-button").click();
    const menu = page.getByTestId("appearance");
    await expect(menu).toBeVisible();
    await settled(menu);
    await shoot(page, "dashboard-appearance-menu");
  });

  test("dashboard-appearance-themes", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/overview");
    await expect(page.getByTestId("kpi-status")).toHaveAttribute("data-level", "ok");
    const graphite = await column(page, 0);
    await pickAppearance(page, "Midnight");
    await expect(page.locator("html")).toHaveAttribute("data-m-theme", "midnight");
    const midnight = await column(page, 0);
    await strip(page, "dashboard-appearance-themes", [graphite, midnight]);
  });

  test("dashboard-appearance-density", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/devices");
    await expect(page.getByTestId("device-card")).toHaveCount(2);
    const x = Math.round((await page.locator(".sidebar").boundingBox()).width);
    const normal = await column(page, x);
    await pickAppearance(page, "Compact");
    await expect(page.locator("html")).toHaveAttribute("data-density", "compact");
    const compact = await column(page, x);
    await strip(page, "dashboard-appearance-density", [normal, compact]);
  });

  test("preview-aggregated", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/overview");
    await expect(page.getByTestId("kpi-status")).toHaveAttribute("data-level", "ok");
    await waitForChart(page);
    await preview(page, "preview-aggregated");
  });

  test("preview-devices", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/devices");
    await expect(page.getByTestId("device-card")).toHaveCount(2);
    await waitForChart(page);
    await preview(page, "preview-devices");
  });

  test("preview-energy", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/energy");
    await expect(page.getByTestId("energy-periods")).toContainText("Last 7 days");
    await page.getByRole("button", { name: "Table" }).click();
    await expect(page.locator("table.data tbody tr")).toHaveCount(12);
    await preview(page, "preview-energy");
  });

  test("preview-analytics", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/analytics");
    await waitForChart(page);
    await page.getByTestId("analytics-view-block").getByRole("button", { name: "Grid", exact: true }).first().click();
    await expect(page.locator(".kpi").filter({ hasText: "Grid import · 24 h" })).toBeVisible();
    await waitForChart(page);
    await preview(page, "preview-analytics");
  });

  test("preview-control", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/control", { scenario: "write-mode" });
    await expect(page.getByTestId("runtime-editor")).toBeVisible();
    await preview(page, "preview-control");
  });

  test("preview-diagnose", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/diagnose", { scenario: "offline-device", signedIn: true });
    await page.locator('[data-profile="hardware"]').click();
    await expect(page.locator(".card", { hasText: "Root causes" }).first()).toBeVisible();
    await preview(page, "preview-diagnose");
  });

  test("preview-logs", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/logs", { scenario: "offline-device", signedIn: true });
    await expect(page.getByTestId("logs-output")).toContainText("device_marked_offline");
    await preview(page, "preview-logs");
  });

  test("preview-maintenance", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/maintenance", { scenario: "write-mode" });
    await expect(page.locator("table.data tbody tr")).toHaveCount(3);
    await page.getByRole("button", { name: "Check config" }).click();
    await expect(page.getByText("Added keys")).toBeVisible();
    await preview(page, "preview-maintenance");
  });
});
