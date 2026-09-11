// SPDX-License-Identifier: AGPL-3.0-or-later
// Choosing a theme, and the part of it that cannot be checked by reading files:
// that the stored choice is in place before anything paints, and that it comes
// from the early script rather than from admin.js noticing later.
import { expect, test } from "@playwright/test";

const STORAGE_KEY = "ems-admin-theme";

test.describe("theme switching", () => {
  test("a chosen theme applies and survives a reload", async ({ page }) => {
    await page.goto("/");
    await expect(page.locator("#theme-select")).toBeVisible();

    await page.selectOption("#theme-select", "void");
    await expect(page.locator("html")).toHaveAttribute("data-theme", "void");

    await page.reload();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "void");
    await expect(page.locator("#theme-select")).toHaveValue("void");
  });

  test("the theme is applied even when admin.js never arrives", async ({ page }) => {
    // The point of the separate early script: without it the page would paint
    // the default and change a moment later. Blocking admin.js is the only way
    // to prove which half did the work.
    await page.goto("/");
    await page.selectOption("#theme-select", "copper");
    await expect(page.locator("html")).toHaveAttribute("data-theme", "copper");

    await page.route("**/admin.js", (route) => route.abort());
    await page.reload();

    await expect(page.locator("html")).toHaveAttribute("data-theme", "copper");
    await page.unroute("**/admin.js");
  });

  test("a stored name this build does not know falls back instead of breaking", async ({
    page,
  }) => {
    await page.goto("/");
    await page.evaluate((key) => window.localStorage.setItem(key, "harlequin"), STORAGE_KEY);
    await page.reload();

    // The early script sets the attribute; nothing matches it, so :root holds.
    // admin.js then corrects both the attribute and the menu to the default.
    await expect(page.locator("#theme-select")).toHaveValue("signal");
    await expect(page.locator("html")).toHaveAttribute("data-theme", "signal");
  });

  test("the theme can be set before signing in", async ({ page }) => {
    await page.goto("/");
    await expect(page.locator("#view-auth")).toBeVisible();
    await expect(page.locator("#theme-select")).toBeVisible();
  });
});
