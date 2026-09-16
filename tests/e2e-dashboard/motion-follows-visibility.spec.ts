// SPDX-License-Identifier: AGPL-3.0-or-later
// The cockpit draws its energy flow with one continuous animation per pipe
// segment, plus a pulse on each sun and battery fill -- 153 of them at twelve
// devices. Each is ticked on the main thread for every frame it runs, and the
// Web Animations API keeps running one whose element is in a switched-away
// view.
//
// Measured on the preview with twelve devices: the control view, which shows no
// flow drawing at all, left ninety animations running and cost 625 ms of style
// recalculation per eight seconds. That is main thread the browser then does
// not have for rasterising what a scroll brings into view.
//
// Never `waitUntil: "networkidle"` here: /api/events is an open SSE stream.
import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

// Endless animations belonging to the energy flow specifically. Other parts of
// the cockpit run their own continuous motion (the control result ring), and
// those are on screen and meant to keep going.
async function flowMotionRunning(page: Page): Promise<number> {
  return page.evaluate(() => {
    const carriers = "#flowSvg, #deviceFlowView, div.flow-tile-layer";
    return document.getAnimations().filter((animation) => {
      if (animation.playState !== "running") return false;
      const target = animation.effect && (animation.effect as KeyframeEffect).target;
      if (!target || !target.closest) return false;
      if (!target.closest(carriers) && !target.matches(carriers)) return false;
      try {
        return animation.effect!.getComputedTiming().iterations === Infinity;
      } catch (_) {
        return false;
      }
    }).length;
  });
}

test.describe("motion follows visibility @smoke", () => {
  test("a view with no flow drawing on screen runs no flow animation", async ({ page }) => {
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await expect(page.locator("#flowSvg")).toBeVisible();
    await expect.poll(() => flowMotionRunning(page)).toBeGreaterThan(0);

    await page.click('[data-flow-view="control"]');
    await expect(page.locator("#controlExplainMount")).toBeVisible();
    await expect.poll(() => flowMotionRunning(page), { timeout: 10_000 }).toBe(0);

    // And coming back brings it to life again.
    await page.click('[data-flow-view="aggregated"]');
    await expect(page.locator("#flowSvg")).toBeVisible();
    await expect.poll(() => flowMotionRunning(page), { timeout: 10_000 }).toBeGreaterThan(0);
  });

  // A short window leaves enough scrolling room for the drawing to clear the
  // observer's margin on the preview's six devices; a 900 px one does not.
  test.describe("in a short window", () => {
    test.use({ viewport: { width: 1600, height: 600 } });

    test("scrolling the flow out of sight stops it, and back into sight starts it", async ({
      page,
    }) => {
      // The devices view is the long one: its drawing leaves the screen well
      // before the page ends, which the short overview never does.
      await page.goto("/", { waitUntil: "domcontentloaded" });
      await page.click('[data-flow-view="devices"]');
      await expect(page.locator("#deviceFlowView")).toBeVisible();
      await expect.poll(() => flowMotionRunning(page)).toBeGreaterThan(0);

      await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
      await expect.poll(() => flowMotionRunning(page), { timeout: 10_000 }).toBe(0);

      await page.evaluate(() => window.scrollTo(0, 0));
      await expect.poll(() => flowMotionRunning(page), { timeout: 10_000 }).toBeGreaterThan(0);
    });

    test("the device cards keep their live values while the flow is paused", async ({ page }) => {
      // Pausing motion must not pause the cockpit. The cards are below the
      // drawing, so they are exactly what a reader is looking at when it stops.
      await page.goto("/", { waitUntil: "domcontentloaded" });
      await page.click('[data-flow-view="devices"]');
      await expect(page.locator("#deviceGrid .device-card").first()).toBeVisible();
      await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
      await expect.poll(() => flowMotionRunning(page), { timeout: 10_000 }).toBe(0);

      // The cards are still rendered and still carry values, not placeholders.
      const values = await page.$$eval("#deviceGrid .device-card strong", (nodes) =>
        nodes.map((node) => node.textContent || "")
      );
      expect(values.length).toBeGreaterThan(0);
      expect(values.every((value) => value.trim().length > 0)).toBe(true);
    });
  });
  test("nothing on screen is ever left standing still", async ({ page }) => {
    // The one invariant that matters to a reader: pausing must never reach an
    // element they can see. Checked in every view and at both ends of a scroll,
    // because each is a separate opportunity to get it wrong.
    const pausedButVisible = () =>
      page.evaluate(() => {
        const endless = document.getAnimations().filter((animation) => {
          try {
            return animation.effect!.getComputedTiming().iterations === Infinity;
          } catch (_) {
            return false;
          }
        });
        return endless
          .filter((animation) => animation.playState === "paused")
          .map((animation) => (animation.effect as KeyframeEffect).target)
          .filter((target): target is Element => Boolean(target))
          .filter((target) => {
            const box = target.getBoundingClientRect();
            return (
              box.width > 0 &&
              box.height > 0 &&
              box.bottom > 0 &&
              box.top < window.innerHeight
            );
          }).length;
      });

    await page.goto("/", { waitUntil: "domcontentloaded" });
    await expect(page.locator("#flowSvg")).toBeVisible();
    expect(await pausedButVisible()).toBe(0);

    for (const view of ["control", "devices", "energy", "aggregated"]) {
      await page.click(`[data-flow-view="${view}"]`);
      await page.waitForTimeout(600);
      expect(await pausedButVisible(), `view ${view}`).toBe(0);
    }

    await page.click('[data-flow-view="devices"]');
    await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
    await page.waitForTimeout(600);
    expect(await pausedButVisible(), "scrolled to the bottom").toBe(0);

    await page.evaluate(() => window.scrollTo(0, 0));
    await page.waitForTimeout(600);
    expect(await pausedButVisible(), "back at the top").toBe(0);
  });
});
