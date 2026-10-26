// SPDX-License-Identifier: AGPL-3.0-or-later
// What an operator does on the Modern dashboard's Control, Schedule and
// Maintenance pages, checked in the browser against the preview server: each
// action asks what Classic asks before it, sends exactly what changed with the
// CSRF token, and shows the answer. The preview's runtime and maintenance
// routes only acknowledge a write, so a test answers it itself where the
// page's reaction depends on the answer; the schedule routes are the real ones
// over the preview's scratch copy of the runtime state.
//
// Never `waitUntil: "networkidle"`: /api/events stays open.
import { expect, test } from "@playwright/test";

const CSRF = "preview-csrf-token";
const SIGNED_IN = {
  auth_configured: true,
  authenticated: true,
  csrf_token: CSRF,
  write_mode_available: true,
  write_mode_active: true,
};

async function openSignedIn(page, hash) {
  await page.route("**/api/auth/status", (route) => route.fulfill({ json: SIGNED_IN }));
  await page.goto(`/${hash}`, { waitUntil: "domcontentloaded" });
  await expect(page.getByTestId("ems-state")).toBeVisible();
}

/**
 * The preview's runtime state, kept by the test as the EMS keeps it: a PATCH
 * is recorded and merged in, so the reload after a save shows what was stored.
 */
async function runtimeStore(page, adjust = (runtime) => runtime) {
  const writes = [];
  let stored = null;
  await page.route("**/api/runtime", async (route) => {
    if (!stored) stored = adjust(await (await route.fetch()).json());
    await route.fulfill({ json: stored });
  });
  await page.route(/\/api\/runtime\/(system|winter|ac_charge_control|device\/[^/?]+)$/, async (route) => {
    const request = route.request();
    const target = new URL(request.url()).pathname.replace("/api/runtime/", "");
    const body = request.postDataJSON();
    writes.push({ target, method: request.method(), body, csrf: request.headers()["x-csrf-token"] });
    if (target.startsWith("device/")) Object.assign(stored.devices[decodeURIComponent(target.slice("device/".length))], body);
    else Object.assign(stored[target], body);
    await route.fulfill({ json: { updated: true } });
  });
  return writes;
}

const form = (page, endpoint) => page.locator(`form[data-endpoint="${endpoint}"]`);

async function save(page, card) {
  await card.getByRole("button", { name: "Save" }).click();
}

