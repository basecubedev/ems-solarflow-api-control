// SPDX-License-Identifier: AGPL-3.0-or-later
// What the Modern dashboard shows and how a reader moves through it, checked in
// the browser against the preview server: the device cards' firmware and
// settings blocks, how the Control page explains a cycle, the history and
// analytics charts' periods, devices, overlays and zoom, and the density and
// animation the page follows. Where the preview's own data could not tell a
// right answer from a wrong one, a test shapes the live snapshot or the series
// itself.
//
// Never `waitUntil: "networkidle"`: /api/events stays open.
import { expect, test } from "@playwright/test";
import { DOCS_SHOT_TIME, pinTheDay } from "./pin-the-day";

async function openModern(page, hash) {
  await page.goto(`/${hash}`, { waitUntil: "domcontentloaded" });
  await expect(page.getByTestId("ems-state")).toBeVisible();
}

/**
 * The live snapshot as the preview sends it, changed by ``shape``; the event
 * stream stays shut, so every snapshot the page reads is this one.
 */
async function shapeLive(page, shape) {
  await page.route("**/api/events", (route) => route.abort());
  await page.route("**/api/live", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    shape(body);
    await route.fulfill({ response, json: body });
  });
}

/** Every period, month and year of the preview with energy charged from AC, so the AC charging row is listed too. */
function withAcCharging(body) {
  const periods = Object.values(body.energy_stats || {}).flatMap((entry) => (Array.isArray(entry) ? entry : [entry]));
  const isPeriod = (entry) => entry && typeof entry === "object" && ("inverter_output_kwh" in entry || "inverter_output_wh" in entry);
  for (const period of periods.filter(isPeriod)) period.ac_charge_kwh = 2.4;
}

const deviceCard = (page, name) => page.locator(`[data-testid="device-card"][data-device="${name}"]`);
const block = (card, title) => card.locator("details.more").filter({ has: card.page().getByText(title, { exact: true }) });
const seriesRead = (path, check = () => true) => (sent) => new URL(sent.url()).pathname === path && check(new URL(sent.url()).searchParams);

test.describe("Modern dashboard views: device cards @smoke", () => {
  test("the firmware status block names each firmware value, an unknown one by its number", async ({ page }) => {
    await shapeLive(page, (body) => {
      Object.assign(body.devices.WR1, { ac_mode: 1, ac_status: 2, soc_limit: 1, pack_state: 2, dc_status: 2, grid_state: 0, soc_status: 1 });
      Object.assign(body.devices.WR2, { ac_mode: 2, ac_status: 0, soc_limit: 7, pack_state: 0, dc_status: 0, grid_state: 1, soc_status: null });
    });
    await openModern(page, "#/devices");
    const first = block(deviceCard(page, "WR1"), "Firmware status");
    await first.locator("summary").click();
    await expect(first.locator(".fact-label")).toHaveText(["AC path", "SoC guard", "Battery state", "DC path", "Grid", "SoC calibration"]);
    await expect(first.locator(".fact-value")).toHaveText(["AC charge active", "Max SoC reached", "Discharging", "DC battery output path", "Grid disconnected", "Calibration running"]);
    const second = block(deviceCard(page, "WR2"), "Firmware status");
    await second.locator("summary").click();
    await expect(second.locator(".fact-label")).toHaveText(["AC path", "SoC guard", "Battery state", "DC path", "Grid"]);
    await expect(second.locator(".fact-value")).toHaveText(["AC output standby", "Unknown (value 7)", "Standby", "DC standby", "Grid connected"]);
  });

  test("the device settings block shows what the device reports, and what the EMS wants instead after an arrow", async ({ page }) => {
    await shapeLive(page, (body) => {
      body.devices.WR1.settings = {
        light: { label: "Light", reported: "On", desired: "Off", reported_value: "on", desired_value: "off" },
        fan: { label: "Fan", reported: "Auto", desired: null, reported_value: "auto", desired_value: null },
        buzzer: { label: "Buzzer", reported: "Off", desired: "On", reported_value: "off", desired_value: "on" },
      };
      body.devices.WR2.settings = {};
    });
    await openModern(page, "#/devices");
    const settings = block(deviceCard(page, "WR1"), "Device settings");
    await settings.locator("summary").click();
    await expect(settings.locator(".fact-label")).toHaveText(["Light", "Fan", "Buzzer"]);
    await expect(settings.locator(".fact-value")).toHaveText(["On → Off", "Auto", "Off → On"]);
    await expect(block(deviceCard(page, "WR2"), "Device settings")).toHaveCount(0);
  });
});

