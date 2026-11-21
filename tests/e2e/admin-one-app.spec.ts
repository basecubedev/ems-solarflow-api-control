// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
// The console is the product's one address: the dashboard's pages and its own
// in one menu. The dashboard's pages read the EMS through the console -- here
// the dashboard preview server, which playwright.config.ts starts beside the
// test-mode Admin -- and are readable signed out; the console's pages and the
// dashboard's operator pages need the console's sign-in, the only one. That
// the console vouches for its operator to a real EMS, and only through its
// socket, is held by tests/test_ems_forwarding.py; the preview server takes it
// as given.
import { expect, test, ADMIN_PASSWORD } from "./fixtures/admin";
import { request as apiRequest, type Page } from "@playwright/test";

async function installConfig(baseURL: string | undefined) {
  const ctx = await apiRequest.newContext({ baseURL });
  const login = await ctx.post("/api/admin/auth/login", { data: { password: ADMIN_PASSWORD } });
  const { csrf_token: csrf } = await login.json();
  const seeded = await ctx.post("/api/admin/test/seed", { headers: { "X-CSRF-Token": csrf }, data: { scenario: "mixed_transports" } });
  expect(seeded.ok()).toBeTruthy();
  await ctx.post("/api/admin/auth/logout", { headers: { "X-CSRF-Token": csrf } });
  await ctx.dispose();
}

async function signIn(page: Page) {
  await expect(page.getByTestId("sign-in-screen")).toHaveAttribute("data-mode", "login");
  await page.fill("#admin-password", ADMIN_PASSWORD);
  await page.getByTestId("sign-in-submit").click();
  await expect(page.locator(".app")).toBeVisible();
}

