// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Documentation screenshots for docs/user/appliance/.
//
// Not part of the normal suite: it is selected by name, writes into
// docs/assets/screenshots/appliance/ and asserts nothing beyond the page being
// the one it claims to photograph. Run it with
//
//   npx playwright test --config=playwright.appliance.config.ts capture-docs
//
// Every capture ID here has a row in that directory's README.md.

import { test, expect, type Page } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { PASSWORD, openView, resetAppliance, setMode, signIn } from "./helpers";

const SHOTS = "docs/assets/screenshots/appliance";

test.describe("documentation captures @docs", () => {
  test.beforeAll(() => {
    mkdirSync(SHOTS, { recursive: true });
  });

  test.beforeEach(async ({ request }) => {
    await resetAppliance(request);
  });

  test("appliance-first-start-password", async ({ page }) => {
    await page.goto("/classic/");
    await expect(page.locator("#gate-title")).toBeVisible();
    await expect(page.locator("#gate-confirm-field")).toBeVisible();
    await page.screenshot({ path: `${SHOTS}/appliance-first-start-password.png` });
  });

  test("appliance-login", async ({ page, request }) => {
    await signIn(page);
    await resetAppliance(request, { expire_sessions: true });
    await page.goto("/classic/");
    await expect(page.locator("#gate-title")).toBeVisible();
    await expect(page.locator("#gate-confirm-field")).toBeHidden();
    await page.screenshot({ path: `${SHOTS}/appliance-login.png` });
  });

  test("appliance-overview", async ({ page }) => {
    await signIn(page);
    await expect(page.locator('[data-test="card-admin"]')).toBeVisible();
    await page.screenshot({ path: `${SHOTS}/appliance-overview.png`, fullPage: true });
  });

  test("appliance-update-plan", async ({ page }) => {
    await signIn(page);
    await setMode(page, "expert");
    await openView(page, "updates");
    await page.locator('[data-test="install-channel"]').selectOption("exact");
    await page.locator('[data-test="install-tag"]').fill("v1.1.0");
    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/admin/plan-install")),
      page.locator('[data-test="install-plan"]').click(),
    ]);
    await expect(page.locator("#dialog")).toBeVisible();
    await page.screenshot({ path: `${SHOTS}/appliance-update-plan.png` });
  });

  test("appliance-update-running", async ({ page }) => {
    await signIn(page);
    await openView(page, "admin");
    await page.locator('[data-test="admin-restart"]').click();
    await expect(page.locator("#dialog")).toBeVisible();
    await page.locator("#dialog-cancel").click();
    await expect(page.locator('[data-test="operation-stage"]')).toBeVisible();
    await page.screenshot({ path: `${SHOTS}/appliance-update-running.png`, fullPage: true });
  });


  test("appliance-network-wifi", async ({ page }) => {
    await signIn(page);
    await openView(page, "network");
    await page.screenshot({ path: `${SHOTS}/appliance-network-wifi.png`, fullPage: true });
  });

  test("appliance-backup-access", async ({ page }) => {
    await signIn(page);
    await openView(page, "access");
    await page.screenshot({ path: `${SHOTS}/appliance-backup-access.png`, fullPage: true });
  });

  test("appliance-recovery", async ({ page }) => {
    await signIn(page);
    await openView(page, "admin");
    await page.screenshot({ path: `${SHOTS}/appliance-recovery.png`, fullPage: true });
  });
});

test.describe("Modern documentation captures @docs", () => {
  test.beforeAll(() => {
    mkdirSync(SHOTS, { recursive: true });
  });

  test.beforeEach(async ({ request }) => {
    await resetAppliance(request, { manager_index: true, manager_package_kept: true });
  });

  async function modernSignIn(page: Page) {
    await page.goto("/");
    await page.locator("#appliance-password").fill(PASSWORD);
    await page.locator("#appliance-password-confirm").fill(PASSWORD);
    await page.getByTestId("sign-in-submit").click();
    await expect(page.getByTestId("card-network")).toContainText("ems-solarflow.local");
  }

  test("appliance-modern-overview", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await modernSignIn(page);
    await page.screenshot({ path: `${SHOTS}/appliance-modern-overview.png` });
  });

  test("appliance-modern-phone", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await modernSignIn(page);
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: `${SHOTS}/appliance-modern-phone.png` });
  });
});