/** One control cycle whose figures all differ, so each place on the page can be told apart. */
function distinctCycle(body) {
  Object.assign(body.control_explain, {
    filtered_load_w: 777,
    requested_total_w: 790,
    effective_target_total_w: 780,
    allocated_target_total_w: 760,
    commanded_total_w: 770,
    max_total_power_w: 900,
    deadband_w: 15,
    min_output_limit_w: 50,
    undistributed_target_w: 10,
    limits: [
      { name: "max_total_power", active: true, value: 900, reason: "requested total was clamped to the configured maximum" },
      { name: "night_min_soc_idle", active: false },
    ],
    notes: ["a note the EMS adds in its own words"],
  });
}

test.describe("Modern dashboard views: control @smoke", () => {
  test("the cycle is said in a sentence with its figures, its active limit and reason, the gates not active, and the EMS's notes", async ({ page }) => {
    await shapeLive(page, distinctCycle);
    await openModern(page, "#/control");
    const now = page.getByRole("region", { name: "What the EMS is doing now" });
    await expect(now.getByTestId("control-sentence")).toHaveText("With a measured load of 777 W it sets a target of 780 W, strategy PV first.");
    await expect(now.locator(".facts .fact-label")).toHaveText(["Measured load", "Requested", "Effective target", "Commanded", "Max total power", "Deadband", "Min output", "Undistributed"]);
    await expect(now.locator(".facts .fact-value")).toHaveText(["777 W", "790 W", "780 W", "770 W", "900 W", "15 W", "50 W", "10 W"]);
    const limits = now.locator(".lists li").filter({ hasText: "Maximum total power" });
    await expect(limits).toContainText("Maximum total power · 900 W");
    await expect(limits).toContainText("requested total was clamped to the configured maximum");
    await expect(now.locator(".inactive-gates")).toHaveText("Not active this cycle: Night idle at minimum SoC");
    const note = now.locator(".lists li", { hasText: "a note the EMS adds" });
    await expect(note).toHaveText("a note the EMS adds in its own words");
    await expect(note).toHaveAttribute("lang", "en");
  });

  test("the pipeline names its six stages in order, each with its own result and figures", async ({ page }) => {
    await shapeLive(page, distinctCycle);
    await openModern(page, "#/control");
    const stages = page.getByRole("region", { name: "How the target is made" }).locator("li.stage");
    await expect(stages.locator(".title")).toHaveText(["Measurements", "Target", "Distribution", "Limits and gates", "Commands", "Result"]);
    await expect(stages.locator(".result")).toHaveText(["777 W", "780 W", "760 W", "770 W", "Send", "780 W"]);
    await expect(stages.nth(2).locator("dd")).toHaveText(/^WR1 400\sW\s+WR2 400\sW$/);
    await expect(stages.nth(3).locator("dd")).toHaveText("Maximum total power");
    await expect(stages.nth(4).locator("dd")).toHaveText(["770 W", "Send 2"]);
    await expect(stages.nth(5).locator("dd")).toHaveText(/^WR1 320\sW\s+WR2 480\sW$/);
    await expect(stages.nth(4)).not.toHaveClass(/blocked/);
  });

  test("each device's decision names its target, its write and why, and opens into its five stages", async ({ page }) => {
    await shapeLive(page, (body) => {
      const devices = body.control_explain.devices;
      Object.assign(devices.WR1, { write_decision: "skip", write_reason: "deadband" });
      devices.WR2.online = false;
      delete devices.WR2.write_decision;
      delete devices.WR2.write_reason;
    });
    await openModern(page, "#/control");
    const cards = page.getByRole("region", { name: "Decision per device" }).locator("details.decision");
    await expect(cards).toHaveCount(2);
    const [first, second] = [cards.filter({ hasText: "WR1" }), cards.filter({ hasText: "WR2" })];
    await expect(first.locator("summary .target")).toHaveText("320 W");
    await expect(first.locator("summary .status")).toHaveText("No write");
    await expect(first.locator("summary .why")).toHaveText("WR1 target is shaped by PV-first allocation and local solar availability.");
    await expect(second.locator("summary .target")).toHaveText("480 W");
    await expect(second.locator("summary .status")).toHaveText("Blocked");

    await first.locator("summary").click();
    const stages = first.locator("ol.stages > li");
    await expect(stages.locator(".stage-head")).toHaveText(["01Inputs", "02Weighting", "03Raw target", "04Adjustments and limits", "05Final target"]);
    await expect(stages.nth(4).locator(".result b")).toHaveText("No write / 320 W");
    await expect(stages.nth(4)).toContainText("Change is inside the deadband");
    await second.locator("summary").click();
    await expect(second.locator("ol.stages > li").first().locator(".result b")).toHaveText("Offline");
    await expect(second.locator("ol.stages > li").first()).toHaveClass(/blocked/);

    const commands = page.getByRole("region", { name: "How the target is made" }).locator("li.stage").nth(4);
    await expect(commands.locator(".result")).toHaveText("Blocked");
    await expect(commands).toHaveClass(/blocked/);
  });
});

