// SPDX-License-Identifier: AGPL-3.0-or-later
// Choosing a palette, an object style and a density on the cockpit, and the
// part of it that cannot be checked by reading files: that the stored choice is
// in place before anything paints, that it comes from the early script rather
// than from app.js noticing later, and that each axis leaves the other two
// alone.
//
// The cockpit is the surface where this matters most. It is the one that gets
// left on a screen in a room, so a flash of the default on every reload is
// something people see all day, and it is the one a tablet in the hall and a
// laptop at the desk want at different densities.
//
// Never `waitUntil: "networkidle"` here: /api/events is an open SSE stream, so
// the network is never idle and every navigation would sit out its timeout.
import { expect, test } from "@playwright/test";

const THEME_KEY = "ems-dashboard-theme";

// The preview serves the real index.html at both / and /preview/<view>. The
// plain root is the one a person opens, so that is what these use.
async function open(page) {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.locator("#themeSelect")).toBeVisible();
}

test.describe("theme switching @smoke", () => {
  test("a chosen palette applies and survives a reload", async ({ page }) => {
    await open(page);

    await page.selectOption("#themeSelect", "void");
    await expect(page.locator("html")).toHaveAttribute("data-theme", "void");

    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.locator("html")).toHaveAttribute("data-theme", "void");
    await expect(page.locator("#themeSelect")).toHaveValue("void");
  });

  test("the palette is applied even when app.js never arrives", async ({ page }) => {
    // The point of the separate early script: without it the page would paint
    // the default and change a moment later. Blocking app.js is the only way to
    // prove which half did the work.
    await open(page);
    await page.selectOption("#themeSelect", "copper");
    await expect(page.locator("html")).toHaveAttribute("data-theme", "copper");

    await page.route("**/app.js", (route) => route.abort());
    await page.reload({ waitUntil: "domcontentloaded" });

    await expect(page.locator("html")).toHaveAttribute("data-theme", "copper");
    await page.unroute("**/app.js");
  });

  test("a stored name this build does not know falls back instead of breaking", async ({
    page,
  }) => {
    await open(page);
    await page.evaluate((key) => window.localStorage.setItem(key, "harlequin"), THEME_KEY);
    await page.reload({ waitUntil: "domcontentloaded" });

    // The early script sets the attribute; nothing matches it, so :root holds.
    // app.js then corrects both the attribute and the menu to the default.
    await expect(page.locator("#themeSelect")).toHaveValue("signal");
    await expect(page.locator("html")).toHaveAttribute("data-theme", "signal");
  });

  test("an object style is a second axis, not a second palette", async ({ page }) => {
    await open(page);
    await page.selectOption("#themeSelect", "void");
    await page.selectOption("#styleSelect", "slab");

    await expect(page.locator("html")).toHaveAttribute("data-theme", "void");
    await expect(page.locator("html")).toHaveAttribute("data-style", "slab");

    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.locator("html")).toHaveAttribute("data-theme", "void");
    await expect(page.locator("html")).toHaveAttribute("data-style", "slab");
    await expect(page.locator("#themeSelect")).toHaveValue("void");
    await expect(page.locator("#styleSelect")).toHaveValue("slab");
  });

  test("the object style reaches an actual corner", async ({ page }) => {
    // Reading the token back would only prove the token changed. This reads a
    // rendered corner, which is the thing a person sees. The cockpit re-renders
    // on every poll, so expect.poll resolves the locator afresh each attempt --
    // a handle taken once can detach midway.
    const corner = () =>
      page.locator(".metric").first().evaluate((node) => getComputedStyle(node).borderTopLeftRadius);

    await open(page);
    await expect.poll(corner).not.toBe("0px");

    await page.selectOption("#styleSelect", "slab");
    await expect.poll(corner).toBe("0px");
  });

  test("density is a third axis, independent of the other two", async ({ page }) => {
    // Three attributes on one element, and what matters is that setting one
    // leaves the other two exactly where they were.
    await open(page);
    await page.selectOption("#themeSelect", "phosphor");
    await page.selectOption("#styleSelect", "console");
    await page.selectOption("#densitySelect", "compact");

    await expect(page.locator("html")).toHaveAttribute("data-theme", "phosphor");
    await expect(page.locator("html")).toHaveAttribute("data-style", "console");
    await expect(page.locator("html")).toHaveAttribute("data-density", "compact");

    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.locator("#themeSelect")).toHaveValue("phosphor");
    await expect(page.locator("#styleSelect")).toHaveValue("console");
    await expect(page.locator("#densitySelect")).toHaveValue("compact");
  });

  test("the density reaches an actual distance", async ({ page }) => {
    // The scalar is only worth something if it arrives at a painted pixel, and
    // it has to arrive in both directions -- a density that only tightened
    // would be half an axis.
    const padding = () =>
      page
        .locator(".metric")
        .first()
        .evaluate((node) => parseFloat(getComputedStyle(node).paddingTop));

    await open(page);
    await expect.poll(padding).toBeGreaterThan(0);
    const normal = await padding();

    await page.selectOption("#densitySelect", "compact");
    await expect.poll(padding).toBeLessThan(normal);

    await page.selectOption("#densitySelect", "roomy");
    await expect.poll(padding).toBeGreaterThan(normal);
  });

  test("the three choices are stored apart", async ({ page }) => {
    // One key for all three would make "void, square, compact" unrepresentable,
    // which is the thing the whole arrangement exists for. Reading the store
    // back is the only way to see that three keys were really written.
    await open(page);
    await page.selectOption("#themeSelect", "oxide");
    await page.selectOption("#styleSelect", "halo");
    await page.selectOption("#densitySelect", "roomy");

    const stored = await page.evaluate(() => ({
      theme: window.localStorage.getItem("ems-dashboard-theme"),
      style: window.localStorage.getItem("ems-dashboard-style"),
      density: window.localStorage.getItem("ems-dashboard-density"),
    }));
    expect(stored).toEqual({ theme: "oxide", style: "halo", density: "roomy" });
  });
});
