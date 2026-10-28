// SPDX-License-Identifier: AGPL-3.0-or-later
// The Modern dashboard at the root, against the same preview server and the
// same API as the deprecated Classic at /classic/. Never `waitUntil: "networkidle"`: /api/events stays open.
import { expect, test } from "@playwright/test";
import { DOCS_SHOT_TIME, pinTheDay } from "./pin-the-day";

const SIGNED_IN = {
  auth_configured: true,
  authenticated: true,
  csrf_token: "preview-csrf-token",
  write_mode_available: true,
  write_mode_active: true,
};

async function clearScheduleRules(request) {
  const overview = await (await request.get("/api/schedule")).json();
  for (const rule of overview.rules || []) {
    const revision = rule.revision ? `?revision=${encodeURIComponent(rule.revision)}` : "";
    await request.delete(`/api/schedule/rules/${encodeURIComponent(rule.id)}${revision}`, {
      headers: { "X-CSRF-Token": SIGNED_IN.csrf_token },
    });
  }
}

async function settled(locator) {
  await locator.evaluate((node) => Promise.all(node.getAnimations().map((animation) => animation.finished)));
}

async function openModern(page, hash = "#/overview") {
  await page.goto(`/${hash}`, { waitUntil: "domcontentloaded" });
  await expect(page.getByTestId("ems-state")).toBeVisible();
}