test.describe("Modern dashboard views: history @smoke", () => {
  test("the device history reads each period it is set to and titles itself by it", async ({ page }) => {
    await openModern(page, "#/devices");
    const card = page.locator(".card", { has: page.getByTestId("history-source") });
    const periods = card.getByRole("group", { name: "Time range" });
    for (const label of ["1 h", "6 h", "7 d", "24 h"]) {
      const range = label.replace(" ", "");
      const [asked] = await Promise.all([
        page.waitForRequest(seriesRead("/api/history/series", (params) => params.get("range") === range)),
        periods.getByRole("button", { name: label, exact: true }).click(),
      ]);
      const params = new URL(asked.url()).searchParams;
      expect(params.get("series")).toBe("pv,output,battery");
      expect(params.has("devices")).toBe(false);
      await expect(card.locator("h2")).toHaveText(`Power, last ${label}`);
      await expect(periods.getByRole("button", { name: label, exact: true })).toHaveAttribute("aria-pressed", "true");
    }
  });

  test("the device history shows one device's own answer, and all devices again", async ({ page }) => {
    await page.route("**/api/history/series**", (route) => {
      const one = new URL(route.request().url()).searchParams.get("devices");
      const now = Math.floor(Date.now() / 1000);
      const solar = one === "WR2" ? 222 : 999;
      return route.fulfill({ json: { available: true, source: "sqlite", time: [now - 120, now - 60], series: { pv: [solar, solar], output: [0, 0], battery: [0, 0] } } });
    });
    await openModern(page, "#/devices");
    const card = page.locator(".card", { has: page.getByTestId("history-source") });
    await card.getByRole("button", { name: "Table" }).click();
    const firstSolar = card.locator("table.data tbody tr").first().locator("td").nth(1);
    await expect(firstSolar).toHaveText("999 W");
    const pick = card.getByRole("combobox", { name: "Device" });
    const [one] = await Promise.all([page.waitForRequest(seriesRead("/api/history/series", (params) => params.has("devices"))), pick.selectOption("WR2")]);
    expect(new URL(one.url()).searchParams.get("devices")).toBe("WR2");
    await expect(firstSolar).toHaveText("222 W");
    const [all] = await Promise.all([page.waitForRequest(seriesRead("/api/history/series")), pick.selectOption("")]);
    expect(new URL(all.url()).searchParams.has("devices")).toBe(false);
    await expect(firstSolar).toHaveText("999 W");
  });

  test("the overview chart reads each period but today, which it already has, and titles itself by it", async ({ page }) => {
    await openModern(page, "#/overview");
    const card = page.locator(".card", { has: page.getByRole("group", { name: "Time range" }) }).first();
    const periods = card.getByRole("group", { name: "Time range" });
    await expect(card.locator("h2")).toHaveText("Power today");
    for (const label of ["1 h", "6 h", "24 h", "7 d"]) {
      const range = label.replace(" ", "");
      const [asked] = await Promise.all([
        page.waitForRequest(seriesRead("/api/history/series", (params) => params.get("range") === range)),
        periods.getByRole("button", { name: label, exact: true }).click(),
      ]);
      expect(new URL(asked.url()).searchParams.get("series")).toBe("pv,home,battery,grid");
      await expect(card.locator("h2")).toHaveText(`Power, last ${label}`);
    }
    let asked = 0;
    page.on("request", (sent) => {
      if (seriesRead("/api/history/series")(sent)) asked += 1;
    });
    await periods.getByRole("button", { name: "Today", exact: true }).click();
    await expect(card.locator("h2")).toHaveText("Power today");
    await expect(card.locator(".chart .u-over")).toBeVisible();
    expect(asked).toBe(0);
  });

  test("the history chart zooms by button, wheel and drag without asking the server again, and Reset zoom or Escape leave it", async ({ page }) => {
    await openModern(page, "#/devices");
    const card = page.locator(".card", { has: page.getByTestId("history-source") });
    const plot = card.locator(".chart .u-over");
    await expect(plot).toBeVisible();
    await expect(card.locator(".legend-time")).not.toHaveText("");
    let asked = 0;
    page.on("request", (sent) => {
      if (seriesRead("/api/history/series")(sent)) asked += 1;
    });
    const reset = card.getByTestId("chart-zoom-reset");

    await card.getByTestId("chart-zoom-in").click();
    await expect(reset).toBeVisible();
    await card.getByTestId("chart-zoom-out").click();
    await expect(reset).toHaveCount(0);

    const box = await plot.boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.wheel(0, -200);
    await expect(reset).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(reset).toHaveCount(0);

    await page.mouse.move(box.x + box.width * 0.3, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.6, box.y + box.height / 2, { steps: 6 });
    await page.mouse.up();
    await expect(reset).toBeVisible();
    await reset.click();
    await expect(reset).toHaveCount(0);
    expect(asked).toBe(0);
  });
});