test.describe("Modern dashboard operator: runtime settings @smoke", () => {
  test("the EMS goes off only after its warning, a cancel sends nothing, and it goes on again without one", async ({ page }) => {
    const writes = await runtimeStore(page);
    await openSignedIn(page, "#/control");
    const system = form(page, "/api/runtime/system");
    const dialog = page.locator("dialog[open]");
    await system.locator('input[name="enabled"]').uncheck();

    await save(page, system);
    await expect(dialog).toContainText("Turn the EMS off?");
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(system.getByRole("status")).toHaveText("Not saved.");
    expect(writes).toEqual([]);

    await save(page, system);
    await dialog.getByTestId("confirm").click();
    await expect.poll(() => writes.length).toBe(1);
    await expect(system.getByRole("status")).toHaveText("Saved");
    await expect(system.locator('input[name="enabled"]')).not.toBeChecked();

    await system.locator('input[name="enabled"]').check();
    await save(page, system);
    await expect.poll(() => writes.length).toBe(2);
    await expect(dialog).toHaveCount(0);
    expect(writes).toEqual([
      { target: "system", method: "PATCH", body: { enabled: false }, csrf: CSRF },
      { target: "system", method: "PATCH", body: { enabled: true }, csrf: CSRF },
    ]);
  });

  test("the minimum output goes out as a number, and once stored a second Save sends nothing", async ({ page }) => {
    const writes = await runtimeStore(page);
    await openSignedIn(page, "#/control");
    const system = form(page, "/api/runtime/system");
    await system.locator('input[name="min_output_limit"]').fill("120");
    await save(page, system);
    await expect.poll(() => writes).toEqual([{ target: "system", method: "PATCH", body: { min_output_limit: 120 }, csrf: CSRF }]);
    await expect(system.getByRole("status")).toHaveText("Saved");
    await expect(system.locator('input[name="min_output_limit"]')).toHaveValue("120");

    await save(page, system);
    await expect(system.getByRole("status")).toHaveText("Nothing to save: no value was changed.");
    expect(writes).toHaveLength(1);
  });

  test("a device's AC charging, max power and AC charge power go out as one write, and AC charging is forbidden again", async ({ page }) => {
    const writes = await runtimeStore(page);
    await openSignedIn(page, "#/control");
    const device = form(page, "/api/runtime/device/WR1");
    await device.locator('input[name="ac_charge_enabled"]').check();
    await device.locator('input[name="max_power"]').fill("600");
    await device.locator('input[name="ac_charge_power_w"]').fill("400");
    await save(page, device);
    await expect.poll(() => writes.length).toBe(1);
    await expect(page.locator("dialog[open]")).toHaveCount(0);
    await expect(device.getByRole("status")).toHaveText("Saved");

    await device.locator('input[name="ac_charge_enabled"]').uncheck();
    await save(page, device);
    await expect.poll(() => writes.length).toBe(2);
    expect(writes).toEqual([
      { target: "device/WR1", method: "PATCH", body: { ac_charge_enabled: true, max_power: 600, ac_charge_power_w: 400 }, csrf: CSRF },
      { target: "device/WR1", method: "PATCH", body: { ac_charge_enabled: false }, csrf: CSRF },
    ]);
    await expect(device.locator('input[name="max_power"]')).toHaveValue("600");
  });

  test("switching a device to AC charging asks first; back to output it does not while the EMS and the device are on", async ({ page }) => {
    const writes = await runtimeStore(page);
    await openSignedIn(page, "#/control");
    const device = form(page, "/api/runtime/device/WR1");
    const dialog = page.locator("dialog[open]");
    await device.locator('select[name="runtime_role"]').selectOption("ac_input");
    await save(page, device);
    await expect(dialog).toContainText("Switch WR1 to AC charging?");
    await dialog.getByTestId("confirm").click();
    await expect.poll(() => writes.length).toBe(1);
    await expect(device.locator('select[name="runtime_role"]')).toHaveValue("ac_input");

    await device.locator('select[name="runtime_role"]').selectOption("ac_output");
    await save(page, device);
    await expect.poll(() => writes.length).toBe(2);
    await expect(dialog).toHaveCount(0);
    expect(writes.map((write) => write.body)).toEqual([{ runtime_role: "ac_input" }, { runtime_role: "ac_output" }]);
  });

  test("light, fan, buzzer and battery heating are asked about, sent as their words, and handed back to the device", async ({ page }) => {
    const writes = await runtimeStore(page, (runtime) => {
      runtime._limits.device_settings_offered.WR1 = ["light", "fan", "buzzer", "auto_heat"];
      return runtime;
    });
    await openSignedIn(page, "#/control");
    const device = form(page, "/api/runtime/device/WR1");
    const dialog = page.locator("dialog[open]");
    await device.locator('select[name="light"]').selectOption("off");
    await device.locator('select[name="fan"]').selectOption("2");
    await device.locator('select[name="buzzer"]').selectOption("on");
    await device.locator('select[name="auto_heat"]').selectOption("off");
    await save(page, device);
    await expect(dialog).toContainText("Change Light, Fan, Buzzer, Battery heating of WR1?");
    await expect(dialog).toContainText("The EMS writes Light, Fan, Buzzer, Battery heating to the inverter and keeps them there.");
    await expect(dialog).toContainText("With battery heating off, the battery does not warm itself in the cold.");
    await dialog.getByTestId("confirm").click();
    await expect.poll(() => writes.length).toBe(1);
    expect(writes[0]).toEqual({ target: "device/WR1", method: "PATCH", body: { light: "off", fan: "2", buzzer: "on", auto_heat: "off" }, csrf: CSRF });
    await expect(device.locator('select[name="fan"]')).toHaveValue("2");

    await device.locator('select[name="light"]').selectOption({ label: "Device decides" });
    await save(page, device);
    await expect(dialog).toContainText("Change Light of WR1?");
    await expect(dialog).toContainText("The EMS writes nothing for Light from now on");
    await dialog.getByTestId("confirm").click();
    await expect.poll(() => writes.length).toBe(2);
    expect(writes[1].body).toEqual({ light: "unmanaged" });
  });

  test("winter mode and AC charging for the whole system are switched on their own cards", async ({ page }) => {
    const writes = await runtimeStore(page);
    await openSignedIn(page, "#/control");
    const winter = form(page, "/api/runtime/winter");
    const charging = form(page, "/api/runtime/ac_charge_control");
    await winter.locator('input[name="enabled"]').check();
    await save(page, winter);
    await expect.poll(() => writes.length).toBe(1);
    await charging.locator('input[name="enabled"]').check();
    await save(page, charging);
    await expect.poll(() => writes.length).toBe(2);
    await expect(page.locator("dialog[open]")).toHaveCount(0);
    expect(writes).toEqual([
      { target: "winter", method: "PATCH", body: { enabled: true }, csrf: CSRF },
      { target: "ac_charge_control", method: "PATCH", body: { enabled: true }, csrf: CSRF },
    ]);
    await expect(winter.locator('input[name="enabled"]')).toBeChecked();
    await expect(charging.locator('input[name="enabled"]')).toBeChecked();
  });

  test("a value the EMS refuses is said in the field's own words and the edit stays", async ({ page }) => {
    await page.route("**/api/runtime/device/WR1", (route) =>
      route.fulfill({ status: 400, json: { error: "invalid_runtime_update", message: "max_power must be between 0 and 800" } }));
    await openSignedIn(page, "#/control");
    const device = form(page, "/api/runtime/device/WR1");
    await device.locator('input[name="max_power"]').fill("900");
    await save(page, device);
    await expect(device.getByRole("status")).toHaveText("Max power must be between 0 and 800");
    await expect(device.getByRole("status")).toHaveClass(/form-error/);
    await expect(device.locator('input[name="max_power"]')).toHaveValue("900");
  });
});

