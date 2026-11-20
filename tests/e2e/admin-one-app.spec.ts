// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
// The console is the product's one address: the dashboard's pages and its own
// in one menu. The dashboard's pages read the EMS through the console -- here
// the dashboard preview server, which playwright.config.ts starts beside the
// test-mode Admin -- and are readable signed out; the console's pages need its
// sign-in, and that one sign-in signs in to the EMS as well.
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

  test("signed out of the console, a dashboard page's sign-in is the one sign-in, and an EMS session of its own is ended", async ({ page, baseURL }) => {
    await installConfig(baseURL);
    let emsSignedIn = true;
    await page.route("**/api/auth/status", (route) =>
      route.fulfill({ json: { auth_configured: true, authenticated: emsSignedIn, csrf_token: "preview-csrf-token", write_mode_available: true, write_mode_active: emsSignedIn } }),
    );
    const ended = page.waitForRequest((sent) => sent.url().endsWith("/api/auth/logout") && sent.method() === "POST");
    await page.route("**/api/auth/logout", (route) => {
      emsSignedIn = false;
      return route.fulfill({ json: { ok: true } });
    });
    await page.goto("/#/schedule");
    await ended;
    await expect(page.locator(".content").getByRole("button", { name: "Sign in" })).toBeVisible();
    await page.locator(".content").getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByTestId("sign-in-screen")).toHaveAttribute("data-mode", "login");
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

  test("a browser too old for the app is told so instead of a blank page", async ({ page }) => {
    await page.addInitScript(() => {
      delete (HTMLElement.prototype as unknown as Record<string, unknown>).popover;
    });
    await page.goto("/");
    await expect(page).toHaveURL(/\/unsupported\.html$/);
    await expect(page.getByTestId("browser-too-old")).toContainText("Chrome or Edge 114");
  });
});