const ANALYTICS_VIEWS = [
  { label: "Devices", series: ["pv", "output", "battery"], legend: ["Solar", "Output", "Battery"], kpis: ["Solar", "Output", "Battery charged", "Battery discharged"] },
  { label: "Grid", series: ["grid", "home", "ac_charge"], legend: ["Grid", "Home use", "AC charging"], kpis: ["Grid import", "Grid export", "Home use", "AC charging"] },
  { label: "Battery", series: ["battery"], legend: ["Battery"], kpis: ["Battery charged", "Battery discharged"] },
  { label: "Solar", series: ["pv", "output"], legend: ["Solar", "Output"], kpis: ["Solar", "Solar peak", "Output"] },
  { label: "Overview", series: ["pv", "output", "battery"], legend: ["Solar", "Output", "Battery"], kpis: ["Solar", "Output", "Battery charged", "Battery discharged"] },
];
const analyticsRead = (check = () => true) => seriesRead("/api/analytics/series", check);
const startsWith = (labels) => labels.map((label) => new RegExp(`^${label}`));

test.describe("Modern dashboard views: analytics @smoke", () => {
  test("each view reads its own series and names its chart, legend and tiles", async ({ page }) => {
    await openModern(page, "#/analytics");
    await expect(page.locator(".chart .u-over")).toBeVisible();
    const views = page.getByTestId("analytics-view-block").locator(".seg");
    for (const view of ANALYTICS_VIEWS) {
      const [asked] = await Promise.all([
        page.waitForRequest(analyticsRead((params) => params.get("series") === view.series.join(","))),
        views.getByRole("button", { name: view.label, exact: true }).click(),
      ]);
      expect(new URL(asked.url()).searchParams.get("range")).toBe("24h");
      const chart = page.getByRole("region", { name: view.label, exact: true });
      await expect(chart.locator(".legend-item")).toHaveText(startsWith(view.legend));
      await expect(page.locator(".kpi-label")).toHaveText([...view.kpis.map((kpi) => `${kpi} · 24 h`), "Current state of charge", "Runtime role"]);
      await expect(views.getByRole("button", { name: view.label, exact: true })).toHaveAttribute("aria-pressed", "true");
    }
  });

  test("one device's analytics are asked for that device, with its own state of charge, and all devices again", async ({ page }) => {
    await page.route("**/api/analytics/series**", (route) => {
      const one = new URL(route.request().url()).searchParams.get("devices");
      const now = Math.floor(Date.now() / 1000);
      const solar = one === "WR1" ? 111 : 888;
      return route.fulfill({ json: { available: true, time: [now - 120, now - 60], series: { pv: [solar, solar], output: [0, 0], battery: [0, 0] } } });
    });
    await openModern(page, "#/analytics");
    await page.getByRole("button", { name: "Table" }).click();
    const firstSolar = page.locator("table.data tbody tr").first().locator("td").nth(1);
    const soc = page.locator(".kpi").filter({ hasText: "Current state of charge" }).locator(".kpi-value");
    await expect(firstSolar).toHaveText("888 W");
    await expect(soc).toHaveText("60%");
    const pick = page.getByRole("combobox", { name: "Device" });
    const [one] = await Promise.all([page.waitForRequest(analyticsRead((params) => params.has("devices"))), pick.selectOption("WR1")]);
    expect(new URL(one.url()).searchParams.get("devices")).toBe("WR1");
    await expect(firstSolar).toHaveText("111 W");
    await expect(soc).toHaveText("62%");
    const [all] = await Promise.all([page.waitForRequest(analyticsRead()), pick.selectOption("")]);
    expect(new URL(all.url()).searchParams.has("devices")).toBe(false);
    await expect(firstSolar).toHaveText("888 W");
    await expect(soc).toHaveText("60%");
  });

  test("an overlay is asked for with the view's series, drawn dashed in the legend, and dropped again", async ({ page }) => {
    await openModern(page, "#/analytics");
    await expect(page.locator(".chart .u-over")).toBeVisible();
    const overlays = page.getByRole("group", { name: "Overlays" });
    const legend = page.locator(".legend-item");
    let series = ["pv", "output", "battery"];
    for (const [label, id] of [["EMS target", "target"], ["Grid", "grid"], ["AC charging", "ac_charge"]]) {
      series = [...series, id];
      const [asked] = await Promise.all([
        page.waitForRequest(analyticsRead((params) => params.get("series") === series.join(","))),
        overlays.getByRole("button", { name: label, exact: true }).click(),
      ]);
      expect(new URL(asked.url()).searchParams.get("range")).toBe("24h");
      await expect(overlays.getByRole("button", { name: label, exact: true })).toHaveAttribute("aria-pressed", "true");
      await expect(legend.filter({ hasText: label }).locator("i")).toHaveClass(/dash/);
    }
    await expect(legend.filter({ hasText: "Solar" }).locator("i")).not.toHaveClass(/dash/);

    await page.getByTestId("analytics-view-block").locator(".seg").getByRole("button", { name: "Grid", exact: true }).click();
    await expect(page.getByRole("region", { name: "Grid", exact: true })).toBeVisible();
    await expect(legend.filter({ hasText: /^Grid/ }).locator("i")).not.toHaveClass(/dash/);
    await expect(legend.filter({ hasText: "EMS target" }).locator("i")).toHaveClass(/dash/);
    await page.getByTestId("analytics-view-block").locator(".seg").getByRole("button", { name: "Overview", exact: true }).click();

    const [dropped] = await Promise.all([
      page.waitForRequest(analyticsRead((params) => params.get("series") === "pv,output,battery,grid,ac_charge")),
      overlays.getByRole("button", { name: "EMS target", exact: true }).click(),
    ]);
    expect(new URL(dropped.url()).searchParams.get("series")).not.toContain("target");
    await expect(legend.filter({ hasText: "EMS target" })).toHaveCount(0);
    await expect(overlays.getByRole("button", { name: "EMS target", exact: true })).toHaveAttribute("aria-pressed", "false");
  });

  test.describe("in a browser in Berlin", () => {
    test.use({ timezoneId: "Europe/Berlin" });

    test("a custom range is read from its start to its end in the browser's own time, and a range button reads that range again", async ({ page }) => {
      await openModern(page, "#/analytics");
      await expect(page.locator(".chart .u-over")).toBeVisible();
      const from = "2026-10-19T00:00";
      const to = "2026-10-22T12:00";
      await page.getByRole("textbox", { name: "From", exact: true }).fill(from);
      await page.getByRole("textbox", { name: "To", exact: true }).fill(to);
      const [custom] = await Promise.all([
        page.waitForRequest(analyticsRead((params) => params.has("start"))),
        page.getByRole("button", { name: "Apply", exact: true }).click(),
      ]);
      const params = new URL(custom.url()).searchParams;
      expect([Number(params.get("start")), Number(params.get("end"))]).toEqual([Date.UTC(2026, 9, 18, 22) / 1000, Date.UTC(2026, 9, 22, 10) / 1000]);
      expect(params.has("range")).toBe(false);
      const ranges = page.getByTestId("analytics-range-block").locator(".seg").getByRole("button");
      for (const button of await ranges.all()) await expect(button).toHaveAttribute("aria-pressed", "false");

      const [back] = await Promise.all([
        page.waitForRequest(analyticsRead((params) => params.get("range") === "7d")),
        page.getByRole("button", { name: "7 d", exact: true }).click(),
      ]);
      expect(new URL(back.url()).searchParams.has("start")).toBe(false);
      await expect(page.getByRole("button", { name: "7 d", exact: true })).toHaveAttribute("aria-pressed", "true");
    });
  });
});