test.describe("Modern dashboard @smoke", () => {
  test("demo mode, ?ui=classic and a browser too old for Modern go to Classic", async ({ page }) => {
    await page.goto("/?Demo=TRUE", { waitUntil: "domcontentloaded" });
    await expect(page).toHaveURL(/\/classic\/\?Demo=TRUE$/);
    await expect(page.locator("#themeSelect")).toBeVisible();
    await page.goto("/?ui=classic", { waitUntil: "domcontentloaded" });
    await expect(page).toHaveURL(/\/classic\/\?ui=classic$/);
    await page.goto("/?demo=0", { waitUntil: "domcontentloaded" });
    await expect(page.getByTestId("ems-state")).toBeVisible();
    await expect(page).toHaveURL(/:\d+\/\?demo=0$/);

    await page.addInitScript(() => {
      delete HTMLElement.prototype.popover;
    });
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await expect(page).toHaveURL(/\/classic\/$/);
    await expect(page.locator("#themeSelect")).toBeVisible();
  });

  test("Modern answers at the root, Classic under /classic/ says it is deprecated, and each links to the other", async ({ page }) => {
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await expect(page.getByTestId("ems-state")).toBeVisible();
    await page.getByTestId("switch-classic").click();
    await expect(page).toHaveURL(/\/classic\/$/);
    await expect(page.locator("#classicNotice")).toContainText("deprecated");
    await page.locator("#modernLink").click();
    await expect(page).toHaveURL(/:\d+\/$/);
    await expect(page.getByTestId("ems-state")).toBeVisible();
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await expect(page.getByTestId("ems-state")).toBeVisible();
  });

  test("a bookmark of the old Modern address opens the same page at the root", async ({ page }) => {
    await page.goto("/modern/#/energy", { waitUntil: "domcontentloaded" });
    await expect(page).toHaveURL(/:\d+\/#\/energy$/);
    await expect(page.getByTestId("energy-periods")).toBeVisible();
  });

  test("the overview shows the status, the energy flow and the live values", async ({ page }) => {
    await openModern(page);
    await expect(page.getByTestId("kpi-status")).toHaveAttribute("data-level", "ok");
    const flow = page.getByTestId("energy-flow");
    await expect(flow).toHaveAttribute("data-mode", "system");
    await expect(flow).toContainText("1.85 kW");
    await expect(page.getByTestId("active-rules")).toContainText("PV priority");
  });

  test("the flow switches to one row per device and keeps that choice", async ({ page }) => {
    await openModern(page);
    await page.getByRole("button", { name: "Per device" }).click();
    const flow = page.getByTestId("energy-flow");
    await expect(flow).toHaveAttribute("data-mode", "devices");
    await expect(flow).toContainText("WR1");
    await expect(flow).toContainText("WR2");
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.getByTestId("energy-flow")).toHaveAttribute("data-mode", "devices");
  });

  test("language, numbers and colour scheme change together and survive a reload", async ({ page }) => {
    await openModern(page);
    await page.getByTestId("appearance-button").click();
    const appearance = page.getByTestId("appearance");
    await appearance.getByRole("button", { name: "DE" }).click();
    await appearance.getByRole("button", { name: "Midnight" }).click();
    await expect(page.locator("html")).toHaveAttribute("lang", "de");
    await expect(page.locator("html")).toHaveAttribute("data-m-theme", "midnight");
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Übersicht");
    await expect(page.getByTestId("energy-flow")).toContainText("1,85 kW");
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Übersicht");
    await expect(page.locator("html")).toHaveAttribute("data-m-theme", "midnight");
  });

  test("devices, energy and analytics render from the same API", async ({ page }) => {
    await openModern(page, "#/devices");
    await expect(page.getByTestId("device-card")).toHaveCount(2);
    await expect(page.getByTestId("grid-meter")).toBeVisible();
    await expect(page.locator(".device-grid > *").first()).toHaveAttribute("data-testid", "grid-meter");

    await page.goto("/#/energy", { waitUntil: "domcontentloaded" });
    await expect(page.getByTestId("energy-periods")).toContainText(/Last 7 days\s*18\.4\d?\skWh/);
    await page.getByRole("button", { name: "Table" }).click();
    await expect(page.locator("table.data tbody tr")).toHaveCount(12);
    await expect(page.locator("table.data")).toContainText(/\d\skWh/);

    await page.goto("/#/analytics", { waitUntil: "domcontentloaded" });
    await expect(page.locator(".kpi").filter({ hasText: "Current state of charge" })).toContainText("60%");
  });

  test("a month opens with its figures, and the channels beyond solar and home use in Expert", async ({ page }) => {
    await pinTheDay(page, DOCS_SHOT_TIME);
    await openModern(page, "#/energy");
    await page.getByRole("button", { name: /^Sept: / }).click();
    const detail = page.getByTestId("month-detail");
    await expect(detail).toContainText(/Solar\s*48\.6\skWh/);
    await expect(detail).toContainText(/Home use\s*70\.2\skWh/);
    await expect(detail).not.toContainText("Grid import");
    await page.getByRole("button", { name: "Expert" }).click();
    await expect(detail).toContainText("September");
    await expect(detail).toContainText("66.9 kWh");
    await expect(detail).toContainText(/Grid import\s*28\.9\skWh/);
    await expect(detail).toContainText("Energy flows since");
    await expect(page.locator("dl.context")).toContainText("Energy flows since");
  });

  test("the month table carries the same figures as the month detail, the channels beyond solar and home use in Expert", async ({ page }) => {
    await pinTheDay(page, DOCS_SHOT_TIME);
    await openModern(page, "#/energy");
    await page.getByRole("button", { name: "Table" }).click();
    const table = page.locator("table.data");
    await expect(table.locator("thead")).not.toContainText("Grid import");
    await expect(table.locator("caption")).toHaveCount(0);
    await page.getByRole("button", { name: "Expert" }).click();
    await expect(table.locator("thead")).toContainText("Grid import");
    await expect(table.locator("thead th").filter({ hasText: /^Solar$/ })).toHaveCount(1);
    await expect(table.locator("thead")).toContainText("Battery charged");
    const september = table.locator("tbody tr", { hasText: "Sept" });
    await expect(september).toContainText("28.9 kWh");
    await expect(table.locator("caption")).toContainText("Energy flows since");
  });

  test("the rules say why they are on, and a chart zooms and resets", async ({ page }) => {
    await openModern(page);
    await expect(page.getByTestId("active-rules")).toContainText("preview control loop active");
    await page.goto("/#/analytics", { waitUntil: "domcontentloaded" });
    const plot = page.locator(".chart .u-over").first();
    await expect(plot).toBeVisible();
    const box = await plot.boundingBox();
    await page.mouse.move(box.x + box.width * 0.3, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.6, box.y + box.height / 2, { steps: 6 });
    await page.mouse.up();
    await page.getByTestId("chart-zoom-reset").click();
    await expect(page.getByTestId("chart-zoom-reset")).toHaveCount(0);

    await page.mouse.move(box.x + box.width * 0.3, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.6, box.y + box.height / 2, { steps: 6 });
    await page.mouse.up();
    await expect(page.getByTestId("chart-zoom-reset")).toBeVisible();
    await page.getByRole("button", { name: "7 d", exact: true }).click();
    await expect(page.getByTestId("chart-zoom-reset")).toHaveCount(0);
  });

  test("a zoom on Analytics reads the window from the server, the wheel and buttons zoom, and Escape goes back", async ({ page }) => {
    const requests: string[] = [];
    page.on("request", (request) => {
      if (request.url().includes("/api/analytics/series")) requests.push(request.url());
    });
    await page.goto("/#/analytics", { waitUntil: "domcontentloaded" });
    const plot = page.locator(".chart .u-over").first();
    await expect(plot).toBeVisible();
    await expect.poll(() => requests.length).toBeGreaterThan(0);
    const before = requests.length;
    await page.getByTestId("chart-zoom-in").first().click();
    await expect(page.getByTestId("chart-zoom-reset").first()).toBeVisible();
    await expect.poll(() => requests.slice(before).some((url) => url.includes("start=") && url.includes("end="))).toBe(true);
    await expect.poll(() => requests.length).toBeGreaterThan(before);
    const zoomed = requests.at(-1);
    await expect(page.locator(".kpi-label").first()).not.toContainText("24 h");
    const box = await plot.boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.wheel(0, -200);
    await expect.poll(() => requests.at(-1)).not.toBe(zoomed);
    expect(requests.at(-1)).toContain("start=");
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("chart-zoom-reset")).toHaveCount(0);
    await expect.poll(() => requests.at(-1)).toContain("range=");
  });

  test("analytics that are not set up say so", async ({ page }) => {
    await page.route("**/api/analytics/status", (route) => route.fulfill({ json: { available: false, reason: "not_configured" } }));
    await page.goto("/#/analytics", { waitUntil: "domcontentloaded" });
    await expect(page.getByTestId("analytics-unavailable")).toContainText("InfluxDB analytics is not configured");
  });

  test("an analytics schema that is incomplete says so with the server's fix", async ({ page }) => {
    await page.route("**/api/analytics/series**", (route) => route.fulfill({ json: { available: false, reason: "schema_incomplete", hint: "Run:\nems-admin repair analytics" } }));
    await page.goto("/#/analytics", { waitUntil: "domcontentloaded" });
    const info = page.getByTestId("analytics-unavailable");
    await expect(info).toContainText("The InfluxDB analytics schema is incomplete");
    await expect(info).toContainText("ems-admin repair analytics");
    await expect(page.getByRole("button", { name: "7 d", exact: true })).toBeVisible();
  });

  test("the view and its overlays form one block, the time range the next, the chart right under it, the tiles below", async ({ page }) => {
    await page.goto("/#/analytics", { waitUntil: "domcontentloaded" });
    const chart = page.locator(".chart .u-over").first();
    await expect(chart).toBeVisible();
    const viewBlock = page.getByTestId("analytics-view-block");
    const rangeBlock = page.getByTestId("analytics-range-block");
    await expect(viewBlock.getByRole("button", { name: "Overview", exact: true })).toBeVisible();
    await expect(viewBlock.getByRole("button", { name: "State of charge", exact: true })).toBeVisible();
    await expect(rangeBlock.getByRole("button", { name: "24 h", exact: true })).toBeVisible();
    await expect(rangeBlock.getByRole("button", { name: "Apply" })).toBeVisible();
    const view = (await viewBlock.boundingBox())!;
    const range = (await rangeBlock.boundingBox())!;
    const chartCard = (await page.locator(".card", { has: chart }).first().boundingBox())!;
    expect(range.y).toBeGreaterThan(view.y + view.height - 1);
    expect(chartCard.y).toBeGreaterThan(range.y + range.height - 1);
    const tileBox = (await page.locator(".kpi").first().boundingBox())!;
    expect(tileBox.y).toBeGreaterThan(chartCard.y + chartCard.height - 1);
  });

  test("the state of charge joins the main analytics chart on its own percent scale on the right", async ({ page }) => {
    const requests: string[] = [];
    page.on("request", (request) => {
      if (request.url().includes("/api/analytics/series")) requests.push(request.url());
    });
    const asksForSoc = (url: string) => (new URL(url).searchParams.get("series") || "").split(",").includes("soc");
    await page.goto("/#/analytics", { waitUntil: "domcontentloaded" });
    const chart = page.locator(".chart").first();
    await expect(chart.locator(".u-over")).toBeVisible();
    const rightGap = () => chart.evaluate((host) => host.getBoundingClientRect().right - host.querySelector(".u-over")!.getBoundingClientRect().right);
    const before = await rightGap();
    await page.getByRole("button", { name: "State of charge", exact: true }).click();
    await expect(page.locator(".legend-item").filter({ hasText: "State of charge" })).toContainText("%");
    await expect(page.locator(".chart .u-over")).toHaveCount(1);
    await expect.poll(rightGap).toBeGreaterThan(before + 20);
    await page.getByTestId("chart-zoom-in").click();
    await expect.poll(() => requests.some((url) => asksForSoc(url) && url.includes("start="))).toBe(true);
  });

  test("samples less than a minute apart are named with their seconds", async ({ page }) => {
    let step = 10;
    await page.route("**/api/analytics/series**", (route) => {
      const start = Date.UTC(2026, 9, 8, 10, 0, 0) / 1000;
      const time = Array.from({ length: 18 }, (_, i) => start + i * step);
      const values = time.map((_, i) => 100 + i);
      return route.fulfill({ json: { available: true, time, series: { pv: values, output: values, battery: values } } });
    });
    await page.goto("/#/analytics", { waitUntil: "domcontentloaded" });
    await expect(page.locator(".legend-time")).toHaveText(/\d:\d\d:\d\d/);
    step = 300;
    await page.getByRole("button", { name: "6 h", exact: true }).click();
    await expect(page.locator(".legend-time")).not.toHaveText(/\d:\d\d:\d\d/);
    await expect(page.locator(".legend-time")).toHaveText(/\d:\d\d/);
  });

  test("Escape that closes the appearance menu or the sign-in keeps the zoom", async ({ page }) => {
    await page.goto("/#/analytics", { waitUntil: "domcontentloaded" });
    await expect(page.locator(".chart .u-over").first()).toBeVisible();
    await page.getByTestId("chart-zoom-in").first().click();
    await expect(page.getByTestId("chart-zoom-reset")).toHaveCount(1);
    await page.getByTestId("appearance-button").first().click();
    await expect(page.getByTestId("appearance")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("appearance")).toBeHidden();
    await expect(page.getByTestId("chart-zoom-reset")).toHaveCount(1);
    await page.getByTestId("sign-in").first().click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toBeHidden();
    await expect(page.getByTestId("chart-zoom-reset")).toHaveCount(1);
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("chart-zoom-reset")).toHaveCount(0);
  });

  test("an unavailable range hides the chart with the state of charge on", async ({ page }) => {
    await page.route("**/api/analytics/series**", (route) => (route.request().url().includes("range=7d") ? route.fulfill({ json: { available: false, reason: "unreachable" } }) : route.continue()));
    await page.goto("/#/analytics", { waitUntil: "domcontentloaded" });
    await page.getByRole("button", { name: "State of charge", exact: true }).click();
    await expect(page.locator(".legend-item").filter({ hasText: "State of charge" })).toBeVisible();
    await page.getByRole("button", { name: "7 d", exact: true }).click();
    await expect(page.getByTestId("analytics-unavailable")).toBeVisible();
    await expect(page.locator(".chart .u-over")).toHaveCount(0);
    await expect(page.getByText("No history in this range yet.")).toHaveCount(0);
  });

  test("a zoom whose read fails says so and has its way back on the page", async ({ page }) => {
    let fail = false;
    await page.route("**/api/analytics/series**", (route) => {
      if (fail && route.request().url().includes("start=")) return route.fulfill({ status: 500, json: { error: "boom" } });
      return route.continue();
    });
    await page.goto("/#/analytics", { waitUntil: "domcontentloaded" });
    await expect(page.locator(".chart .u-over").first()).toBeVisible();
    fail = true;
    await page.getByTestId("chart-zoom-in").first().click();
    await expect(page.getByText("Could not load this data.")).toBeVisible();
    await page.getByTestId("analytics-zoom-reset").click();
    await expect(page.locator(".chart .u-over").first()).toBeVisible();
    await expect(page.getByTestId("chart-zoom-reset")).toHaveCount(0);
  });

  test("a click on the range or view already shown leaves a zoom, as in Classic", async ({ page }) => {
    const requests: string[] = [];
    page.on("request", (request) => {
      if (request.url().includes("/api/analytics/series")) requests.push(request.url());
    });
    await page.goto("/#/analytics", { waitUntil: "domcontentloaded" });
    await expect(page.locator(".chart .u-over").first()).toBeVisible();
    await page.getByTestId("chart-zoom-in").first().click();
    await expect(page.getByTestId("chart-zoom-reset")).toHaveCount(1);
    await expect.poll(() => requests.at(-1)).toContain("start=");
    await page.getByRole("button", { name: "24 h", exact: true }).click();
    await expect(page.getByTestId("chart-zoom-reset")).toHaveCount(0);
    await expect.poll(() => requests.at(-1)).toContain("range=24h");
    await expect(page.locator(".kpi-label").first()).toContainText("24 h");
    await page.getByTestId("chart-zoom-in").first().click();
    await expect(page.getByTestId("chart-zoom-reset")).toHaveCount(1);
    await page.getByRole("button", { name: "Overview", exact: true }).first().click();
    await expect(page.getByTestId("chart-zoom-reset")).toHaveCount(0);
  });

  test("a click on the history range already shown leaves the history chart's zoom", async ({ page }) => {
    await openModern(page, "#/devices");
    const card = page.locator(".card", { has: page.getByTestId("history-source") }).first();
    await card.getByTestId("chart-zoom-in").click();
    await expect(card.getByTestId("chart-zoom-reset")).toBeVisible();
    await card.getByRole("button", { name: "24 h", exact: true }).click();
    await expect(card.getByTestId("chart-zoom-reset")).toHaveCount(0);
  });

  test("a custom analytics range names the KPIs' period by its length", async ({ page }) => {
    await page.goto("/#/analytics", { waitUntil: "domcontentloaded" });
    await expect(page.locator(".chart .u-over").first()).toBeVisible();
    await page.getByRole("textbox", { name: "From", exact: true }).fill("2026-10-19T00:00");
    await page.getByRole("textbox", { name: "To", exact: true }).fill("2026-10-22T12:00");
    await page.getByRole("button", { name: "Apply", exact: true }).click();
    await expect(page.locator(".kpi-label").first()).toContainText("3.5 d");
  });

  test("a range whose read fails says so instead of keeping the last range under the new label", async ({ page }) => {
    await page.route("**/api/analytics/series**", (route) => (route.request().url().includes("range=7d") ? route.fulfill({ status: 500, json: { error: "boom" } }) : route.continue()));
    await page.goto("/#/analytics", { waitUntil: "domcontentloaded" });
    await expect(page.locator(".chart .u-over").first()).toBeVisible();
    await page.getByRole("button", { name: "7 d", exact: true }).click();
    await expect(page.getByText("Could not load this data.")).toBeVisible();
    await expect(page.locator(".kpi-value").first()).toHaveText("—");
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => (release = resolve));
    await page.route("**/api/analytics/series**", async (route) => {
      if (!route.request().url().includes("range=6h")) return route.fallback();
      await gate;
      await route.fallback().catch(() => undefined);
    });
    await page.getByRole("button", { name: "6 h", exact: true }).click();
    await expect(page.getByText("Could not load this data.")).toHaveCount(0);
    release();
    await expect(page.locator(".chart .u-over").first()).toBeVisible();
  });

  test("a refresh that fails keeps the last answer and says so", async ({ page }) => {
    await page.clock.install();
    let fail = false;
    await page.route("**/api/analytics/series**", (route) => (fail ? route.fulfill({ status: 500, json: { error: "boom" } }) : route.continue()));
    await page.goto("/#/analytics", { waitUntil: "domcontentloaded" });
    await expect(page.locator(".chart .u-over").first()).toBeVisible();
    fail = true;
    await page.clock.fastForward(31_000);
    await expect(page.getByText("Could not refresh; this is the last answer.")).toBeVisible();
    await expect(page.locator(".chart .u-over").first()).toBeVisible();
  });

  test("a history range whose read fails says so on the Devices page", async ({ page }) => {
    await page.route("**/api/history/series**", (route) => (route.request().url().includes("range=7d") ? route.fulfill({ status: 503, json: { error: "history_unavailable" } }) : route.continue()));
    await openModern(page, "#/devices");
    const card = page.locator(".card", { has: page.getByTestId("history-source") }).first();
    await expect(card.locator(".chart .u-over")).toBeVisible();
    await card.getByRole("button", { name: "7 d", exact: true }).click();
    await expect(page.getByText("Could not load this data.")).toBeVisible();
  });

  test("a click on the range that failed reads it again, and going back to the last one shows no old error", async ({ page }) => {
    let fail = true;
    const asked: string[] = [];
    await page.route("**/api/analytics/series**", (route) => {
      const url = route.request().url();
      if (url.includes("range=7d")) {
        asked.push(url);
        if (fail) return route.fulfill({ status: 500, json: { error: "boom" } });
      }
      return route.continue();
    });
    await page.goto("/#/analytics", { waitUntil: "domcontentloaded" });
    await expect(page.locator(".chart .u-over").first()).toBeVisible();
    await page.getByRole("button", { name: "7 d", exact: true }).click();
    await expect(page.getByText("Could not load this data.")).toBeVisible();
    fail = false;
    const before = asked.length;
    await page.getByRole("button", { name: "7 d", exact: true }).click();
    await expect.poll(() => asked.length).toBeGreaterThan(before);
    await expect(page.getByText("Could not load this data.")).toHaveCount(0);
    fail = true;
    await page.getByRole("button", { name: "24 h", exact: true }).click();
    await page.getByRole("button", { name: "7 d", exact: true }).click();
    await expect(page.getByText("Could not load this data.")).toBeVisible();
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => (release = resolve));
    await page.route("**/api/analytics/series**", async (route) => {
      if (!route.request().url().includes("range=24h")) return route.fallback();
      await gate;
      await route.fallback().catch(() => undefined);
    });
    await page.getByRole("button", { name: "24 h", exact: true }).click();
    await expect(page.getByText("Could not load this data.")).toHaveCount(0);
    release();
    await expect(page.locator(".chart .u-over").first()).toBeVisible();
  });

  test("a reset while a zoom is still read drops that read", async ({ page }) => {
    const aborted: string[] = [];
    page.on("requestfailed", (request) => {
      if (request.url().includes("/api/analytics/series") && request.url().includes("start=")) aborted.push(request.url());
    });
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => (release = resolve));
    let held = 0;
    await page.route("**/api/analytics/series**", async (route) => {
      if (!route.request().url().includes("start=")) return route.continue();
      held += 1;
      await gate;
      await route.continue().catch(() => undefined);
    });
    await page.goto("/#/analytics", { waitUntil: "domcontentloaded" });
    await expect(page.locator(".chart .u-over").first()).toBeVisible();
    await page.getByTestId("chart-zoom-in").first().click();
    await expect.poll(() => held).toBe(1);
    await page.getByTestId("chart-zoom-reset").first().click();
    await expect.poll(() => aborted.length).toBe(1);
    release();
    await expect(page.locator(".kpi-label").first()).toContainText("24 h");
  });

  test("a zoom the analytics cannot answer has its way back on the page, by button or Escape", async ({ page }) => {
    let refuse = false;
    await page.route("**/api/analytics/series**", (route) => {
      if (refuse && route.request().url().includes("start=")) return route.fulfill({ json: { available: false, reason: "schema_incomplete", hint: "missing bucket" } });
      return route.continue();
    });
    await page.goto("/#/analytics", { waitUntil: "domcontentloaded" });
    await expect(page.locator(".chart .u-over").first()).toBeVisible();
    refuse = true;
    await page.getByTestId("chart-zoom-in").first().click();
    await expect(page.getByTestId("analytics-unavailable")).toContainText("missing bucket");
    await page.keyboard.press("Escape");
    await expect(page.locator(".chart .u-over").first()).toBeVisible();
    await page.getByTestId("chart-zoom-in").first().click();
    await expect(page.getByTestId("analytics-unavailable")).toBeVisible();
    await page.getByTestId("analytics-zoom-reset").click();
    await expect(page.getByTestId("analytics-unavailable")).toHaveCount(0);
    await expect(page.getByTestId("chart-zoom-reset")).toHaveCount(0);
  });

  test("a range the analytics cannot answer keeps the controls, and another range shows its chart again", async ({ page }) => {
    await page.route("**/api/analytics/series**", async (route) => {
      if (route.request().url().includes("range=7d")) return route.fulfill({ json: { available: false, reason: "unreachable", hint: "InfluxDB did not answer." } });
      return route.continue();
    });
    await page.goto("/#/analytics", { waitUntil: "domcontentloaded" });
    await expect(page.locator(".chart .u-over").first()).toBeVisible();
    await page.getByRole("button", { name: "7 d", exact: true }).click();
    await expect(page.getByTestId("analytics-unavailable")).toContainText("InfluxDB analytics is not reachable");
    await page.getByRole("button", { name: "24 h", exact: true }).click();
    await expect(page.getByTestId("analytics-unavailable")).toHaveCount(0);
    await expect(page.locator(".chart .u-over").first()).toBeVisible();
  });

  test("the history names its source and says when it is unavailable", async ({ page }) => {
    await openModern(page);
    await expect(page.getByTestId("history-source").first()).toBeVisible();
    await page.route("**/api/history/series**", (route) => route.fulfill({ json: { time: [], series: {}, meta: { unavailable: true } } }));
    await page.getByRole("button", { name: "6 h", exact: true }).click();
    await expect(page.getByText("The history is not available right now.")).toBeVisible();
  });

  test("every power chart draws the battery charging below zero and reads it back as Charge or Discharge, as Classic", async ({ page }) => {
    const pinned = new Date();
    pinned.setHours(18, 30, 0, 0);
    await page.clock.install({ time: pinned });
    const answer = (source: string) => {
      const now = Math.floor(pinned.getTime() / 1000);
      const flat = [0, 0, 0];
      return { available: true, source, time: [now - 180, now - 120, now - 60], series: { pv: flat, output: flat, home: flat, grid: flat, battery: [200, 0, -450] } };
    };
    await page.route("**/api/history/series**", (route) => route.fulfill({ json: answer("sqlite") }));
    await page.route("**/api/analytics/series**", (route) => route.fulfill({ json: answer("influxdb") }));
    for (const hash of ["#/overview", "#/devices", "#/analytics"]) {
      await openModern(page, hash);
      const card = page.locator(".card", { has: page.locator(".legend") }).first();
      await expect(card.locator(".legend-item", { hasText: "Battery" })).toContainText(/Discharge\s450\sW/);
      await card.getByRole("button", { name: "Table" }).click();
      const rows = card.locator("table.data tbody tr");
      await expect(rows).toHaveCount(3);
      const battery = await card.locator("table.data thead th").allTextContents().then((heads) => heads.indexOf("Battery"));
      await expect(rows.nth(0).locator("td").nth(battery)).toHaveText(/^Charge\s200\sW$/);
      await expect(rows.nth(1).locator("td").nth(battery)).toHaveText(/^0\sW$/);
      await expect(rows.nth(2).locator("td").nth(battery)).toHaveText(/^Discharge\s450\sW$/);
    }
  });

  test("the today chart zooms within today: one step in and one out is the whole day again", async ({ page }) => {
    await openModern(page);
    const card = page.locator(".card", { has: page.getByTestId("history-source") }).first();
    await card.getByTestId("chart-zoom-in").click();
    await expect(card.getByTestId("chart-zoom-reset")).toBeVisible();
    await card.getByTestId("chart-zoom-out").focus();
    await page.keyboard.press("Enter");
    await expect(card.getByTestId("chart-zoom-reset")).toHaveCount(0);
    await expect(card.getByTestId("chart-zoom-out")).toBeFocused();
  });

  test("a history that cannot be read says so on the Overview instead of loading forever", async ({ page }) => {
    await page.route("**/api/history/series**", (route) => route.fulfill({ status: 500, json: { error: "boom" } }));
    await openModern(page);
    await expect(page.getByText("Could not load", { exact: false }).first()).toBeVisible();
    await expect(page.getByText("Loading the history…")).toHaveCount(0);
  });

  test("a runtime state that failed before sign-in is asked again at sign-in", async ({ page }) => {
    let signedIn = false;
    let runtimeAsks = 0;
    await page.route("**/api/auth/status", (route) => route.fulfill({ json: signedIn ? SIGNED_IN : { ...SIGNED_IN, authenticated: false, csrf_token: null } }));
    await page.route("**/api/runtime", (route) => {
      runtimeAsks += 1;
      return route.fulfill({ status: 503, json: { error: "unavailable" } });
    });
    await openModern(page);
    await expect.poll(() => runtimeAsks).toBeGreaterThan(0);
    await expect(page.getByText("Read-only").first()).toBeVisible();
    const before = runtimeAsks;
    signedIn = true;
    await page.route("**/api/auth/login", (route) => route.fulfill({ json: SIGNED_IN }));
    await page.getByTestId("sign-in").first().click();
    await page.getByLabel("Password").fill("pw");
    await page.getByRole("dialog").getByRole("button", { name: "Sign in" }).click();
    await expect.poll(() => runtimeAsks, { timeout: 4_000 }).toBeGreaterThan(before);
    await expect(page.getByText("Write mode · runtime state unavailable, retrying").first()).toBeVisible();
  });

  test("a runtime state that does not answer is named beside the write mode and asked again", async ({ page }) => {
    await page.route("**/api/auth/status", (route) => route.fulfill({ json: SIGNED_IN }));
    let failing = true;
    await page.route("**/api/runtime", (route) => (failing ? route.fulfill({ status: 503, json: { error: "unavailable" } }) : route.continue()));
    await openModern(page);
    await expect(page.getByText("Write mode · runtime state unavailable, retrying").first()).toBeVisible();
    failing = false;
    await expect(page.getByText("Write mode · runtime state unavailable, retrying")).toHaveCount(0, { timeout: 10_000 });
  });

  test("an overdue full-charge assist names how far overdue it is and when it is due next", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/live", async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      const first = Object.values(body.devices)[0] as Record<string, unknown>;
      first.battery_full_charge_assist = { enabled: true, has_battery: true, status: "overdue", days_until_due: -3, next_due_at: "2026-10-30T08:00:00Z", message: "Full charge is overdue" };
      await route.fulfill({ response, json: body });
    });
    await openModern(page, "#/devices");
    const facts = page.getByTestId("assist-facts").first();
    await expect(facts).toContainText("Overdue by");
    await expect(facts).toContainText("3 d");
    await expect(page.getByTestId("device-card").first()).toContainText("EMS starts an assist charge as soon as conditions allow.");
    await page.unrouteAll({ behavior: "ignoreErrors" });
  });

  test("a today tile names the top of its scale and its hours, and puts the comparison beside the figure", async ({ page }) => {
    await page.setViewportSize({ width: 900, height: 900 });
    await pinTheDay(page);
    await openModern(page);
    const tile = page.getByTestId("today-tiles").locator("section").first();
    await expect(tile.getByTestId("today-scale")).toHaveText(/^\d[\d.,]*\sk?Wh$/);
    await expect(tile.locator(".hours span")).toHaveText(["00", "06", "12", "18", "24"]);
    await expect(tile.locator(".delta span")).toContainText("vs. yesterday by 18:30");
    const [value, delta, note] = await Promise.all([".value", ".delta b", ".delta span"].map((selector) => tile.locator(selector).boundingBox()));
    expect(delta.x).toBeGreaterThan(value.x + value.width);
    expect(note.x).toBeGreaterThan(value.x + value.width);
    expect(note.y + note.height).toBeLessThanOrEqual(value.y + value.height + 4);
  });

  test("with the side menu open the today tiles keep their bars level in every language", async ({ page }) => {
    await pinTheDay(page);
    for (const width of [1024, 1280]) {
      await page.setViewportSize({ width, height: 900 });
      for (const lang of ["en", "de", "nl", "fr"]) {
        await page.goto("about:blank");
        await page.addInitScript((value) => window.localStorage.setItem("ems-modern.lang", value), lang);
        await openModern(page);
        const tiles = page.getByTestId("today-tiles").locator("section");
        await expect(tiles).toHaveCount(4);
        const boxes = await tiles.evaluateAll((nodes) =>
          nodes.map((node) => {
            const tile = node.getBoundingClientRect();
            return { left: Math.round(tile.left), top: Math.round(tile.top), plot: Math.round(node.querySelector("svg.spark").getBoundingClientRect().top) };
          }),
        );
        expect(new Set(boxes.map((box) => box.left)).size, `${width} px, ${lang}: columns`).toBe(2);
        for (const row of new Set(boxes.map((box) => box.top))) {
          const plots = new Set(boxes.filter((box) => box.top === row).map((box) => box.plot));
          expect(plots.size, `${width} px, ${lang}: bars level in the row at ${row}`).toBe(1);
        }
      }
    }
  });

  test("on a narrow tile the comparison wraps below the figure rather than over it", async ({ page }) => {
    await pinTheDay(page);
    for (const width of [360, 390]) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto("about:blank");
      await openModern(page);
      for (const tile of await page.getByTestId("today-tiles").locator("section").all()) {
        await expect(tile.locator(".delta b")).toBeVisible();
        const [value, delta] = await Promise.all([".value", ".delta b"].map((selector) => tile.locator(selector).boundingBox()));
        expect(delta.x >= value.x + value.width - 1 || delta.y >= value.y + value.height - 1, `${width} px`).toBe(true);
      }
    }
  });

  test("an inverter charging from the grid runs its line to the home backwards in the grid's colour", async ({ page }) => {
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/live", async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      for (const device of Object.values(body.devices)) Object.assign(device, { output_w: 0, ac_charge_w: 500 });
      await route.fulfill({ response, json: { ...body, inverter_output_w: 0, inverter_charge_w: 1000 } });
    });
    await openModern(page);
    const flow = page.getByTestId("energy-flow");
    await expect(flow.locator('[data-wire="out"]')).toHaveAttribute("stroke", "var(--grid)", { timeout: 10_000 });
    const firstFrame = () => flow.locator('[data-run="out"] .dashes').first().evaluate((node) => node.getAnimations()[0]?.effect.getKeyframes()[0].transform);
    await expect.poll(firstFrame).toBe("translateX(0px)");
    await page.unrouteAll({ behavior: "ignoreErrors" });
  });

  test("a value that stops arriving turns the status stale instead of staying normal", async ({ page }) => {
    await page.clock.install();
    let feeding = true;
    await page.route("**/api/events", (route) => route.abort());
    await page.route("**/api/live", (route) => (feeding ? route.continue() : route.abort()));
    await openModern(page);
    await expect(page.getByTestId("kpi-status")).toHaveAttribute("data-level", "ok");
    feeding = false;
    await page.clock.fastForward(45_000);
    await expect(page.getByTestId("kpi-status")).toHaveAttribute("data-level", "stale");
    await page.clock.fastForward(10_000);
    await expect(page.getByTestId("kpi-status")).toHaveAttribute("data-level", "stale");
    await page.unrouteAll({ behavior: "ignoreErrors" });
  });

  test("operator pages ask for a password before they show anything", async ({ page }) => {
    await openModern(page, "#/logs");
    await expect(page.locator("#content").getByText("No dashboard password is set.", { exact: false })).toBeVisible();
    await expect(page.getByTestId("logs-output")).toHaveCount(0);
  });

  test("the skip link moves focus to the page it is on, not to the overview", async ({ page }) => {
    await openModern(page, "#/devices");
    await page.keyboard.press("Tab");
    await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/#\/devices$/);
    await expect(page.locator("#content")).toBeFocused();
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Devices");
  });

  test("on a desktop the side menu shows its text from the start", async ({ page }) => {
    for (const width of [1024, 1280]) {
      await page.setViewportSize({ width, height: 800 });
      await page.goto("about:blank");
      await openModern(page);
      await expect(page.locator(".app")).not.toHaveClass(/\brail\b/);
      await expect(page.locator(".sidebar").getByRole("link", { name: "Overview" }).locator("span")).toBeVisible();
    }
  });

  test("the mark stays when the side menu folds, with EMS beneath it, and heads the phone's top bar", async ({ page }) => {
    await page.setViewportSize({ width: 1000, height: 800 });
    await openModern(page);
    const brand = page.locator(".sidebar .brand");
    await expect(brand.locator(".brand-mark")).toBeVisible();
    await expect(brand.locator(".brand-short")).toHaveText("EMS");
    await page.setViewportSize({ width: 1280, height: 800 });
    await expect(brand.locator(".brand-short")).toBeHidden();
    await expect(brand.locator(".brand-text")).toBeVisible();
    await expect(brand.locator(".brand-name")).toHaveText("EMS SolarFlow");
    await page.setViewportSize({ width: 390, height: 844 });
    const mark = page.locator(".topbar-mark");
    await expect(mark.locator(".brand-mark")).toBeVisible();
    await expect(mark).toHaveAttribute("href", "#/overview");
  });

  test("the side menu shows its text on request and keeps that choice", async ({ page }) => {
    await page.setViewportSize({ width: 1000, height: 800 });
    await openModern(page);
    const overview = page.locator(".sidebar").getByRole("link", { name: "Overview" });
    await expect(overview.locator("span")).toBeHidden();
    await page.getByTestId("nav-labels").click();
    await expect(overview.locator("span")).toBeVisible();
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.locator(".sidebar").getByRole("link", { name: "Overview" }).locator("span")).toBeVisible();

    await page.getByTestId("appearance-button").click();
    await page.getByTestId("appearance").getByRole("button", { name: "Icons" }).click();
    await expect(page.locator(".sidebar").getByRole("link", { name: "Overview" }).locator("span")).toBeHidden();
  });
});