async function clearScheduleRules(request) {
  const overview = await (await request.get("/api/schedule")).json();
  for (const rule of overview.rules || []) {
    const revision = rule.revision ? `?revision=${encodeURIComponent(rule.revision)}` : "";
    await request.delete(`/api/schedule/rules/${encodeURIComponent(rule.id)}${revision}`, { headers: { "X-CSRF-Token": CSRF } });
  }
}

function tomorrow() {
  const day = new Date(Date.now() + 86_400_000);
  return `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(day.getDate()).padStart(2, "0")}`;
}

async function addRule(request, name, value = 600) {
  const created = await request.post("/api/schedule/rules", {
    headers: { "X-CSRF-Token": CSRF },
    data: {
      rule: {
        name,
        target: { scope: "system" },
        action: { id: "total_output_limit", value },
        when: { start: `${tomorrow()}T06:00`, repeat: { freq: "daily" } },
      },
    },
  });
  expect(created.status()).toBe(201);
}

async function storedRule(request, name) {
  const overview = await (await request.get("/api/schedule")).json();
  return (overview.rules || []).find((rule) => rule.name === name);
}

const ruleCard = (page, name) => page.locator("[data-rule]").filter({ hasText: name });

test.describe("Modern dashboard operator: schedule @smoke", () => {
  test.beforeEach(async ({ request }) => {
    await clearScheduleRules(request);
    await request.patch("/api/schedule", { headers: { "X-CSRF-Token": CSRF }, data: { enabled: true } });
  });

  test("a schedule that cannot be read says so, and Try again reads it", async ({ page }) => {
    let failing = true;
    await page.route("**/api/schedule", (route) => {
      if (failing && route.request().method() === "GET") {
        failing = false;
        return route.fulfill({ status: 500, json: { error: "internal_error" } });
      }
      return route.fallback();
    });
    await openSignedIn(page, "#/schedule");
    await expect(page.getByText("Something went wrong (internal_error).")).toBeVisible();
    await page.getByRole("button", { name: "Try again" }).click();
    await expect(page.getByText("Schedules on")).toBeVisible();
    await expect(page.getByText("No rules yet.", { exact: false })).toBeVisible();
  });

  test("a rule is switched off and on again from its card", async ({ page, request }) => {
    await addRule(request, "Morning cap");
    await openSignedIn(page, "#/schedule");
    const card = ruleCard(page, "Morning cap");
    await expect(card).toContainText("Waiting");
    const [off] = await Promise.all([
      page.waitForRequest((sent) => sent.method() === "PUT" && sent.url().includes("/api/schedule/rules/")),
      card.getByRole("button", { name: "Switch off" }).click(),
    ]);
    expect(off.postDataJSON().rule.enabled).toBe(false);
    expect(off.headers()["x-csrf-token"]).toBe(CSRF);
    await expect(card).toContainText("Paused");
    await expect(card.getByRole("button", { name: "Apply now" })).toBeDisabled();
    await card.getByRole("button", { name: "Switch on" }).click();
    await expect(card).toContainText("Waiting");
    expect((await storedRule(request, "Morning cap")).enabled).toBe(true);
  });

  test("Skip next moves the rule's next switch point to the day after", async ({ page, request }) => {
    await addRule(request, "Morning cap");
    const before = (await storedRule(request, "Morning cap")).next.at;
    await openSignedIn(page, "#/schedule");
    const card = ruleCard(page, "Morning cap");
    const next = card.getByText(/^Next: /);
    await expect(next).toBeVisible();
    const shown = await next.textContent();
    const [skip] = await Promise.all([
      page.waitForRequest((sent) => sent.method() === "POST" && sent.url().endsWith("/skip")),
      card.getByRole("button", { name: "Skip next" }).click(),
    ]);
    expect(Object.keys(skip.postDataJSON())).toEqual(["revision"]);
    const [year, month, day] = before.slice(0, 10).split("-").map(Number);
    const dayAfter = new Date(Date.UTC(year, month - 1, day + 1)).toISOString().slice(0, 10);
    await expect.poll(async () => (await storedRule(request, "Morning cap")).next.at.slice(0, 10)).toBe(dayAfter);
    await expect(next).not.toHaveText(shown);
  });

  test("Apply now sets the rule's value at once and says what it set", async ({ page, request }) => {
    await addRule(request, "Morning cap", 650);
    await openSignedIn(page, "#/schedule");
    const [apply] = await Promise.all([
      page.waitForRequest((sent) => sent.method() === "POST" && sent.url().endsWith("/apply-now")),
      ruleCard(page, "Morning cap").getByRole("button", { name: "Apply now" }).click(),
    ]);
    expect(apply.headers()["x-csrf-token"]).toBe(CSRF);
    await expect(page.locator(".message[role=status]")).toContainText(/^Set to .*650/);
  });

  test("rules move down and up in the list, and the first cannot move up", async ({ page, request }) => {
    await addRule(request, "First cap", 600);
    await addRule(request, "Second cap", 700);
    await openSignedIn(page, "#/schedule");
    const names = page.locator("[data-rule] .rule-name");
    await expect(names).toHaveText(["First cap", "Second cap"]);
    await expect(page.getByRole("button", { name: "Move First cap up" })).toBeDisabled();
    await page.getByRole("button", { name: "Move First cap down" }).click();
    await expect(names).toHaveText(["Second cap", "First cap"]);
    await page.getByRole("button", { name: "Move First cap up" }).click();
    await expect(names).toHaveText(["First cap", "Second cap"]);
  });

  test("a rule is deleted only after its question, and a cancel keeps it", async ({ page, request }) => {
    await addRule(request, "Morning cap");
    await openSignedIn(page, "#/schedule");
    const card = ruleCard(page, "Morning cap");
    const dialog = page.locator("dialog[open]");
    await card.getByRole("button", { name: "Delete" }).click();
    await expect(dialog).toContainText("Delete “Morning cap”?");
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(card).toBeVisible();
    await card.getByRole("button", { name: "Delete" }).click();
    const [removed] = await Promise.all([
      page.waitForRequest((sent) => sent.method() === "DELETE"),
      dialog.getByTestId("confirm").click(),
    ]);
    expect(new URL(removed.url()).searchParams.get("revision")).toBeTruthy();
    await expect(card).toHaveCount(0);
    expect(await storedRule(request, "Morning cap")).toBeUndefined();
  });

  test("an unreadable entry offers only Delete and is deleted without a revision", async ({ page }) => {
    let listed = true;
    await page.route("**/api/schedule", async (route) => {
      if (route.request().method() !== "GET") return route.fallback();
      const overview = await (await route.fetch()).json();
      if (listed) overview.rules = [...(overview.rules || []), { id: "rule-unreadable", status: "broken", message: "repeat: unknown frequency 'hourly'" }];
      return route.fulfill({ json: overview });
    });
    const deletes = [];
    await page.route("**/api/schedule/rules/rule-unreadable*", (route) => {
      deletes.push(route.request().url());
      listed = false;
      return route.fulfill({ json: { deleted: true } });
    });
    await openSignedIn(page, "#/schedule");
    const card = page.locator('[data-rule="rule-unreadable"]');
    await expect(card).toContainText("rule-unreadable");
    await expect(card).toContainText("Broken");
    await expect(card).toContainText("unknown frequency");
    await expect(card.getByRole("button")).toHaveText(["Delete"]);
    await card.getByRole("button", { name: "Delete" }).click();
    await page.locator("dialog[open]").getByTestId("confirm").click();
    await expect(card).toHaveCount(0);
    expect(deletes.map((url) => new URL(url).search)).toEqual([""]);
  });

  test("Pause all stops every rule until Resume", async ({ page, request }) => {
    await addRule(request, "Morning cap");
    await openSignedIn(page, "#/schedule");
    const [pause] = await Promise.all([
      page.waitForRequest((sent) => sent.method() === "PATCH" && sent.url().endsWith("/api/schedule")),
      page.getByRole("button", { name: "Pause all" }).click(),
    ]);
    expect(pause.postDataJSON()).toEqual({ enabled: false });
    await expect(page.getByText("Schedules paused")).toBeVisible();
    expect((await (await request.get("/api/schedule")).json()).enabled).toBe(false);
    await page.getByRole("button", { name: "Resume" }).click();
    await expect(page.getByText("Schedules on")).toBeVisible();
    expect((await (await request.get("/api/schedule")).json()).enabled).toBe(true);
  });
});