test.describe("Modern dashboard views: energy @smoke", () => {
  test("Basic lists savings, self-sufficiency, solar and home use, and Expert goes on from there with AC charging", async ({ page }) => {
    await shapeLive(page, (body) => {
      withAcCharging(body);
      const stats = body.energy_stats;
      for (const month of stats.monthly_current_year || []) month.ratios = { self_sufficiency: 0.5 };
      Object.assign(stats.today, { ratios: { self_sufficiency: 0.54 }, channels: { pv_yield: { kwh: 4.4 }, home_consumption: { kwh: 5.7 } }, coverage: {} });
      stats.best_day = {
        ...stats.best_day,
        ratios: { self_sufficiency: null },
        channels: { pv_yield: { kwh: 3.1 }, home_consumption: { kwh: 0 } },
        coverage: { home_consumption: "none" },
      };
    });
    await pinTheDay(page, DOCS_SHOT_TIME);
    await openModern(page, "#/energy");
    const today = page.getByTestId("energy-periods").getByRole("region", { name: "Today" }).locator(".energy-facts dt");
    const basic = ["Savings", "Self-sufficiency", "Solar", "Home use"];
    await expect(today).toHaveText(basic);
    await expect(page.getByTestId("month-detail").locator(".energy-facts dt").first()).toHaveText("Savings");
    await expect(page.locator(".energy-facts dt", { hasText: "AC charging" })).toHaveCount(0);
    const bestDay = page.getByTestId("energy-periods").getByRole("region", { name: "Best day" }).locator(".energy-facts dt");
    await expect(bestDay).toHaveText(["Savings", "Solar"]);
    await page.getByRole("button", { name: "Table" }).click();
    await expect(page.locator("table.data thead th")).toHaveText(["Month", "Output", ...basic, "Data"]);

    await page.getByRole("button", { name: "Expert" }).click();
    await expect(today.nth(basic.length)).toHaveText("AC charging");
    for (const [index, label] of basic.entries()) {
      await expect(today.nth(index)).toHaveText(label);
      await expect(bestDay.nth(index)).toHaveText(label);
    }
  });
});

