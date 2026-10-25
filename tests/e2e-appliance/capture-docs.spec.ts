// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Every documentation screenshot of the Appliance Manager, all of them of the
// Modern console at the appliance's address: the PNGs under
// docs/assets/screenshots/appliance/ that the guides in docs/user/appliance/
// embed. One test per file, titled with the file's name;
// tests/test_appliance_screenshot_index.py and tests/test_docs_user_guides.py
// read those titles as the manifest, and the directory's README.md lists every
// file.
//
// Not part of the normal suite: it writes into tracked files, so
// playwright.appliance.config.ts leaves it out unless EMS_APPLIANCE_CAPTURE_DOCS
// is set. Run it with
//
//   EMS_APPLIANCE_CAPTURE_DOCS=1 npx playwright test \
//     --config=playwright.appliance.config.ts --project=chromium capture-docs
//
// The server is the browser suite's deterministic EMS_APPLIANCE_TEST_MODE host:
// a scripted Raspberry Pi with fixed versions, a fixed hostname and fixed
// containers. Each capture resets it into the state it shows with the options
// of /api/test/reset -- a freshly flashed card (never_installed), a package
// index to choose from (manager_index), an update the agent is still carrying
// out (operation_running). A guide PNG is the 1440x900 window (390x844 on a
// phone), or the window grown to the whole page at that width where a capture
// says so. The browser runs in English in Europe/Berlin with the clock fixed,
// so the times a page prints read the same on every capture. The test host
// keeps its files under a temporary directory with a random name, which a
// page that names a host path (the export root, the EMS data, the
// configuration file) would print; the captures read the status answers with
// that directory taken off, so a path reads as it does on an appliance and the
// same on every capture. The appearance
// strips are the same region of the same page photographed once per value of
// one axis and laid side by side on a blank page, so nothing but that axis
// changes.

import { expect, test, type Page } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { PASSWORD, resetAppliance } from "./helpers";

const SHOTS = "docs/assets/screenshots/appliance";
const DESKTOP = { width: 1440, height: 900 };
const PHONE = { width: 390, height: 844 };
const STRIP = { width: 714, height: 560, gap: 12 };
const DEMO_NOW = new Date("2026-07-05T13:30:00Z");
const DEFAULT_PREFS = {
  "ems-modern.lang": "en",
  "ems-modern.theme": "graphite",
  "ems-modern.density": "normal",
  "ems-modern.motion": "off",
};
const TEST_ROOT = /\/[^"\s]*?\/ems-appliance-e2e-[A-Za-z0-9_]+/g;
const DEMO_DISK = { total_mb: 59640, free_mb: 41210, used_mb: 18430, used_percent: 30.9 };
const PINNED_POSTS = /\/api\/updates\/plan$/;

/** The test host answers with the disk of the machine running it; a capture shows a 64 GB card instead. */
function demoDisk(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(demoDisk);
  if (!value || typeof value !== "object") return value;
  const out: Record<string, unknown> = {};
  for (const [key, entry] of Object.entries(value)) out[key] = demoDisk(entry);
  if ("free_mb" in out && "total_mb" in out && out.available !== false) Object.assign(out, DEMO_DISK);
  if (typeof out.free_megabytes === "number" && out.free_megabytes > 0) out.free_megabytes = DEMO_DISK.free_mb;
  return out;
}

test.use({ locale: "en-US", timezoneId: "Europe/Berlin", deviceScaleFactor: 1, reducedMotion: "reduce" });

async function prepare(page: Page, { viewport = DESKTOP, prefs = {} }: { viewport?: { width: number; height: number }; prefs?: Record<string, string> } = {}) {
  await page.setViewportSize(viewport);
  await page.addInitScript((stored) => {
    for (const [key, value] of Object.entries(stored)) window.localStorage.setItem(key, value);
  }, { ...DEFAULT_PREFS, ...prefs });
  await page.clock.setFixedTime(DEMO_NOW);
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    if (request.method() !== "GET" && !PINNED_POSTS.test(new URL(request.url()).pathname)) return route.continue();
    const response = await route.fetch();
    if (!(response.headers()["content-type"] || "").includes("json")) return route.fulfill({ response });
    const text = (await response.text()).replace(TEST_ROOT, "");
    await route.fulfill({ response, body: JSON.stringify(demoDisk(JSON.parse(text))) });
  });
}

