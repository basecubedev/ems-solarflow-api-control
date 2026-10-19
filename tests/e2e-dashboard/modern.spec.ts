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

async function openModern(page, hash = "#/overview") {
  await page.goto(`/modern/${hash}`, { waitUntil: "domcontentloaded" });
  await expect(page.getByTestId("ems-state")).toBeVisible();
}

test.describe("Modern dashboard @smoke", () => {
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

  test("on a phone the sidebar gives way to a bottom bar", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await openModern(page);
    await expect(page.locator(".sidebar")).toBeHidden();
    const bottom = page.locator(".bottom-nav");
    await expect(bottom).toBeVisible();
    await bottom.getByRole("link", { name: "Devices" }).click();
    await expect(page.getByTestId("device-card").first()).toBeVisible();
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