test.describe("Modern dashboard views: density and animation @smoke", () => {
  test("Compact tightens the page, and the choice survives a reload", async ({ page }) => {
    await openModern(page, "#/overview");
    const root = page.locator("html");
    const card = page.locator(".card").first();
    const padding = () => card.evaluate((node) => parseFloat(getComputedStyle(node).paddingTop));
    await expect(root).toHaveAttribute("data-density", "normal");
    const roomy = await padding();
    await page.getByTestId("appearance-button").first().click();
    const density = page.getByTestId("appearance").getByRole("group", { name: "Density" });
    await density.getByRole("button", { name: "Compact", exact: true }).click();
    await expect(root).toHaveAttribute("data-density", "compact");
    await expect.poll(padding).toBeLessThan(roomy);

    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(root).toHaveAttribute("data-density", "compact");
    await expect.poll(padding).toBeLessThan(roomy);
  });

  test("the flow moves at the installation's animation mode: reduced at half the speed, off not at all, nor where the reader asks for less motion", async ({ page }) => {
    let mode = "normal";
    await page.route("**/api/ui-config", async (route) => {
      const response = await route.fetch();
      await route.fulfill({ response, json: { ...(await response.json()), animation_mode: mode } });
    });
    await shapeLive(page, () => {});
    const rates = () =>
      page.getByTestId("energy-flow").locator("[data-run]").evaluateAll((runs) =>
        Object.fromEntries(
          runs.flatMap((run, runIndex) =>
            [...run.querySelectorAll(".dashes")].map((dash, index) => [`${runIndex}:${run.dataset.run}:${index}`, dash.getAnimations()[0]?.playbackRate ?? null]),
          ),
        ),
      );
    const moving = async () => {
      const values = Object.values(await rates());
      return values.length > 0 && values.every((rate) => rate > 0);
    };
    const still = async () => Object.values(await rates()).every((rate) => rate === null);

    await openModern(page, "#/overview");
    await expect.poll(moving).toBe(true);
    const normal = await rates();
    await expect(page.locator("html")).toHaveAttribute("data-motion", "on");

    mode = "reduced";
    await page.reload({ waitUntil: "domcontentloaded" });
    const halved = Object.fromEntries(Object.keys(normal).map((key) => [key, 2]));
    await expect
      .poll(async () => {
        const now = await rates();
        return Object.fromEntries(Object.keys({ ...normal, ...now }).map((key) => [key, now[key] > 0 && normal[key] > 0 ? Number((normal[key] / now[key]).toFixed(3)) : null]));
      })
      .toEqual(halved);

    mode = "off";
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.locator("html")).toHaveAttribute("data-motion", "off");
    await expect(page.getByTestId("energy-flow").locator("[data-run] .dashes").first()).toBeAttached();
    await expect.poll(still).toBe(true);
    await page.getByTestId("appearance-button").first().click();
    await expect(page.getByTestId("appearance")).toContainText("Animation is switched off for this installation.");

    mode = "normal";
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.locator("html")).toHaveAttribute("data-motion", "off");
    await expect(page.getByTestId("energy-flow").locator("[data-run] .dashes").first()).toBeAttached();
    await expect.poll(still).toBe(true);
  });
});