test.describe("Modern dashboard on a phone @smoke", () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
  });

  test("the sidebar gives way to a bottom bar", async ({ page }) => {
    await openModern(page);
    await expect(page.locator(".sidebar")).toBeHidden();
    await expect(page.locator(".app")).toHaveClass(/\brail\b/);
    expect((await page.locator(".main").boundingBox()).width).toBe(390);
    const bottom = page.locator(".bottom-nav");
    await expect(bottom).toBeVisible();
    await bottom.getByRole("link", { name: "Devices" }).click();
    await expect(page.getByTestId("device-card").first()).toBeVisible();
  });

  test("the system flow on a phone runs the inverter down into the home and the grid into the home beside it", async ({ page }) => {
    await openModern(page);
    const flow = page.getByTestId("energy-flow");
    await expect(flow).toHaveAttribute("data-mode", "system");
    const box = async (selector) => flow.locator(selector).boundingBox();
    await expect(async () => {
      const [hub, bat, home, grid, out, line] = await Promise.all(
        ["[data-node=hub]", "[data-node=bat]", "[data-node=home]", "[data-node=grid]", '[data-wire="out"]', '[data-wire="grid"]'].map(box),
      );
      expect(home.y).toBeGreaterThan(bat.y + bat.height);
      expect(Math.abs(grid.y - home.y)).toBeLessThan(1);
      expect(grid.x + grid.width).toBeLessThan(home.x);
      expect(Math.abs(out.x + out.width / 2 - (home.x + home.width / 2))).toBeLessThan(1);
      expect(out.y).toBeGreaterThan(hub.y + hub.height / 2);
      expect(Math.abs(out.y + out.height - home.y)).toBeLessThan(8);
      expect(Math.abs(line.y + line.height / 2 - (home.y + home.height / 2))).toBeLessThan(1);
      expect(Math.abs(line.x - (grid.x + grid.width))).toBeLessThan(8);
      expect(Math.abs(line.x + line.width - home.x)).toBeLessThan(8);
    }).toPass();
  });

  test("the flow per device stacks the inverters above the home, with room for every line", async ({ page }) => {
    await page.addInitScript(() => window.localStorage.setItem("ems-modern.flow", "devices"));
    await openModern(page);
    const flow = page.getByTestId("energy-flow");
    await expect(flow).toHaveAttribute("data-mode", "devices");
    await expect(flow.locator('[data-wire="bus-above1"]')).toBeAttached();
    const box = async (selector) => flow.locator(selector).boundingBox();
    const [hub0, hub1, home, grid, pv0] = await Promise.all(["[data-node=hub0]", "[data-node=hub1]", "[data-node=home]", "[data-node=grid]", "[data-node=pv0]"].map(box));
    expect(home.y).toBeGreaterThan(hub1.y + hub1.height);
    expect(Math.abs(grid.y - home.y)).toBeLessThan(1);
    expect(grid.x + grid.width).toBeLessThan(home.x);
    expect(hub0.x - (pv0.x + pv0.width)).toBeGreaterThanOrEqual(24);
    await expect(flow.locator('[data-wire="trunk"]')).toHaveCount(0);
    const bus = await flow.locator('[data-wire="bus-above1"]').boundingBox();
    expect(bus.x).toBeGreaterThan(hub0.x + hub0.width + 12);
    expect(bus.x).toBeGreaterThan(home.x + 24);
    expect(bus.x + bus.width).toBeLessThan(home.x + home.width - 16);
    expect(Math.abs(bus.y + bus.height - home.y)).toBeLessThan(8);
  });

  test("More opens a sheet that Esc, the scrim, the handle, a swipe and a page close", async ({ page }) => {
    await openModern(page);
    const more = page.getByTestId("nav-more");
    const sheet = page.getByRole("dialog", { name: "More" });
    await more.click();
    await expect(sheet).toBeVisible();
    await expect(sheet.getByRole("link", { name: /^Analytics/ })).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(sheet).toBeHidden();
    await expect(more).toBeFocused();

    await more.click();
    await expect(sheet).toBeVisible();
    await page.mouse.click(195, 30);
    await expect(sheet).toBeHidden();

    await more.click();
    await sheet.getByTestId("sheet-handle").click();
    await expect(sheet).toBeHidden();

    await more.click();
    await settled(sheet);
    const handle = await sheet.getByTestId("sheet-handle").boundingBox();
    await page.mouse.move(handle.x + handle.width / 2, handle.y + handle.height / 2);
    await page.mouse.down();
    await page.mouse.move(handle.x + handle.width / 2, handle.y + 40, { steps: 4 });
    await page.mouse.move(handle.x + handle.width / 2, handle.y + 140, { steps: 4 });
    await page.mouse.up();
    await expect(sheet).toBeHidden();

    await more.click();
    await sheet.getByRole("link", { name: /^Analytics/ }).click();
    await expect(sheet).toBeHidden();
    await expect(page).toHaveURL(/#\/analytics$/);
  });

  test("a short drag on the handle springs back instead of closing", async ({ page }) => {
    await openModern(page);
    await page.getByTestId("nav-more").click();
    const sheet = page.getByRole("dialog", { name: "More" });
    await settled(sheet);
    const handle = await sheet.getByTestId("sheet-handle").boundingBox();
    await page.mouse.move(handle.x + handle.width / 2, handle.y + handle.height / 2);
    await page.mouse.down();
    await page.mouse.move(handle.x + handle.width / 2, handle.y + 40, { steps: 4 });
    await page.mouse.up();
    await settled(sheet);
    await expect(sheet).toHaveJSProperty("open", true);
    await expect(sheet).not.toHaveAttribute("style", /translateY/);
    await sheet.getByTestId("sheet-handle").press("Enter");
    await expect(sheet).toBeHidden();
  });

  test("on a page under More, the fifth button names that page and opens the sheet", async ({ page }) => {
    await openModern(page, "#/logs");
    const more = page.getByTestId("nav-more");
    await expect(more).toHaveAttribute("aria-current", "page");
    await expect(more).toHaveAccessibleName("Logs — more pages");
    await expect(more).toContainText("Logs");
    await more.click();
    const sheet = page.getByRole("dialog", { name: "More" });
    const logs = sheet.getByRole("link", { name: /^Logs/ });
    await expect(logs).toHaveAttribute("aria-current", "page");
    await expect(logs).toBeFocused();

    await expect(sheet.getByRole("link", { name: /^Overview/ })).toHaveCount(0);
    await page.keyboard.press("Escape");
    await page.locator(".bottom-nav").getByRole("link", { name: "Overview" }).click();
    await expect(more).not.toHaveAttribute("aria-current", "page");
    await expect(more).toHaveAccessibleName("More");
  });

  test("signed out, operator pages carry a lock, and the sheet signs in, sets the language and the scheme", async ({ page }) => {
    await openModern(page);
    await page.getByTestId("nav-more").click();
    const sheet = page.locator("dialog.sheet");
    for (const id of ["schedule", "diagnose", "logs", "maintenance"]) {
      await expect(sheet.locator(`[data-page="${id}"] .lock`)).toBeVisible();
    }
    await expect(sheet.locator('[data-page="analytics"] .lock')).toHaveCount(0);

    await sheet.getByRole("button", { name: "Midnight" }).click();
    await expect(page.locator("html")).toHaveAttribute("data-m-theme", "midnight");
    await sheet.getByRole("button", { name: "DE" }).click();
    await expect(page.locator("html")).toHaveAttribute("lang", "de");
    await expect(sheet.getByRole("link", { name: /^Analysen/ })).toBeVisible();

    await sheet.getByTestId("sheet-sign-in").click();
    await expect(page.getByRole("dialog", { name: "Am Dashboard anmelden" })).toBeVisible();
    await expect(sheet).toBeHidden();
  });

  test("the reader picks the bar's four pages, and the choice survives a reload", async ({ page }) => {
    await openModern(page);
    const bar = page.locator(".bottom-nav");
    await page.getByTestId("nav-more").click();
    const sheet = page.getByRole("dialog", { name: "More" });
    await sheet.getByTestId("customize-bar").click();
    await sheet.getByTestId("bar-pages").getByRole("button", { name: "Control" }).click();
    await expect(sheet.getByTestId("bar-pages").getByRole("button", { name: "Control" })).toHaveAttribute("aria-pressed", "true");
    await sheet.getByTestId("more-pages").getByRole("button", { name: "Analytics" }).click();
    await expect(sheet.getByTestId("bar-pages").getByRole("button")).toHaveText(["Overview", "Devices", "Energy", "Analytics"]);
    await expect(sheet.getByTestId("bar-pages").getByRole("button", { name: "Analytics" })).toBeFocused();
    await expect(sheet.locator(".hint")).toHaveText("In the bar: Overview, Devices, Energy, Analytics");
    await expect(bar.getByRole("link")).toHaveText(["Overview", "Devices", "Energy", "Analytics"]);

    await sheet.getByTestId("bar-pages").getByRole("button", { name: "Overview" }).click();
    await sheet.getByTestId("bar-pages").getByRole("button", { name: "Energy" }).click();
    await expect(bar.getByRole("link")).toHaveText(["Energy", "Devices", "Overview", "Analytics"]);
    await sheet.getByTestId("customize-done").click();
    await expect(sheet.getByTestId("customize-bar")).toBeFocused();

    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(bar.getByRole("link")).toHaveText(["Energy", "Devices", "Overview", "Analytics"]);
    await page.getByTestId("nav-more").click();
    await expect(sheet.getByRole("link", { name: /^Control/ })).toBeVisible();
    await sheet.getByTestId("customize-bar").click();
    await sheet.getByTestId("reset-bar").click();
    await expect(bar.getByRole("link")).toHaveText(["Overview", "Devices", "Energy", "Control"]);
    await expect(sheet.getByTestId("reset-bar")).toHaveCount(0);
    await expect(sheet.getByTestId("customize-done")).toBeFocused();
  });

  test("an operator page in the bar carries the lock while signed out", async ({ page }) => {
    await page.addInitScript(() => window.localStorage.setItem("ems-modern.mobilePages", "overview,logs,ghost,logs"));
    await openModern(page);
    const bar = page.locator(".bottom-nav");
    await expect(bar.getByRole("link")).toHaveText(["Overview", "Logs", "Devices", "Energy"]);
    await expect(bar.getByRole("link", { name: "Logs Sign-in required" })).toBeVisible();
  });

  test("the bar and the sheet fit a narrow phone in every language", async ({ page }) => {
    await openModern(page, "#/maintenance");
    for (const width of [360, 390, 414]) {
      await page.setViewportSize({ width, height: 800 });
      for (const lang of ["en", "de", "nl", "fr"]) {
        await page.evaluate((value) => window.localStorage.setItem("ems-modern.lang", value), lang);
        await page.reload({ waitUntil: "domcontentloaded" });
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
        expect(overflow, `${width} px, ${lang}`).toBeLessThanOrEqual(0);
        const [mark, title] = await Promise.all([page.locator(".topbar-mark"), page.locator(".page-title")].map((node) => node.boundingBox()));
        expect(title.x, `title beside the mark at ${width} px, ${lang}`).toBeGreaterThan(mark.x + mark.width);
        expect(title.y, `title beside the mark at ${width} px, ${lang}`).toBeLessThan(mark.y + mark.height);
        await page.getByTestId("nav-more").click();
        const content = page.locator("dialog.sheet .sheet-content");
        const sheetOverflow = () => content.evaluate((node) => node.scrollWidth - node.clientWidth);
        expect(await sheetOverflow(), `sheet at ${width} px, ${lang}`).toBeLessThanOrEqual(0);
        await page.getByTestId("customize-bar").click();
        expect(await sheetOverflow(), `editing at ${width} px, ${lang}`).toBeLessThanOrEqual(0);
      }
    }
  });
});

