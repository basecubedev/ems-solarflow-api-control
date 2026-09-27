// SPDX-License-Identifier: AGPL-3.0-or-later
// The Energy board's detail switch: Basic is the board as it was, Expert adds
// the channel rows to the same cards. The part that cannot be checked by
// reading files is that the choice survives a reload and that an unmeasured
// period stays unmeasured in both levels -- a zero there would be a claim
// nobody measured.
//
// Never `waitUntil: "networkidle"` here: /api/events is an open SSE stream, so
// the network is never idle and every navigation would sit out its timeout.
import { expect, test } from "@playwright/test";

const DETAIL_KEY = "dashboard.energyDetail";

async function openEnergy(page) {
  await page.goto("/preview/energy", { waitUntil: "domcontentloaded" });
  await expect(page.locator(".energy-period-stage").first()).toBeVisible();
}

function card(page, kind) {
  return page.locator(`.energy-period-${kind}`);
}

test.describe("energy detail switch @smoke", () => {
  test("opens in Basic and keeps the board it had", async ({ page }) => {
    await openEnergy(page);

    await expect(page.locator('[data-energy-detail="basic"]')).toHaveClass(/active/);
    await expect(card(page, "today")).toContainText("Energy");
    await expect(card(page, "today")).toContainText("Savings");
    await expect(card(page, "today")).not.toContainText("Grid Import");
  });

  test("Expert adds the channels to the same card", async ({ page }) => {
    await openEnergy(page);

    await page.click('[data-energy-detail="expert"]');

    const today = card(page, "today");
    await expect(today).toContainText("Grid Import");
    await expect(today).toContainText("2.6 kWh");
    await expect(today).toContainText("Charged");
    await expect(today).toContainText("Self-Sufficiency");
    // The months and years carry the same rows, not a second layout. The
    // preview measures the channels from September on, so September and the
    // current year are the entries that have them.
    await expect(page.locator(".energy-month-card").nth(8)).toContainText("Grid Import");
    await expect(page.locator(".energy-year-card").nth(1)).toContainText("Grid Export");
    // A month before the measurement says so instead of listing six blanks.
    await expect(page.locator(".energy-month-card").first()).toContainText("not measured");
    await expect(page.locator(".energy-month-card").first()).not.toContainText("Grid Import");
  });

  test("a period nobody measured says so once", async ({ page }) => {
    await openEnergy(page);
    await page.click('[data-energy-detail="expert"]');

    const best = card(page, "best");
    await expect(best).toContainText("not measured");
    await expect(best).not.toContainText("Grid Import");
    await expect(best).not.toContainText("0.0 kWh");
  });

  test("a partly measured period marks its figures", async ({ page }) => {
    await openEnergy(page);
    await page.click('[data-energy-detail="expert"]');

    const rolling = card(page, "month");
    await expect(rolling).toContainText("28.9 kWh ◦");
    await expect(rolling).toContainText("Measured since");
    await expect(rolling).toContainText("2026-09-12");
  });

  test("the Analytics tabs leave the switch alone", async ({ page }) => {
    // Both are segmented controls in the same page. While they shared a class,
    // the Analytics view's global button query wired its own click handler to
    // these two buttons and cleared their state on every analytics render.
    await openEnergy(page);
    await page.click('[data-energy-detail="expert"]');

    await page.click('[data-flow-view="analytics"]');
    await page.click('[data-analytics-tab="grid"]');
    await page.click('[data-flow-view="energy"]');

    await expect(page.locator('[data-energy-detail="expert"]')).toHaveClass(/active/);
    await expect(card(page, "today")).toContainText("Grid Import");
  });

  test("the chosen level survives a reload", async ({ page }) => {
    await openEnergy(page);

    await page.click('[data-energy-detail="expert"]');
    await expect(card(page, "today")).toContainText("Grid Import");

    await page.reload({ waitUntil: "domcontentloaded" });

    await expect(page.locator('[data-energy-detail="expert"]')).toHaveClass(/active/);
    await expect(card(page, "today")).toContainText("Grid Import");
    expect(await page.evaluate((key) => window.localStorage.getItem(key), DETAIL_KEY)).toBe(
      "expert",
    );
  });
});
