// SPDX-License-Identifier: AGPL-3.0-or-later
// The Modern dashboard at /modern/, against the same preview server and the
// same API as Classic. Never `waitUntil: "networkidle"`: /api/events stays open.
import { expect, test } from "@playwright/test";

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
  await page.goto(`/modern/${hash}`, { waitUntil: "domcontentloaded" });
  await expect(page.getByTestId("ems-state")).toBeVisible();
}

test.describe("Modern dashboard @smoke", () => {
  test("demo mode and a browser too old for Modern stay on Classic whatever the choice", async ({ page }) => {
    await page.goto("/?ui=modern", { waitUntil: "domcontentloaded" });
    await expect(page).toHaveURL(/\/modern\/$/);
    await page.goto("/?Demo=TRUE", { waitUntil: "domcontentloaded" });
    await expect(page.locator("#themeSelect")).toBeVisible();
    await expect(page).toHaveURL(/\/\?Demo=TRUE$/);
    await page.goto("/?demo=0", { waitUntil: "domcontentloaded" });
    await expect(page).toHaveURL(/\/modern\/$/);

    await page.addInitScript(() => {
      delete HTMLElement.prototype.popover;
    });
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await expect(page.locator("#themeSelect")).toBeVisible();
    await expect(page).not.toHaveURL(/modern/);
  });

  test("Classic links to Modern, Modern back to Classic, and the choice is remembered", async ({ page }) => {
    await page.goto("/?ui=classic", { waitUntil: "domcontentloaded" });
    await expect(page.locator("#themeSelect")).toBeVisible();
    await page.locator("#modernLink").click();
    await expect(page).toHaveURL(/\/modern\/$/);
    await expect(page.getByTestId("ems-state")).toBeVisible();

    await page.goto("/", { waitUntil: "domcontentloaded" });
    await expect(page).toHaveURL(/\/modern\//);

    await page.getByTestId("switch-classic").click();
    await expect(page).toHaveURL(/\/\?ui=classic$/);
    await expect(page.locator("#themeSelect")).toBeVisible();
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await expect(page).not.toHaveURL(/modern/);
  });

  test("a browser that has never chosen opens Modern", async ({ page }) => {
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await expect(page).toHaveURL(/\/modern\/$/);
    await expect(page.getByTestId("ems-state")).toBeVisible();
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

    await page.goto("/modern/#/energy", { waitUntil: "domcontentloaded" });
    await expect(page.getByTestId("energy-periods")).toContainText(/Last 7 days\s*18\.4\d? kWh/);
    await page.getByRole("button", { name: "Table" }).click();
    await expect(page.locator("table.data tbody tr")).toHaveCount(12);
    await expect(page.locator("table.data")).toContainText(/\d kWh/);

    await page.goto("/modern/#/analytics", { waitUntil: "domcontentloaded" });
    await expect(page.locator(".kpi").filter({ hasText: "Current SoC" })).toContainText("60%");
  });

  test("a month opens with every figure Classic shows for it, channels in Expert", async ({ page }) => {
    await openModern(page, "#/energy");
    await page.getByRole("button", { name: "Expert" }).click();
    await page.getByRole("button", { name: /^Sept: / }).click();
    const detail = page.getByTestId("month-detail");
    await expect(detail).toContainText("September");
    await expect(detail).toContainText("66.9 kWh");
    await expect(detail).toContainText(/Grid import\s*28\.9 kWh/);
    await expect(detail).toContainText("Measured since");
    await expect(page.locator("dl.context")).toContainText("Channels measured since");
  });

  test("the rules say why they are on, and a chart zooms and resets", async ({ page }) => {
    await openModern(page);
    await expect(page.getByTestId("active-rules")).toContainText("preview control loop active");
    await page.goto("/modern/#/analytics", { waitUntil: "domcontentloaded" });
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