/**
 * The energy labels fit where they stand, in every language and at the widths
 * where the cards change their columns and just beside them: a fact row keeps
 * its label and its value on one line, an Analytics KPI label stays on one
 * line, and the status tiles, device cards, grid meter and flow keep every
 * line whole: not cut off, and not broken onto a second line.
 */
test.describe("Modern dashboard views: labels fit @smoke", () => {
  const LANGUAGES = ["en", "de", "nl", "fr"];
  const WIDTHS = [1920, 1440, 1300, 1225, 960, 905, 640, 632, 390, 360];

  async function inLanguage(page, hash, lang) {
    await openModern(page, hash);
    await page.evaluate((code) => {
      localStorage.setItem("ems-modern.lang", code);
      localStorage.setItem("ems-modern.energyDetail", "expert");
    }, lang);
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.locator("html")).toHaveAttribute("lang", lang);
  }

  const brokenFacts = (page) => page.locator(".energy-facts > div").evaluateAll((rows) => rows.flatMap((row) => {
    const [label, value] = [row.querySelector("dt"), row.querySelector("dd")];
    const line = parseFloat(getComputedStyle(label).fontSize) * 1.7;
    const end = row.getBoundingClientRect().right;
    const fits = label.getBoundingClientRect().height <= line && value.getBoundingClientRect().height <= line && value.getBoundingClientRect().right <= end + 0.5;
    return fits ? [] : [label.textContent.trim()];
  }));
  const broken = (locator) => locator.evaluateAll((elements) => elements.flatMap((element) => {
    let lines = 1;
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const range = document.createRange();
      range.selectNodeContents(node);
      const tops = [...range.getClientRects()].filter((rect) => rect.width > 0).map((rect) => rect.top).sort((a, b) => a - b);
      lines = Math.max(lines, tops.filter((top, index) => index === 0 || top - tops[index - 1] > 4).length);
    }
    return lines > 1 || element.scrollWidth > element.clientWidth + 1 ? [element.textContent.trim()] : [];
  }));

  test("every energy fact keeps its label and its value on one line", async ({ page }) => {
    await shapeLive(page, withAcCharging);
    for (const lang of LANGUAGES) {
      await inLanguage(page, "#/energy", lang);
      await expect(page.getByTestId("energy-periods").locator(".energy-facts > div")).not.toHaveCount(0);
      await expect(page.locator(".energy-facts dt .c-grid").first()).toBeVisible();
      for (const width of WIDTHS) {
        await page.setViewportSize({ width, height: 900 });
        expect(await brokenFacts(page), `${lang} at ${width} px`).toEqual([]);
      }
    }
  });

  test("every Analytics KPI label stays on one line", async ({ page }) => {
    for (const lang of LANGUAGES) {
      await inLanguage(page, "#/analytics", lang);
      const views = page.locator('[role="group"]').first().getByRole("button");
      for (let index = 0; index < await views.count(); index += 1) {
        await views.nth(index).click();
        await expect(page.locator(".kpi-label").first()).toBeVisible();
        for (const width of WIDTHS) {
          await page.setViewportSize({ width, height: 900 });
          expect(await broken(page.locator(".kpi-label")), `${lang}, view ${index}, at ${width} px`).toEqual([]);
        }
      }
    }
  });

  const offline = (count) => (body) => {
    Object.values(body.devices || {}).slice(0, count).forEach((device) => { device.online = false; });
  };
  const STATES = {
    "all normal": { level: "ok", shape: () => {} },
    "control off": { level: "paused", shape: (body) => { body.controller = { ...body.controller, enabled: false }; } },
    "grid meter offline": { level: "fault", shape: (body) => { body.grid_power_valid = false; } },
    "every device offline": { level: "fault", shape: offline(Infinity) },
    "device power not valid": { level: "attention", shape: (body) => { body.device_power_valid = false; } },
    "one device offline": { level: "attention", shape: offline(1) },
  };
  for (const [state, { level, shape }] of Object.entries(STATES)) {
    test(`the status tiles and the energy flow keep every line whole: ${state}`, async ({ page }) => {
      await shapeLive(page, shape);
      for (const lang of LANGUAGES) {
        await inLanguage(page, "#/overview", lang);
        await expect(page.getByTestId("kpi-status")).toHaveAttribute("data-level", level);
        await expect(page.locator('[data-testid="energy-flow"] [data-node="grid"] .node-label')).toHaveText(/^\S+ · \S/);
        const lines = page.locator('[data-testid^="kpi-"] :is(.kpi-label, .kpi-value, .kpi-meta), [data-testid="energy-flow"] :is(.node-label, .hub-label)');
        for (const width of WIDTHS) {
          await page.setViewportSize({ width, height: 900 });
          expect(await broken(lines), `${lang} at ${width} px`).toEqual([]);
        }
      }
    });
  }

  for (const scenario of ["ac-charging", "firmware-status"]) {
    test(`the device cards, the grid meter and the flow keep every line whole: ${scenario}`, async ({ page, baseURL }) => {
      await page.context().addCookies([{ name: "ems-preview-scenario", value: scenario, url: baseURL }]);
      for (const lang of LANGUAGES) {
        await inLanguage(page, "#/devices", lang);
        await expect(page.getByTestId("device-card")).not.toHaveCount(0);
        await page.locator('[data-testid="device-card"] details').evaluateAll((blocks) => blocks.forEach((block) => { block.open = true; }));
        const lines = page.locator('[data-testid="device-card"] :is(.fact-label .ellipsis, .fact-value), [data-testid="grid-meter"] :is(.name, .fact-label .ellipsis, .fact-value), [data-testid="energy-flow"] :is(.node-label, .hub-label)');
        for (const width of WIDTHS) {
          await page.setViewportSize({ width, height: 900 });
          expect(await broken(lines), `${lang} at ${width} px`).toEqual([]);
        }
      }
    });
  }
});
