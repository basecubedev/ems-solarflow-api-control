// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
// Consumers in the Modern dashboard, against the preview's "consumers" scenario:
// a washing machine at 420 W, a fridge at 85 W, an office at 60 W and an idle
// dishwasher in a house drawing 795 W, 230 W of it measured by no consumer. The
// System flow draws them as one block, the per-device flow names the two drawing
// most and the rest of the house as Other; every one leads to the Consumers
// page, which ranks them, stacks their history and prices them per period. The
// "many-consumers" scenario meters thirty things.
import { expect, test } from "@playwright/test";

async function open(page, baseURL, hash, scenario = "consumers", flow = "system") {
  await page.context().addCookies([{ name: "ems-preview-scenario", value: scenario, url: baseURL }]);
  await page.addInitScript((mode) => localStorage.setItem("ems-modern.flow", mode), flow);
  await page.goto(`/${hash}`, { waitUntil: "domcontentloaded" });
  await expect(page.getByTestId("ems-state")).toBeVisible();
}

test.describe("Modern dashboard consumers @smoke", () => {
  test("the System flow runs from the home to one block of every consumer, which opens the page", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/overview");
    const flow = page.getByTestId("energy-flow");
    const block = flow.getByTestId("flow-consumers");
    await expect(block.locator(".node-label")).toHaveText("Consumers");
    await expect(block.locator(".node-value")).toHaveText(/^565\sW$/);
    await expect(flow.locator('[data-wire="cons"]')).toHaveCount(1);
    await expect(flow).toHaveAttribute("aria-label", /Home use 795\sW, Consumers 565\sW/);
    const [home, cons] = await Promise.all([flow.locator("[data-node=home]").boundingBox(), block.boundingBox()]);
    expect(cons.x).toBeGreaterThan(home.x + home.width);
    await block.click();
    await expect(page).toHaveURL(/#\/consumers$/);
    await expect(page.getByTestId("consumer-row")).toHaveCount(4);
  });

  test("per device the flow names the two drawing most and the rest of the house, each leading to the page", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/overview", "consumers", "devices");
    const flow = page.getByTestId("energy-flow");
    const column = flow.getByTestId("flow-consumers");
    await expect(column.locator(".node-label")).toHaveText(["Washing machine", "Fridge", "Other"]);
    await expect(column.locator(".node-value")).toHaveText([/^420\sW$/, /^85\sW$/, /^290\sW$/]);
    for (const id of ["c0", "c1", "cother", "c-trunk"]) await expect(flow.locator(`[data-wire="${id}"]`)).toHaveCount(1);
    await expect(flow).toHaveAttribute("aria-label", /Washing machine 420\sW, Fridge 85\sW, Other 290\sW/);
    await column.getByTestId("flow-consumers-other").click();
    await expect(page).toHaveURL(/#\/consumers$/);
  });

  test("an installation without consumers draws no consumer node and an empty page", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/overview", "normal");
    await expect(page.getByTestId("energy-flow").locator("[data-node=home]")).toBeVisible();
    await expect(page.getByTestId("flow-consumers")).toHaveCount(0);
    await page.goto("/#/consumers");
    await expect(page.getByTestId("consumers-empty")).toContainText("No consumers yet");
  });

  test("the Consumers page ranks each consumer now, stacks them over time and prices them by period", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/consumers");
    const ranking = page.getByTestId("consumer-ranking");
    await expect(ranking.locator("li .name")).toHaveText(["Washing machine", "Not measured", "Fridge", "Office", "Dishwasher"]);
    await expect(ranking.locator('[data-consumer="Washing machine"]')).toContainText(/420\sW\s*53\s?%\s*912\sWh\s*€0\.07/);
    await expect(page.getByTestId("consumer-row-unmeasured")).toContainText(/230\sW/);
    const history = page.getByTestId("consumer-history");
    await expect(history.locator(".legend-item")).toHaveText([/^Dishwasher/, /^Fridge/, /^Office/, /^Washing machine/, /^Not measured/]);
    await expect(history.locator("canvas").first()).toBeVisible();
    const table = page.getByTestId("consumer-energy");
    await expect(table.locator("tbody tr").first()).toContainText(/Washing machine\s*912\sWh\s*711\sWh\s*201\sWh\s*€0\.07\s*€0\.25\s*16\s?%/);
    await expect(table.locator("tbody tr").last()).toContainText(/Not measured\s*3\.2\skWh/);
    await expect(table.locator("tfoot")).toContainText(/Home use\s*5\.7\skWh/);
    await page.getByRole("button", { name: "Last 12 months", exact: true }).click();
    await expect(table.locator('tr[data-consumer="Freezer"]')).toContainText(/Freezer \(until .*2026\)/);
    await expect(page.getByRole("button", { name: "Best day", exact: true })).toHaveCount(0);
  });

  test("the Consumers page counts the Energy page's periods, this year's months and the years, in the same cards", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/consumers");
    const periods = page.getByTestId("consumer-periods").locator("section.period");
    await expect(periods.locator(".period-label > span:first-child")).toHaveText(["Today", "Yesterday", "Last 7 days", "Last 4 weeks", "Last 12 months", "Peak day"]);
    const today = page.getByTestId("consumer-periods").getByRole("region", { name: "Today" });
    await expect(today.locator(".period-value")).toHaveText(/^2\.5\skWh$/);
    await expect(today.locator(".energy-facts dt")).toHaveText(["Cost", "Saved", "Share of home", "Most: Washing machine"]);
    await expect(page.getByTestId("consumer-periods").getByRole("region", { name: "Peak day" })).toContainText("18 Jan");
    await page.getByRole("button", { name: "Expert", exact: true }).click();
    await expect(today.locator(".energy-facts dt")).toHaveText(["Cost", "Saved", "Share of home", "Most: Washing machine", "From the grid", "Own supply", "Consumers"]);
    await page.getByRole("button", { name: "Basic", exact: true }).click();
    await expect(page.locator('.context [data-key="peakDay"]')).toContainText("18 January 2026");
    await expect(page.locator('.context [data-key="lifetime"]')).toContainText(/1,594\skWh/);

    const months = page.getByTestId("consumer-months");
    await expect(months.locator(".month-col")).toHaveCount(12);
    await months.locator(".month-col").first().click();
    const detail = page.getByTestId("consumer-month-detail");
    await expect(detail.locator(".month-name")).toHaveText(/^January \d{4}$/);
    await expect(detail.locator(".energy-facts").first().locator("dt").first()).toHaveText("Washing machine");
    await page.getByTestId("consumer-months").locator("..").getByRole("button", { name: "Table" }).click();
    await expect(page.getByTestId("consumer-months-table").locator("tbody tr")).toHaveCount(12);

    const years = page.getByTestId("consumer-years");
    await expect(years.locator("[data-year]")).toHaveCount(3);
    await expect(years.locator('[data-year="2025"]')).toContainText(/576\.4\skWh/);
    await expect(years.locator('[data-year="lifetime"]')).toContainText(/1,594\skWh/);
    await page.getByRole("button", { name: "Lifetime", exact: true }).click();
    await expect(page.getByTestId("consumer-energy").locator('tr[data-consumer="Freezer"]')).toContainText(/Freezer \(until .*2026\)/);
  });

  test("a consumer has the same colour in the month chart and in the power chart", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/consumers", "many-consumers");
    const legend = page.locator(".legend li", { hasText: "Heat pump" }).locator(".dot");
    const series = page.getByTestId("consumer-history").locator(".legend-item", { hasText: "Heat pump" }).locator(".swatch, .dot, i").first();
    await expect(legend).toBeVisible();
    await expect(series).toBeVisible();
    const colour = (locator) => locator.evaluate((element) => getComputedStyle(element).backgroundColor);
    expect(await colour(legend)).toBe(await colour(series));
  });

  test("thirty consumers read as a ranking or by name, and the history stacks five and the rest", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/consumers", "many-consumers");
    const ranking = page.getByTestId("consumer-ranking");
    await expect(page.getByTestId("consumer-row")).toHaveCount(30);
    await expect(ranking.locator("li .name").first()).toHaveText("Heat pump");
    await page.getByRole("button", { name: "A–Z", exact: true }).click();
    await expect(ranking).toHaveAttribute("data-order", "name");
    await expect(ranking.locator("li .name").first()).toHaveText("Aquarium");
    await expect(ranking.locator("li .name").last()).toHaveText("Not measured");
    await expect(page.getByTestId("consumer-energy").locator("tbody tr").first()).toContainText("Aquarium");
    await page.reload();
    await expect(page.getByTestId("consumer-ranking")).toHaveAttribute("data-order", "name");
    const legend = page.getByTestId("consumer-history").locator(".legend-item");
    await expect(legend).toHaveCount(6);
    await expect(legend.last()).toContainText("Other");
  });

  test("the Energy page carries the consumer table, and Analytics a consumer view", async ({ page, baseURL }) => {
    await open(page, baseURL, "#/energy");
    await expect(page.getByTestId("consumer-energy")).toContainText("Washing machine");
    await page.goto("/#/analytics");
    await page.getByTestId("analytics-view-block").getByRole("button", { name: "Consumers", exact: true }).click();
    await expect(page.getByTestId("analytics-consumer-kpi")).toHaveCount(5);
    await expect(page.getByTestId("analytics-consumer-kpi").first()).toContainText(/^Dishwasher · 24 h/);
    await expect(page.getByTestId("analytics-consumer-kpi").last()).toContainText(/^Not measured · 24 h/);
    await expect(page.getByTestId("analytics-view-block").locator("select")).toHaveCount(0);
  });

  test("on a phone the consumers stand under the home and the page keeps its width", async ({ page, baseURL }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await open(page, baseURL, "#/overview", "consumers", "devices");
    const flow = page.getByTestId("energy-flow");
    await expect(flow).toHaveClass(/narrow/);
    const [home, first] = await Promise.all([flow.locator("[data-node=home]").boundingBox(), flow.locator("[data-node=c0]").boundingBox()]);
    expect(first.y).toBeGreaterThan(home.y + home.height);
    await expect(flow.locator('[data-wire="c0"]')).toHaveCount(1);
    await expect(flow.locator('[data-wire="c-trunk"]')).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
    await page.goto("/#/consumers");
    await expect(page.getByTestId("consumer-row").first()).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  });
});
