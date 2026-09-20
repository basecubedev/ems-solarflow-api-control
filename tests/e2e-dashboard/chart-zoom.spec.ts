// SPDX-License-Identifier: AGPL-3.0-or-later
// Zooming a chart, and the part of it that only a browser can answer: that the
// wheel reaches the chart at all, that it cancels the scroll it would otherwise
// cause, and that one notch produces exactly one backend request rather than
// one per animation frame.
//
// The two charts differ on purpose. Analytics re-queries, because the backend
// picks a finer bucket for a narrower window; History has one profile and only
// rescales its axis. So the Analytics assertions are about the request that
// goes out, and the History ones are about the request that does not.
//
// Never `waitUntil: "networkidle"` here: /api/events is an open SSE stream, so
// the network is never idle and every navigation would sit out its timeout.
import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

// app.js is a classic script, so its top-level `const state` lives in the page's
// global lexical scope: reachable as a bare identifier inside page.evaluate,
// but never as a property of `window`. Declaring it here is what lets the specs
// read the cockpit's own state instead of re-deriving it from pixels.
type Window = { min: number; max: number };
declare const state: {
  analytics: { chart: { scales: { x: Window } } | null; zoom: Window | null };
  history: { chart: { scales: { x: Window } } | null; zoom: Window | null };
};
declare function historyShouldAutoRefresh(): boolean;

type Chart = "analytics" | "history";

async function chartWindow(page: Page, chart: Chart): Promise<Window> {
  return page.evaluate((which) => {
    const scale = state[which as Chart].chart!.scales.x;
    return { min: scale.min, max: scale.max };
  }, chart);
}

function span(window: Window) {
  return window.max - window.min;
}

// Open a preview view and wait until its chart has actually been built -- the
// canvas being in the DOM is not the same thing as uPlot owning a scale.
async function openChart(page: Page, view: string, chart: Chart) {
  await page.goto(`/preview/${view}`, { waitUntil: "domcontentloaded" });
  const containerId = chart === "analytics" ? "#analyticsChart" : "#historyChart";
  await expect(page.locator(`${containerId} canvas`).first()).toBeVisible();
  await expect
    .poll(() => page.evaluate((which) => !!state[which as Chart].chart, chart))
    .toBe(true);
}

// One notch forward with the pointer parked over the middle of the plot, and
// the page's own scroll position from just before it. The History panel sits
// below the fold, so the chart is brought on screen first -- a wheel event
// aimed past the viewport reaches nothing, which reads exactly like a chart
// that ignored it.
async function wheelOver(page: Page, containerId: string): Promise<number> {
  const target = page.locator(containerId);
  await target.scrollIntoViewIfNeeded();
  const box = await target.boundingBox();
  expect(box).not.toBeNull();
  await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
  const scrollBefore = await page.evaluate(() => window.scrollY);
  await page.mouse.wheel(0, -120);
  return scrollBefore;
}

test.describe("analytics chart zoom @smoke", () => {
  test("one wheel notch narrows the window and re-queries exactly once", async ({
    page,
  }) => {
    const zoomRequests: string[] = [];
    page.on("request", (request) => {
      const url = request.url();
      if (url.includes("/api/analytics/series") && url.includes("start=")) {
        zoomRequests.push(url);
      }
    });

    await openChart(page, "analytics", "analytics");
    const before = await chartWindow(page, "analytics");

    // The page has to be long enough for a missed preventDefault to show.
    const scrollable = await page.evaluate(
      () => document.documentElement.scrollHeight > window.innerHeight
    );
    expect(scrollable).toBe(true);

    const [, scrollBefore] = await Promise.all([
      page.waitForResponse(
        (response) =>
          response.url().includes("/api/analytics/series") &&
          response.url().includes("start=")
      ),
      wheelOver(page, "#analyticsChart"),
    ]);

    // One notch, one request, and it carries the window the chart now shows.
    expect(zoomRequests).toHaveLength(1);
    const requested = new URL(zoomRequests[0]);
    expect(requested.searchParams.get("range")).toBeNull();
    const start = Number(requested.searchParams.get("start"));
    const end = Number(requested.searchParams.get("end"));
    expect(end - start).toBeLessThan(span(before));

    const after = await chartWindow(page, "analytics");
    expect(span(after)).toBeLessThan(span(before));
    // Anchored on the pointer at the middle of the plot, so both edges moved in.
    expect(after.min).toBeGreaterThan(before.min);
    expect(after.max).toBeLessThan(before.max);

    // The wheel zoomed the chart instead of scrolling the page past it.
    expect(await page.evaluate(() => window.scrollY)).toBe(scrollBefore);

    await expect(page.locator("#analyticsBackToLive")).toBeVisible();
  });

  test("the + button zooms further and Back to live undoes all of it", async ({
    page,
  }) => {
    await openChart(page, "analytics", "analytics");
    const live = await chartWindow(page, "analytics");

    await Promise.all([
      page.waitForResponse((response) => response.url().includes("start=")),
      page.locator("#analyticsZoomIn").click(),
    ]);
    const zoomed = await chartWindow(page, "analytics");
    expect(span(zoomed)).toBeLessThan(span(live));

    await Promise.all([
      page.waitForResponse((response) => response.url().includes("start=")),
      page.locator("#analyticsZoomIn").click(),
    ]);
    const deeper = await chartWindow(page, "analytics");
    expect(span(deeper)).toBeLessThan(span(zoomed));

    await Promise.all([
      page.waitForResponse(
        (response) =>
          response.url().includes("/api/analytics/series") &&
          response.url().includes("range=")
      ),
      page.locator("#analyticsBackToLive").click(),
    ]);
    await expect(page.locator("#analyticsBackToLive")).toBeHidden();
    expect(await page.evaluate(() => state.analytics.zoom)).toBeNull();
  });
});

test.describe("history chart zoom", () => {
  test("the wheel zooms without asking the backend, and Back to live resets", async ({
    page,
  }) => {
    const historyRequests: string[] = [];
    page.on("request", (request) => {
      if (request.url().includes("/api/history/series")) {
        historyRequests.push(request.url());
      }
    });

    await openChart(page, "aggregated", "history");
    const before = await chartWindow(page, "history");
    expect(await page.evaluate(() => historyShouldAutoRefresh())).toBe(true);

    const requestsBeforeZoom = historyRequests.length;
    const scrollBefore = await wheelOver(page, "#historyChart");

    const after = await chartWindow(page, "history");
    expect(span(after)).toBeLessThan(span(before));
    expect(await page.evaluate(() => state.history.zoom)).not.toBeNull();
    // A refresh would reset the axis, so it waits while the window is read.
    expect(await page.evaluate(() => historyShouldAutoRefresh())).toBe(false);
    expect(await page.evaluate(() => window.scrollY)).toBe(scrollBefore);
    await expect(page.locator("#historyBackToLive")).toBeVisible();

    // The reset is the only thing here that fetches: exactly one request since
    // the wheel, which is the live reload it performs.
    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/history/series")),
      page.locator("#historyBackToLive").click(),
    ]);
    expect(historyRequests.length - requestsBeforeZoom).toBe(1);
    // The History URL never carries a window: there is only one profile here.
    expect(historyRequests[historyRequests.length - 1]).not.toContain("start=");

    await expect(page.locator("#historyBackToLive")).toBeHidden();
    expect(await page.evaluate(() => state.history.zoom)).toBeNull();
    expect(await page.evaluate(() => historyShouldAutoRefresh())).toBe(true);
  });
});