test.describe("Modern dashboard operator: maintenance @smoke", () => {
  test("the page names the config, the backup folder and the backup types, and lists every backup", async ({ page }) => {
    await openSignedIn(page, "#/maintenance");
    const status = page.getByRole("region", { name: "Maintenance status" });
    await expect(status).toContainText("config/config.json");
    await expect(status).toContainText("data/backups");
    await expect(status).toContainText("Config + state, Local SQLite databases, Analytics (InfluxDB)");
    await expect(status).toContainText("bundled");
    await expect(status).toContainText("available");
    const rows = page.locator("table.data tbody tr");
    await expect(rows).toHaveCount(3);
    await expect(rows.nth(0)).toContainText("ems-config-manual-2026-06-29-201500.tar.gz");
    await expect(rows.nth(0).locator("td").nth(2)).toHaveText(/\d{2}:\d{2}/);
    await expect(rows.nth(0)).toContainText("12 KiB");
    await expect(rows.nth(0)).toContainText("0.6.0");
    await expect(rows.nth(1)).toContainText("Local SQLite databases");
    await expect(rows.nth(1)).toContainText("375 KiB");
    await expect(rows.nth(2)).toContainText("encrypted");
    await expect(page.getByRole("region", { name: "Safety notes" }).locator("li")).toHaveCount(4);
  });

  test("with no backups the list says so and the restore has nothing to pick", async ({ page }) => {
    await page.route("**/api/maintenance/backups", (route) => route.fulfill({ json: { items: [] } }));
    await openSignedIn(page, "#/maintenance");
    await expect(page.getByText("No backups yet.")).toBeVisible();
    await expect(page.locator("table.data")).toHaveCount(0);
    const restoreCard = page.getByRole("region", { name: "Restore", exact: true });
    await expect(restoreCard.locator("select option")).toHaveText(["Select a backup…"]);
    await expect(restoreCard.getByRole("button", { name: "Preview restore" })).toBeDisabled();
  });

  test("each backup type is created by its own button, and the list is read again after each", async ({ page }) => {
    const created = [];
    await page.route("**/api/maintenance/backups/create", (route) => {
      const { type } = route.request().postDataJSON();
      created.push({ type, csrf: route.request().headers()["x-csrf-token"] });
      return route.fulfill({ json: { created: true, backup: { name: `ems-${type}-manual.tar.gz`, backup_type: type } } });
    });
    let listings = 0;
    page.on("request", (sent) => {
      if (sent.method() === "GET" && sent.url().endsWith("/api/maintenance/backups")) listings += 1;
    });
    await openSignedIn(page, "#/maintenance");
    await expect.poll(() => listings).toBe(1);
    const message = page.locator(".message[role=status]");
    for (const [label, type] of [["Config + state", "config"], ["Local SQLite databases", "databases"], ["Analytics (InfluxDB)", "influxdb"]]) {
      await page.getByRole("button", { name: `Back up ${label}` }).click();
      await expect(message).toHaveText(`Created ems-${type}-manual.tar.gz.`);
    }
    expect(created).toEqual(["config", "databases", "influxdb"].map((type) => ({ type, csrf: CSRF })));
    await expect.poll(() => listings).toBe(4);
  });

  test("a backup the EMS cannot start because another is running says so", async ({ page }) => {
    await page.route("**/api/maintenance/backups/create", (route) => route.fulfill({ status: 429, json: { error: "maintenance_busy" } }));
    await openSignedIn(page, "#/maintenance");
    await page.getByRole("button", { name: "Back up Config + state" }).click();
    await expect(page.locator(".message[role=status]")).toHaveText(/^Another backup, restore or config upgrade is still running/);
    await expect(page.getByRole("button", { name: "Back up Config + state" })).toBeEnabled();
  });

  test("a backup's details show its provenance and contents, and an encrypted one stays locked", async ({ page }) => {
    await openSignedIn(page, "#/maintenance");
    const rows = page.locator("table.data tbody tr");
    const [inspect] = await Promise.all([
      page.waitForRequest((sent) => sent.url().endsWith("/api/maintenance/backups/inspect")),
      rows.nth(0).getByRole("button", { name: "Details" }).click(),
    ]);
    expect(inspect.postDataJSON()).toEqual({ file: "ems-config-manual-2026-06-29-201500.tar.gz" });
    const detail = page.locator(".detail");
    await expect(detail).toContainText("Provenance of ems-config-manual-2026-06-29-201500.tar.gz");
    await expect(detail).toContainText("main @ b87add4324ea");
    await expect(detail).toContainText("Archive contents (3)");
    await expect(detail.locator(".files li")).toHaveCount(3);
    await expect(detail).toContainText("dashboard-auth.json");

    await rows.nth(1).getByRole("button", { name: "Details" }).click();
    await expect(detail).toContainText("Archive contents (2)");
    await expect(detail).toContainText("data/ems_dashboard.sqlite");
    await expect(detail).not.toContainText("config.json");

    await rows.nth(2).getByRole("button", { name: "Details" }).click();
    await expect(detail).toContainText("The details of an encrypted backup are locked.");
  });

  test("a restore is previewed, its plan dropped when the backup changes, and run only after its question", async ({ page }) => {
    await openSignedIn(page, "#/maintenance");
    const restoreCard = page.getByRole("region", { name: "Restore", exact: true });
    const pick = restoreCard.locator("select");
    const confirmButton = restoreCard.getByRole("button", { name: "Confirm restore" });
    const plan = page.getByTestId("restore-plan");
    await expect(restoreCard.getByRole("button", { name: "Preview restore" })).toBeDisabled();
    await expect(confirmButton).toBeDisabled();

    await pick.selectOption("ems-config-manual-2026-06-20-070000.tar.gz.enc");
    await restoreCard.locator('input[type="password"]').fill("backup secret");
    const [planned] = await Promise.all([
      page.waitForRequest((sent) => sent.url().endsWith("/api/maintenance/backups/restore-plan")),
      restoreCard.getByRole("button", { name: "Preview restore" }).click(),
    ]);
    expect(planned.postDataJSON()).toEqual({ file: "ems-config-manual-2026-06-20-070000.tar.gz.enc", password: "backup secret" });
    await expect(plan).toContainText("Preview for ems-config-manual-2026-06-20-070000.tar.gz.enc (Config + state)");
    await expect(plan).toContainText("would_replace_conflict");
    await expect(plan).toContainText("A restart is required after the restore.");
    await expect(page.locator(".message[role=status]")).toHaveText("Preview ready. Check the plan, then confirm.");
    await expect(confirmButton).toBeEnabled();

    await pick.selectOption("ems-config-manual-2026-06-29-201500.tar.gz");
    await expect(plan).toHaveCount(0);
    await expect(confirmButton).toBeDisabled();
    await pick.selectOption("ems-config-manual-2026-06-20-070000.tar.gz.enc");
    await restoreCard.getByRole("button", { name: "Preview restore" }).click();
    await expect(plan).toBeVisible();

    const dialog = page.locator("dialog[open]");
    await confirmButton.click();
    await expect(dialog).toContainText("Restore ems-config-manual-2026-06-20-070000.tar.gz.enc?");
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(plan).toBeVisible();

    await confirmButton.click();
    const [restored] = await Promise.all([
      page.waitForRequest((sent) => sent.url().endsWith("/api/maintenance/backups/restore")),
      dialog.getByTestId("confirm").click(),
    ]);
    expect(restored.postDataJSON()).toEqual({
      file: "ems-config-manual-2026-06-20-070000.tar.gz.enc",
      password: "backup secret",
      confirm_preview: true,
      confirm_restore: true,
      confirm_replace: true,
    });
    expect(restored.headers()["x-csrf-token"]).toBe(CSRF);
    const message = page.locator(".message[role=status]");
    await expect(message).toContainText("Restore completed (preview only — nothing was written).");
    await expect(message).toContainText("Restart the EMS. You may need to sign in again.");
    await expect(plan).toHaveCount(0);
    await expect(restoreCard.locator('input[type="password"]')).toHaveValue("");
    await expect(confirmButton).toBeDisabled();
  });

  test("the config upgrade names every planned change, and Check config reads it again", async ({ page }) => {
    let checks = 0;
    await page.route("**/api/maintenance/config-upgrade", (route) => {
      checks += 1;
      return checks === 1 ? route.continue() : route.fulfill({ json: { changed: false, apply_available: false } });
    });
    await openSignedIn(page, "#/maintenance");
    const upgradeCard = page.getByRole("region", { name: "Config upgrade" });
    await expect(upgradeCard).toContainText("4 config value change(s) planned.");
    await expect(upgradeCard.locator("h3")).toHaveText(["Added keys", "Migrated values", "New explanatory comments", "Comment refresh"]);
    await expect(upgradeCard.locator("li", { hasText: "config_upgrade.on_startup" })).toHaveText('config_upgrade.on_startup = "check"');
    await expect(upgradeCard.locator("li", { hasText: "config_schema_version" })).toHaveText("config_schema_version: 2 → 3");
    await expect(upgradeCard.locator("li", { hasText: "dashboard._comment" })).toHaveCount(1);
    await expect(upgradeCard).toContainText("21 explanatory comments can be refreshed from the template.");
    await expect(upgradeCard).toContainText("config.json will be rewritten in the template layout.");
    await expect(upgradeCard.getByRole("button", { name: "Apply with backup" })).toBeEnabled();

    await upgradeCard.getByRole("button", { name: "Check config" }).click();
    await expect(upgradeCard).toContainText("Config is up to date.");
    await expect(upgradeCard).not.toContainText("config_schema_version");
    await expect(upgradeCard.getByRole("button", { name: "Apply with backup" })).toBeDisabled();
    expect(checks).toBe(2);
  });

  test("the config upgrade is applied with a backup only after its question", async ({ page }) => {
    let applies = 0;
    page.on("request", (sent) => {
      if (sent.url().endsWith("/api/maintenance/config-upgrade/apply")) applies += 1;
    });
    await openSignedIn(page, "#/maintenance");
    const upgradeCard = page.getByRole("region", { name: "Config upgrade" });
    const dialog = page.locator("dialog[open]");
    await upgradeCard.getByRole("button", { name: "Apply with backup" }).click();
    await expect(dialog).toContainText("Apply the config changes shown? A backup is created first.");
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(dialog).toHaveCount(0);
    expect(applies).toBe(0);

    await upgradeCard.getByRole("button", { name: "Apply with backup" }).click();
    const [applied] = await Promise.all([
      page.waitForRequest((sent) => sent.url().endsWith("/api/maintenance/config-upgrade/apply")),
      dialog.getByTestId("confirm").click(),
    ]);
    expect(applied.postDataJSON()).toEqual({ refresh_comments: true, confirm_apply: true, plan_id: "preview-config-upgrade-plan" });
    expect(applied.headers()["x-csrf-token"]).toBe(CSRF);
    await expect(page.locator(".message[role=status]")).toContainText("Backup: ems-config-manual-2026-06-30-120000.tar.gz. Restart the EMS.");
  });

  test("an upgrade that only refreshes comments says so on its button", async ({ page }) => {
    await page.route("**/api/maintenance/config-upgrade", (route) =>
      route.fulfill({
        json: {
          changed: true, apply_available: true, plan_id: "comments-only", format_changed: false,
          add_count: 0, migration_count: 0, comment_add_count: 0, comment_refresh_count: 2,
          items: [{ kind: "comment_refresh", path: "system._comment" }],
        },
      }));
    await openSignedIn(page, "#/maintenance");
    const upgradeCard = page.getByRole("region", { name: "Config upgrade" });
    await expect(upgradeCard.getByRole("button", { name: "Refresh comments with backup" })).toBeEnabled();
    await expect(upgradeCard).toContainText("system._comment");
  });
});