async function signIn(page: Page, hash = "") {
  await page.goto(`/${hash}`);
  await expect(page.getByTestId("sign-in-screen")).toHaveAttribute("data-mode", "create");
  await page.locator("#appliance-password").fill(PASSWORD);
  await page.locator("#appliance-password-confirm").fill(PASSWORD);
  await Promise.all([
    page.waitForResponse((response) => response.url().includes("/api/session/setup") && response.ok()),
    page.getByTestId("sign-in-submit").click(),
  ]);
  await expect(page.locator(".app[data-landed]")).toBeVisible();
  await expect(page.getByTestId("health-state")).not.toHaveAttribute("data-tone", "idle");
  await page.evaluate(() => document.fonts.ready);
}

async function painted(page: Page) {
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}

async function settled(locator) {
  await locator.evaluate((node) => Promise.all(node.getAnimations({ subtree: true }).map((animation) => animation.finished)));
}

async function wholePage(page: Page) {
  const { width } = page.viewportSize();
  let height = 0;
  for (let next = DESKTOP.height; next !== height; ) {
    height = next;
    await page.setViewportSize({ width, height });
    await painted(page);
    next = Math.max(DESKTOP.height, await page.evaluate(() => document.documentElement.scrollHeight));
  }
}

async function shoot(page: Page, name: string, { whole = false } = {}) {
  if (whole) await wholePage(page);
  await painted(page);
  await page.screenshot({ path: `${SHOTS}/${name}.png`, animations: "disabled" });
}

async function overviewReady(page: Page) {
  await expect(page.getByTestId("card-network")).toContainText("ems-solarflow.local");
  await expect(page.getByTestId("card-admin")).toContainText("v1.0.0");
  await expect(page.getByTestId("card-updates")).toContainText("Security updates");
}

async function pickAppearance(page: Page, label: string) {
  await page.getByTestId("appearance-button").click();
  const menu = page.getByTestId("appearance");
  await expect(menu).toBeVisible();
  await menu.getByRole("button", { name: label, exact: true }).click();
  await page.keyboard.press("Escape");
  await expect(menu).toBeHidden();
  await painted(page);
}

async function column(page: Page, x: number) {
  await page.evaluate(() => window.scrollTo(0, 0));
  await painted(page);
  return page.screenshot({ clip: { x, y: 0, width: STRIP.width, height: STRIP.height }, animations: "disabled" });
}