test.describe("One app for the dashboard and the console", { tag: ["@authority"] }, () => {
  test("signed out, the address opens the dashboard; a console page asks for the sign-in and lets the reader go back", async ({ page, baseURL }) => {
    await installConfig(baseURL);
    await page.goto("/");
    await expect(page.getByTestId("energy-flow")).toBeVisible();
    const menu = page.locator("aside.sidebar nav");
    await expect(menu.getByText("Dashboard", { exact: true })).toBeVisible();
    await expect(menu.getByText("Admin", { exact: true })).toBeVisible();
    await expect(menu.locator('a[data-page="status"] .lock')).toBeVisible();
    await expect(menu.locator('a[data-page="energy"] .lock')).toHaveCount(0);

    await menu.locator('a[data-page="status"]').click();
    await expect(page.getByTestId("sign-in-screen")).toBeVisible();
    await page.getByTestId("sign-in-continue").click();
    await expect(page).toHaveURL(/#\/overview$/);
    await expect(page.getByTestId("energy-flow")).toBeVisible();
  });

  test("one sign-in opens the console's pages and the dashboard's operator pages", async ({ page, baseURL }) => {
    await installConfig(baseURL);
    await page.goto("/#/status");
    await signIn(page);
    await expect(page.getByTestId("health-state")).toBeVisible();
    await page.locator('aside.sidebar a[data-page="schedule"]').click();
    await expect(page.getByRole("button", { name: "New rule" })).toBeVisible();
    await expect(page.locator(".content")).not.toContainText("Sign in to see this page.");
    await expect(page.locator('aside.sidebar a[data-page="schedule"] .lock')).toHaveCount(0);
  });

  test("an installation without an EMS says so on the dashboard's pages and leads to the assistant", async ({ page }) => {
    await page.goto("/");
    const unavailable = page.getByTestId("ems-unavailable");
    await expect(unavailable).toHaveAttribute("data-state", "ems_not_configured");
    await page.getByTestId("ems-unavailable-next").click();
    await expect(page).toHaveURL(/#\/assistant$/);
    await signIn(page);
    await expect(page.locator(".app")).toBeVisible();
  });

  test("an old address of the console or the dashboard opens the same page here", async ({ page }) => {
    await page.goto("/#/status");
    await signIn(page);
    await page.goto("/classic/#maintenance-upgrade");
    await expect(page).toHaveURL(/\/#maintenance-upgrade$/);
    await expect(page.locator('aside.sidebar a[data-page="updates"]')).toHaveAttribute("aria-current", "page");
    await page.goto("/#/diagnose");
    await expect(page.locator('aside.sidebar a[data-page="diagnostics"]')).toHaveAttribute("aria-current", "page");
  });

  test("signed out, a dashboard page's sign-in is the console's, and the menu's sign-out ends it for every page", async ({ page, baseURL }) => {
    await installConfig(baseURL);
    await page.goto("/#/schedule");
    await expect(page.locator(".content").getByRole("button", { name: "Sign in" })).toBeVisible();
    await page.locator(".content").getByRole("button", { name: "Sign in" }).click();
    await signIn(page);
    await expect(page.getByRole("button", { name: "New rule" })).toBeVisible();
    await expect(page.locator('aside.sidebar a[data-page="schedule"] .lock')).toHaveCount(0);

    await page.getByTestId("sign-out").click();
    await expect(page.locator(".content").getByRole("button", { name: "Sign in" })).toBeVisible();
    await expect(page.locator('aside.sidebar a[data-page="schedule"] .lock')).toBeVisible();
  });

  test("a browser meets the one app once, and its notice stays away once understood", async ({ browser, baseURL }) => {
    const context = await browser.newContext({ baseURL });
    const fresh = await context.newPage();
    await fresh.goto("/");
    const notice = fresh.getByTestId("one-app-notice");
    await expect(notice).toContainText("Dashboard and Admin are one app now");
    await expect(notice).toContainText("old dashboard address");
    await fresh.getByTestId("one-app-dismiss").click();
    await expect(notice).toHaveCount(0);
    await fresh.reload();
    await expect(fresh.locator("aside.sidebar nav")).toBeVisible();
    await expect(fresh.getByTestId("one-app-notice")).toHaveCount(0);
    await context.close();
  });

  test("an EMS that does not take the console's sign-in says why: older, or reached without its socket", async ({ page, baseURL }) => {
    await installConfig(baseURL);
    await page.route("**/api/ui-config", async (route) => {
      const response = await route.fetch();
      const { console_operator: _, ...older } = await response.json();
      await route.fulfill({ response, json: older });
    });
    await page.goto("/#/energy");
    await expect(page.getByTestId("ems-too-old")).toContainText("older than the console");
    await page.unroute("**/api/ui-config");
    await page.route("**/api/ui-config", async (route) => {
      const response = await route.fetch();
      await route.fulfill({ response, json: { ...(await response.json()), console_operator: false } });
    });
    await page.reload();
    await expect(page.getByTestId("ems-unlinked")).toContainText("socket file");
    await expect(page.getByTestId("ems-too-old")).toHaveCount(0);
    await page.unroute("**/api/ui-config");
    await page.reload();
    await expect(page.getByTestId("ems-too-old")).toHaveCount(0);
    await expect(page.getByTestId("ems-unlinked")).toHaveCount(0);
  });

  test("a dashboard page the EMS answered stays open while the EMS does not answer, and says so", async ({ page, baseURL }) => {
    await installConfig(baseURL);
    await page.clock.install();
    await page.goto("/#/status");
    await signIn(page);
    await page.locator('aside.sidebar a[data-page="schedule"]').click();
    await page.getByRole("button", { name: "New rule" }).click();
    await page.getByLabel("Name").fill("Kept while the EMS restarts");
    await page.route("**/api/ui-config", (route) => route.fulfill({ status: 502, json: { error: "ems_unreachable", message: "The EMS does not answer." } }));
    await page.clock.fastForward(16_000);
    await expect(page.getByTestId("ems-interrupted")).toHaveAttribute("data-state", "ems_unreachable");
    await expect(page.getByLabel("Name")).toHaveValue("Kept while the EMS restarts");
    await page.unroute("**/api/ui-config");
    await page.clock.fastForward(16_000);
    await expect(page.getByTestId("ems-interrupted")).toHaveCount(0);
    await expect(page.getByLabel("Name")).toHaveValue("Kept while the EMS restarts");
  });

  test("signed in, a runtime state that does not answer is named on the Control page and asked again", async ({ page, baseURL }) => {
    await installConfig(baseURL);
    let failing = true;
    await page.route("**/api/runtime", (route) => (failing ? route.fulfill({ status: 503, json: { error: "unavailable" } }) : route.continue()));
    await page.goto("/#/status");
    await signIn(page);
    await page.locator('aside.sidebar a[data-page="control"]').click();
    await expect(page.getByText("The runtime state did not answer. Asking again…")).toBeVisible();
    failing = false;
    await expect(page.getByTestId("runtime-editor")).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText("The runtime state did not answer. Asking again…")).toHaveCount(0);
  });

  test("a browser too old for the app is told so instead of a blank page", async ({ page }) => {
    await page.addInitScript(() => {
      delete (HTMLElement.prototype as unknown as Record<string, unknown>).popover;
    });
    await page.goto("/");
    await expect(page).toHaveURL(/\/unsupported\.html$/);
    await expect(page.getByTestId("browser-too-old")).toContainText("Chrome or Edge 114");
  });
});
