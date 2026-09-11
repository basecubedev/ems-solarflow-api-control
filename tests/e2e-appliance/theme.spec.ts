// SPDX-License-Identifier: AGPL-3.0-or-later
// Choosing a palette in the Manager, and the part of it that cannot be checked
// by reading files: that the stored choice is in place before anything paints,
// and that it comes from the early script rather than from app.js noticing
// later.
import { expect, test } from "@playwright/test";
import { resetAppliance, signIn } from "./helpers";

const STORAGE_KEY = "ems-appliance-theme";

test.beforeEach(async ({ request }) => {
  await resetAppliance(request);
});

test.describe("theme switching @smoke", () => {
  test("a chosen palette applies and survives a reload", async ({ page }) => {
    await signIn(page);
    await expect(page.locator("#theme-select")).toBeVisible();

    await page.selectOption("#theme-select", "void");
    await expect(page.locator("html")).toHaveAttribute("data-theme", "void");

    await page.reload();
    await expect(page.locator("#shell")).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "void");
    await expect(page.locator("#theme-select")).toHaveValue("void");
  });

  test("the palette is applied even when app.js never arrives", async ({ page }) => {
    // The point of the separate early script: without it the page would paint
    // the default and change a moment later. Blocking app.js is the only way to
    // prove which half did the work.
    await signIn(page);
    await page.selectOption("#theme-select", "copper");
    await expect(page.locator("html")).toHaveAttribute("data-theme", "copper");

    await page.route("**/static/app.js", (route) => route.abort());
    await page.reload();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "copper");
    await page.unroute("**/static/app.js");
  });

  test("the palette reaches the sign-in gate, which has no menu of its own", async ({ page }) => {
    // The gate is what an owner sees when the appliance is in trouble, and it
    // renders before there is a session to ask about preferences.
    await signIn(page);
    await page.selectOption("#theme-select", "phosphor");

    await resetAppliance(page.request, { expire_sessions: true });
    await page.reload();

    await expect(page.locator("#gate")).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "phosphor");
  });

  test("a stored name this build does not know falls back instead of breaking", async ({
    page,
  }) => {
    await signIn(page);
    await page.evaluate((key) => window.localStorage.setItem(key, "harlequin"), STORAGE_KEY);
    await page.reload();

    // The early script sets the attribute; nothing matches it, so :root holds.
    // app.js then corrects both the attribute and the menu to the default.
    await expect(page.locator("#theme-select")).toHaveValue("signal");
    await expect(page.locator("html")).toHaveAttribute("data-theme", "signal");
  });
});