test.describe("Modern dashboard writes @smoke", () => {
  test("a runtime change sends only what changed, with the CSRF token, after confirmation", async ({ page }) => {
    await page.route("**/api/auth/status", (route) => route.fulfill({ json: SIGNED_IN }));
    await page.route("**/api/runtime/device/WR1", (route) => route.fulfill({ json: { updated: true } }));
    await openModern(page, "#/control");
    const form = page.locator('form[data-endpoint="/api/runtime/device/WR1"]');
    await expect(form).toBeVisible();
    await form.locator('select[name="offgrid_socket_mode"]').selectOption("eco");
    await form.getByRole("button", { name: "Save" }).click();
    await expect(page.locator("dialog[open]")).toContainText("Change the off-grid socket of WR1?");
    await expect(page.getByRole("dialog", { name: "WR1" })).toBeVisible();
    const [request] = await Promise.all([
      page.waitForRequest((sent) => sent.url().endsWith("/api/runtime/device/WR1") && sent.method() === "PATCH"),
      page.locator("dialog[open]").getByTestId("confirm").click(),
    ]);
    expect(request.postDataJSON()).toEqual({ offgrid_socket_mode: "eco" });
    expect(request.headers()["x-csrf-token"]).toBe("preview-csrf-token");
  });

  test("the device settings and the rule reasons are said in the reader's language", async ({ page, context }) => {
    await context.addInitScript(() => window.localStorage.setItem("ems-modern.lang", "de"));
    await page.route("**/api/auth/status", (route) => route.fulfill({ json: SIGNED_IN }));
    await openModern(page, "#/control");
    const form = page.locator('form[data-endpoint="/api/runtime/device/WR1"]');
    await expect(form.locator('select[name="fan"] option')).toContainText(["Stufe 1", "Stufe 2"]);
    await expect(form).toContainText("Automatisch lässt den Wechselrichter den Lüfter");
    await page.goto("/#/devices", { waitUntil: "domcontentloaded" });
    const card = page.getByTestId("device-card").first();
    await card.getByText("Geräteeinstellungen").click();
    await expect(card).toContainText(/Licht\s*An/);
    await page.goto("/#/overview", { waitUntil: "domcontentloaded" });
    await page.getByText(/\d Regeln inaktiv/).click();
    await expect(page.getByText("inaktiv · alle Geräte online")).toBeVisible();
  });

  test("a field nobody changed is not sent and nothing is written", async ({ page }) => {
    await page.route("**/api/auth/status", (route) => route.fulfill({ json: SIGNED_IN }));
    let writes = 0;
    await page.route("**/api/runtime/system", (route) => {
      writes += 1;
      return route.fulfill({ json: { updated: true } });
    });
    await openModern(page, "#/control");
    const form = page.locator('form[data-endpoint="/api/runtime/system"]');
    await form.getByRole("button", { name: "Save" }).click();
    await expect(form).toContainText("Nothing to save");
    expect(writes).toBe(0);
  });

  test("a stored value that changes under an untouched card is shown and never written back", async ({ page }) => {
    await page.clock.install();
    await page.route("**/api/auth/status", (route) => route.fulfill({ json: SIGNED_IN }));
    let changedElsewhere = false;
    await page.route("**/api/runtime", async (route) => {
      const response = await route.fetch();
      const runtime = await response.json();
      if (changedElsewhere) {
        runtime.system.max_total_power = 600;
        runtime.devices.WR1.enabled = false;
      }
      await route.fulfill({ response, json: runtime });
    });
    const sent = [];
    await page.route(/\/api\/runtime\/(system|device\/WR1)$/, (route) => {
      sent.push(route.request().postDataJSON());
      return route.fulfill({ json: { updated: true } });
    });
    await openModern(page, "#/control");
    const system = page.locator('form[data-endpoint="/api/runtime/system"]');
    const device = page.locator('form[data-endpoint="/api/runtime/device/WR1"]');
    await expect(system.locator('input[name="max_total_power"]')).toHaveValue("800");
    await device.locator('input[name="pv_priority_factor"]').fill("3");

    changedElsewhere = true;
    await page.clock.fastForward(31_000);
    await expect(system.locator('input[name="max_total_power"]')).toHaveValue("600");
    await expect(device.locator('input[name="enabled"]')).not.toBeChecked();
    await expect(device.locator('input[name="pv_priority_factor"]')).toHaveValue("3");

    await system.locator('input[name="loop_interval"]').fill("7");
    await system.getByRole("button", { name: "Save" }).click();
    await device.getByRole("button", { name: "Save" }).click();
    await expect.poll(() => sent).toEqual([{ loop_interval: 7 }, { pv_priority_factor: 3 }]);
    await page.unrouteAll({ behavior: "ignoreErrors" });
  });

  test("a schedule rule is checked by the EMS before it can be saved", async ({ page }, testInfo) => {
    await clearScheduleRules(page.request);
    const name = `Evening cap ${testInfo.project.name} ${Date.now()}`;
    await page.route("**/api/auth/status", (route) => route.fulfill({ json: SIGNED_IN }));
    await openModern(page, "#/schedule");
    await page.getByRole("button", { name: "New rule" }).click();
    await expect(page.getByTestId("schedule-save")).toBeDisabled();
    await page.getByLabel("Name").fill(name);
    await page.getByRole("spinbutton", { name: "Set to", exact: true }).fill("600");
    await page.getByRole("spinbutton", { name: "Then set to" }).fill("800");
    await expect(page.getByTestId("schedule-preview")).not.toContainText("Checking");
    await expect(page.getByTestId("schedule-save")).toBeEnabled();
    const [request] = await Promise.all([
      page.waitForRequest((sent) => sent.url().endsWith("/api/schedule/rules") && sent.method() === "POST"),
      page.getByTestId("schedule-save").click(),
    ]);
    const body = request.postDataJSON();
    expect(body.rule.name).toBe(name);
    expect(body.rule.action).toEqual({ id: "total_output_limit", value: 600, end_value: 800 });
    await expect(page.locator("[data-rule]").filter({ hasText: name })).toBeVisible();
  });

  test("a session that ends on a screen nobody touches takes the operator page with it", async ({ page }) => {
    await page.clock.install();
    let session = SIGNED_IN;
    await page.route("**/api/auth/status", (route) => route.fulfill({ json: session }));
    await openModern(page, "#/logs");
    await expect(page.getByTestId("logs-output")).toBeVisible();
    session = { auth_configured: true, authenticated: false, write_mode_available: true, write_mode_active: false };
    await page.clock.fastForward(61_000);
    await expect(page.getByTestId("logs-output")).toHaveCount(0);
    await expect(page.locator("#content").getByText("Sign in to see this page.")).toBeVisible();
  });

  test("Clear empties the log view, the EMS keeps its log", async ({ page }) => {
    await page.route("**/api/auth/status", (route) => route.fulfill({ json: SIGNED_IN }));
    await openModern(page, "#/logs");
    const output = page.getByTestId("logs-output");
    await expect(output).toContainText("control_cycle");
    await page.getByRole("button", { name: "Clear" }).click();
    await expect(output).toContainText("No log lines");
  });

  test("a sign-out the server refuses says so and leaves the session as it is", async ({ page }) => {
    await page.route("**/api/auth/status", (route) => route.fulfill({ json: SIGNED_IN }));
    await page.route("**/api/auth/logout", (route) => route.fulfill({ status: 500, json: { error: "internal_error" } }));
    await openModern(page);
    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(page.getByRole("alert")).toHaveText("Sign-out failed");
    await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
  });

  test("an on/off rule opens in the editor with the value it holds", async ({ page }) => {
    await clearScheduleRules(page.request);
    const tomorrow = new Date(Date.now() + 86_400_000).toISOString().slice(0, 10);
    const created = await page.request.post("/api/schedule/rules", {
      headers: { "X-CSRF-Token": SIGNED_IN.csrf_token },
      data: {
        rule: {
          name: "Winter off",
          target: { scope: "system" },
          action: { id: "winter_mode", value: false },
          when: { start: `${tomorrow}T06:00` },
        },
      },
    });
    expect(created.status()).toBe(201);
    await page.route("**/api/auth/status", (route) => route.fulfill({ json: SIGNED_IN }));
    await openModern(page, "#/schedule");
    await page.locator("[data-rule]").filter({ hasText: "Winter off" }).getByRole("button", { name: "Edit" }).click();
    await expect(page.getByRole("combobox", { name: "Set to", exact: true })).toHaveValue("false");
  });
});