async function strip(page: Page, name: string, columns: Buffer[]) {
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

test.describe("documentation captures @docs", () => {
  test.beforeAll(() => {
    mkdirSync(SHOTS, { recursive: true });
  });

  test("appliance-first-start-password", async ({ page, request }) => {
    await resetAppliance(request);
    await prepare(page);
    await page.goto("/");
    await expect(page.getByTestId("sign-in-screen")).toHaveAttribute("data-mode", "create");
    await expect(page.locator("#appliance-password-confirm")).toBeVisible();
    await page.evaluate(() => document.fonts.ready);
    await shoot(page, "appliance-first-start-password");
  });

  test("appliance-login", async ({ page, request }) => {
    await resetAppliance(request);
    await prepare(page);
    await signIn(page);
    await resetAppliance(request, { expire_sessions: true });
    await page.reload();
    await expect(page.getByTestId("sign-in-screen")).toHaveAttribute("data-mode", "login");
    await expect(page.locator("#appliance-password-confirm")).toHaveCount(0);
    await shoot(page, "appliance-login");
  });

  test("appliance-assistant-fresh", async ({ page, request }) => {
    await resetAppliance(request, { never_installed: true });
    await prepare(page);
    await signIn(page);
    await expect(page).toHaveURL(/\/#\/assistant$/);
    await expect(page.getByTestId("assistant-welcome")).toHaveAttribute("data-kind", "bootstrap");
    const steps = page.getByTestId("assistant-steps");
    await expect(steps.locator('[data-step="admin"]')).toHaveAttribute("aria-current", "step");
    await expect(steps.locator('[data-step="ems"]')).toHaveAttribute("data-state", "later");
    await expect(steps.locator('[data-step="backup"]')).toHaveAttribute("data-state", "todo");
    await shoot(page, "appliance-assistant-fresh", { whole: true });
  });

  test("appliance-assistant", async ({ page, request }) => {
    await resetAppliance(request);
    await prepare(page);
    await signIn(page);
    await expect(page).toHaveURL(/\/#\/assistant$/);
    await expect(page.getByTestId("assistant-welcome")).toHaveAttribute("data-kind", "setup");
    const steps = page.getByTestId("assistant-steps");
    await expect(steps.locator('[data-step="admin"]')).toHaveAttribute("data-state", "done");
    await expect(steps.locator('[data-step="ems"]')).toHaveAttribute("aria-current", "step");
    await expect(steps.locator('[data-step="backup"]')).toHaveAttribute("data-state", "todo");
    await expect(page.getByTestId("assistant-tasks").locator('[data-task="openAdmin"]')).toBeVisible();
    await shoot(page, "appliance-assistant", { whole: true });
  });

  test("appliance-overview", async ({ page, request }) => {
    await resetAppliance(request);
    await prepare(page);
    await signIn(page, "#/overview");
    await overviewReady(page);
    await shoot(page, "appliance-overview", { whole: true });
  });

  test("appliance-overview-phone", async ({ page, request }) => {
    await resetAppliance(request);
    await prepare(page, { viewport: PHONE });
    await signIn(page, "#/overview");
    await overviewReady(page);
    await page.evaluate(() => window.scrollTo(0, 0));
    await shoot(page, "appliance-overview-phone");
  });

  test("appliance-phone-more", async ({ page, request }) => {
    await resetAppliance(request);
    await prepare(page, { viewport: PHONE });
    await signIn(page, "#/overview");
    await overviewReady(page);
    await page.getByTestId("nav-more").click();
    const sheet = page.locator("dialog.sheet[open]");
    await expect(sheet).toBeVisible();
    await expect(sheet.getByTestId("customize-bar")).toBeVisible();
    await settled(sheet);
    await shoot(page, "appliance-phone-more");
  });

  test("appliance-admin-install", async ({ page, request }) => {
    await resetAppliance(request, { never_installed: true });
    await prepare(page);
    await signIn(page, "#/admin");
    await expect(page.getByTestId("admin-bootstrap-install")).toBeVisible();
    await expect(page.getByTestId("admin-bootstrap-plan")).toBeEnabled();
    await expect(page.getByTestId("install-channel")).toContainText("v1.1.0");
    await shoot(page, "appliance-admin-install");
  });

  test("appliance-updates", async ({ page, request }) => {
    await resetAppliance(request, { manager_index: true, manager_package_kept: true });
    await prepare(page);
    await signIn(page, "#/updates");
    await expect(page.getByTestId("update-manager-latest")).toBeVisible();
    await expect(page.getByTestId("update-admin-latest")).toBeVisible();
    await expect(page.getByTestId("update-os-security")).toBeVisible();
    await expect(page.getByTestId("manager-version")).toBeVisible();
    await shoot(page, "appliance-updates", { whole: true });
  });

  test("appliance-update-plan", async ({ page, request }) => {
    await resetAppliance(request, { manager_index: true, manager_package_kept: true });
    await prepare(page);
    await signIn(page, "#/updates");
    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/updates/plan") && response.ok()),
      page.getByTestId("update-os-security").click(),
    ]);
    const dialog = page.getByTestId("plan-dialog");
    await expect(dialog).toBeVisible();
    await expect(page.getByTestId("package-table")).toContainText("openssl");
    await settled(page.getByRole("dialog"));
    await shoot(page, "appliance-update-plan");
  });

  test("appliance-update-running", async ({ page, request }) => {
    await resetAppliance(request, { manager_index: true, manager_package_kept: true, operation_running: true });
    await prepare(page);
    await signIn(page, "#/updates");
    await expect(page.getByTestId("operation-banner")).toHaveAttribute("data-state", "running");
    await expect(page.getByTestId("operation-stage")).toHaveText("installing");
    await shoot(page, "appliance-update-running");
  });

  test("appliance-recovery", async ({ page, request }) => {
    await resetAppliance(request);
    await prepare(page);
    await signIn(page, "#/admin");
    await expect(page.getByTestId("admin-version")).toContainText("v1.0.0");
    await expect(page.getByTestId("admin-known-good")).toContainText("v0.9.0");
    await expect(page.getByTestId("admin-rollback")).toBeEnabled();
    await shoot(page, "appliance-recovery", { whole: true });
  });

  test("appliance-network-wifi", async ({ page, request }) => {
    await resetAppliance(request);
    await prepare(page);
    await signIn(page, "#/network");
    await expect(page.getByTestId("network-hostname")).toContainText("ems-solarflow.local");
    await page.getByTestId("wifi-scan").click();
    await expect(page.getByTestId("wifi-select")).toContainText("GuestNet");
    await page.getByTestId("wifi-select").selectOption("GuestNet");
    await expect(page.getByTestId("wifi-ssid")).toHaveValue("GuestNet");
    await page.getByTestId("wifi-select").blur();
    await shoot(page, "appliance-network-wifi", { whole: true });
  });

  test("appliance-backup-access", async ({ page, request }) => {
    await resetAppliance(request);
    await prepare(page);
    await signIn(page, "#/access");
    await expect(page.getByTestId("backup-account")).toContainText("ems-backup");
    await expect(page.getByTestId("backup-paths")).toBeVisible();
    await shoot(page, "appliance-backup-access", { whole: true });
  });

  test("appliance-diagnostics", async ({ page, request }) => {
    await resetAppliance(request);
    await prepare(page);
    await signIn(page, "#/diagnostics");
    await expect(page.getByTestId("diag-memory")).toContainText("%");
    await expect(page.getByTestId("log-source")).toBeVisible();
    await shoot(page, "appliance-diagnostics", { whole: true });
  });

  test("appliance-settings", async ({ page, request }) => {
    await resetAppliance(request);
    await prepare(page);
    await signIn(page, "#/settings");
    await expect(page.getByTestId("settings-appliance")).toContainText("8090");
    await expect(page.getByTestId("settings-password")).toBeVisible();
    await shoot(page, "appliance-settings", { whole: true });
  });

  test("appliance-appearance-menu", async ({ page, request }) => {
    await resetAppliance(request);
    await prepare(page);
    await signIn(page, "#/overview");
    await overviewReady(page);
    await page.getByTestId("appearance-button").click();
    const menu = page.getByTestId("appearance");
    await expect(menu).toBeVisible();
    await settled(menu);
    await shoot(page, "appliance-appearance-menu");
  });

  test("appliance-appearance-themes", async ({ page, request }) => {
    await resetAppliance(request);
    await prepare(page);
    await signIn(page, "#/overview");
    await overviewReady(page);
    const graphite = await column(page, 0);
    await pickAppearance(page, "Midnight");
    await expect(page.locator('html[data-m-theme="midnight"]')).toBeAttached();
    const midnight = await column(page, 0);
    await strip(page, "appliance-appearance-themes", [graphite, midnight]);
  });

  test("appliance-appearance-density", async ({ page, request }) => {
    await resetAppliance(request);
    await prepare(page);
    await signIn(page, "#/overview");
    await overviewReady(page);
    const x = Math.round((await page.locator(".sidebar").boundingBox()).width);
    const normal = await column(page, x);
    await pickAppearance(page, "Compact");
    await expect(page.locator('html[data-density="compact"]')).toBeAttached();
    const compact = await column(page, x);
    await strip(page, "appliance-appearance-density", [normal, compact]);
  });
});
