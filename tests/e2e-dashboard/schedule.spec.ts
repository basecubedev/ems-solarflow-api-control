// SPDX-License-Identifier: AGPL-3.0-or-later
// The Schedule tab against the real schedule routes: the preview server mounts
// them over a scratch copy of the scenario's runtime state. What is checked
// here cannot be read off the files -- that a draft is judged by the EMS while
// it is typed, that Save follows that judgement rather than the page's own,
// and that a saved rule comes back as the sentence the EMS reads it as.
//
// Never `waitUntil: "networkidle"` here: /api/events is an open SSE stream.
import { expect, test } from "@playwright/test";

const CSRF = { "X-CSRF-Token": "preview-csrf-token" };

async function clearRules(page) {
  const overview = await (await page.request.get("/api/schedule")).json();
  for (const rule of overview.rules || []) {
    await page.request.delete(`/api/schedule/rules/${rule.id}?revision=${rule.revision ?? ""}`, { headers: CSRF });
  }
  await page.request.patch("/api/schedule", { headers: CSRF, data: { enabled: true } });
}

// The default preview scenario has no dashboard password; the signed-in
// answer is the one the preview's own write-mode scenario gives.
const SIGNED_IN = {
  auth_configured: true,
  authenticated: true,
  csrf_token: "preview-csrf-token",
  write_mode_available: true,
  write_mode_active: true,
};

async function openSchedule(page) {
  await page.route("**/api/auth/status", (route) => route.fulfill({ json: SIGNED_IN }));
  await Promise.all([
    page.waitForResponse((response) => response.url().endsWith("/api/schedule") && response.request().method() === "GET"),
    page.goto("/preview/schedule", { waitUntil: "domcontentloaded" }),
  ]);
  await expect(page.locator(".schedule-summary")).toContainText("Schedules on");
}

function form(page) {
  return page.locator("#scheduleForm");
}

test.describe("schedule tab", () => {
  test.beforeEach(async ({ page }) => {
    await clearRules(page);
  });

  test("a window on weekdays is judged by the EMS while it is typed and then saved", async ({ page }) => {
    await openSchedule(page);
    await page.click('[data-schedule-action="new"]');

    await form(page).locator('input[name="name"]').fill("Evening socket");
    await form(page).locator('select[name="action"]').selectOption("offgrid_socket");
    await form(page).locator('select[name="value"]').selectOption("standard");
    await form(page).locator('input[name="from"]').fill("18:00");
    await form(page).locator('input[name="to"]').fill("23:00");
    await form(page).locator('select[name="end_value"]').selectOption("off");
    await form(page).locator('input[name="preset"][value="weekdays"]').check();

    const preview = page.locator("#schedulePreview");
    await expect(preview).toContainText("Every weekday, 18:00–23:00");
    await expect(preview).toContainText("WR1 · Off-grid socket standard, then off");
    await expect(preview).toContainText("emsctl device WR1 offgrid standard");
    const save = page.locator("#scheduleSave");
    await expect(save).toBeEnabled();

    await Promise.all([
      page.waitForResponse((response) => response.url().endsWith("/api/schedule/rules") && response.status() === 201),
      save.click(),
    ]);

    const rule = page.locator(".schedule-rule").filter({ hasText: "Evening socket" });
    await expect(rule).toContainText("Every weekday, 18:00–23:00");
    await expect(rule).toContainText("Waiting");
    await expect(page.locator("#scheduleStatus")).toContainText("Saved “Evening socket”.");
    await expect(page.locator("#scheduleForm")).toHaveCount(0);
  });

  test("a value the installation does not take keeps Save off and says why", async ({ page }) => {
    await openSchedule(page);
    await page.click('[data-schedule-action="new"]');

    await form(page).locator('input[name="name"]').fill("Too much");
    await form(page).locator('select[name="action"]').selectOption("device_output_limit");
    await form(page).locator('input[name="value"]').fill("900");
    await form(page).locator('input[name="end_value"]').fill("0");

    const preview = page.locator("#schedulePreview");
    await expect(preview).toContainText("Cannot be saved");
    await expect(preview).toContainText("max_power must be between 0 and 800");
    await expect(page.locator("#scheduleSave")).toBeDisabled();

    await form(page).locator('input[name="value"]').fill("400");
    await expect(preview).toContainText("WR1 · Device output limit 400 W, then 0 W");
    await expect(page.locator("#scheduleSave")).toBeEnabled();
  });

  test("a schedule that cannot be loaded says so and can be tried again", async ({ page }) => {
    await page.route("**/api/auth/status", (route) => route.fulfill({ json: SIGNED_IN }));
    let broken = true;
    await page.route("**/api/schedule", (route) => (broken
      ? route.fulfill({ status: 503, json: { error: "schedule_store_unreadable", message: "cannot read schedules" } })
      : route.continue()));
    await page.goto("/preview/schedule", { waitUntil: "domcontentloaded" });

    await expect(page.locator("#scheduleBody")).toContainText("The schedule could not be loaded (503): cannot read schedules");
    broken = false;
    await page.click('[data-schedule-action="reload"]');

    await expect(page.locator(".schedule-summary")).toContainText("Schedules on");
  });

  test("a rule is skipped, switched off and deleted from its card", async ({ page }) => {
    const tomorrow = new Date(Date.now() + 86_400_000).toISOString().slice(0, 10);
    const created = await page.request.post("/api/schedule/rules", {
      headers: CSRF,
      data: {
        rule: {
          name: "Night limit",
          target: { scope: "system" },
          action: { id: "total_output_limit", value: 400, end_value: 800 },
          when: { start: `${tomorrow}T22:00`, end: `${tomorrow}T23:30`, repeat: { freq: "daily" } },
        },
      },
    });
    expect(created.status()).toBe(201);
    await openSchedule(page);
    const card = page.locator(".schedule-rule").filter({ hasText: "Night limit" });
    await expect(card).toContainText("Every day, 22:00–23:30");

    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/skip")),
      card.locator('[data-schedule-action="skip"]').click(),
    ]);
    await expect(card).toContainText("skipping 1 date");

    await card.locator('[data-schedule-action="toggle"]').click();
    await expect(card).toContainText("Paused");

    page.once("dialog", (dialog) => dialog.accept());
    await card.locator('[data-schedule-action="delete"]').click();
    await expect(page.locator(".schedule-rule")).toHaveCount(0);
    await expect(page.locator("#scheduleBody")).toContainText("No rules yet.");
  });
});

test.describe("schedule tab, a rule made with emsctl", () => {
  test.beforeEach(async ({ page }) => {
    await clearRules(page);
  });

  test("a repeat beyond the editor is kept exactly when the rule is renamed", async ({ page }) => {
    const nextMonth = new Date(Date.now() + 40 * 86_400_000).toISOString().slice(0, 8) + "01";
    const repeat = { freq: "monthly", by_month_day: [1, 15] };
    const created = await page.request.post("/api/schedule/rules", {
      headers: CSRF,
      data: {
        rule: {
          name: "Twice a month",
          target: { scope: "system" },
          action: { id: "total_output_limit", value: 600 },
          when: { start: `${nextMonth}T06:00`, repeat },
        },
      },
    });
    expect(created.status()).toBe(201);
    await openSchedule(page);

    const card = page.locator(".schedule-rule").filter({ hasText: "Twice a month" });
    await card.locator('[data-schedule-action="edit"]').click();
    await expect(form(page).locator('input[name="preset"][value="kept"]')).toBeChecked();
    await form(page).locator('input[name="name"]').fill("Twice a month, renamed");
    await expect(page.locator("#scheduleSave")).toBeEnabled();
    await Promise.all([
      page.waitForResponse((response) => response.url().includes("/api/schedule/rules/") && response.request().method() === "PUT"),
      page.locator("#scheduleSave").click(),
    ]);

    const saved = await (await page.request.get("/api/schedule")).json();
    expect(saved.rules[0].name).toBe("Twice a month, renamed");
    expect(saved.rules[0].when.repeat.by_month_day).toEqual([1, 15]);
  });
});

test.describe("schedule tab, a viewer in another time zone", () => {
  test.use({ timezoneId: "America/New_York" });

  test.beforeEach(async ({ page }) => {
    await clearRules(page);
  });

  test("dates and times are the EMS's, and a window over the weekend survives an edit", async ({ page }) => {
    const friday = new Date();
    friday.setUTCDate(friday.getUTCDate() + ((5 - friday.getUTCDay() + 7) % 7 || 7));
    const start = friday.toISOString().slice(0, 10);
    const monday = new Date(friday.getTime() + 3 * 86_400_000).toISOString().slice(0, 10);
    const created = await page.request.post("/api/schedule/rules", {
      headers: CSRF,
      data: {
        rule: {
          name: "Weekend off",
          target: { scope: "device", device: "WR1" },
          action: { id: "device_output_limit", value: 0, end_value: 800 },
          when: { start: `${start}T00:30`, end: `${monday}T06:00`, repeat: { freq: "weekly" } },
        },
      },
    });
    expect(created.status()).toBe(201);
    const rule = await created.json();
    await openSchedule(page);

    const card = page.locator(".schedule-rule").filter({ hasText: "Weekend off" });
    await expect(card).toContainText("Next: Fri");
    await expect(card).toContainText("00:30 → 0 W");

    await card.locator('[data-schedule-action="edit"]').click();
    await form(page).locator('input[name="name"]').fill("Weekend off, renamed");
    await expect(page.locator("#scheduleSave")).toBeEnabled();
    await Promise.all([
      page.waitForResponse((response) => response.url().includes(`/api/schedule/rules/${rule.id}`)),
      page.locator("#scheduleSave").click(),
    ]);

    const saved = await (await page.request.get("/api/schedule")).json();
    expect(saved.rules[0].name).toBe("Weekend off, renamed");
    expect(saved.rules[0].when.end).toBe(`${monday}T06:00`);
  });

  test("a one-time night window edited into an evening one ends the same evening", async ({ page }) => {
    const day = new Date(Date.now() + 2 * 86_400_000).toISOString().slice(0, 10);
    const next = new Date(Date.now() + 3 * 86_400_000).toISOString().slice(0, 10);
    const created = await page.request.post("/api/schedule/rules", {
      headers: CSRF,
      data: {
        rule: {
          name: "Night",
          target: { scope: "device", device: "WR1" },
          action: { id: "offgrid_socket", value: "standard", end_value: "off" },
          when: { start: `${day}T22:00`, end: `${next}T02:00` },
        },
      },
    });
    expect(created.status()).toBe(201);
    const rule = await created.json();
    await openSchedule(page);

    await page.locator(".schedule-rule").filter({ hasText: "Night" }).locator('[data-schedule-action="edit"]').click();
    await expect(form(page).locator('select[name="action"] optgroup[label="Off-grid socket"]')).toHaveCount(1);
    await form(page).locator('input[name="from"]').fill("20:00");
    await form(page).locator('input[name="to"]').fill("23:00");
    await expect(page.locator("#schedulePreview")).toContainText("20:00–23:00");
    await Promise.all([
      page.waitForResponse((response) => response.url().includes(`/api/schedule/rules/${rule.id}`)),
      page.locator("#scheduleSave").click(),
    ]);

    const saved = await (await page.request.get("/api/schedule")).json();
    expect(saved.rules[0].when).toMatchObject({ start: `${day}T20:00`, end: `${day}T23:00` });
  });

  test("a rule on a device no longer offered keeps it in the editor instead of moving to another", async ({ page }) => {
    const created = await page.request.post("/api/schedule/rules", {
      headers: CSRF,
      data: {
        rule: {
          name: "Old inverter",
          enabled: false,
          target: { scope: "device", device: "Gone" },
          action: { id: "offgrid_socket", value: "standard" },
          when: { start: "2027-01-01T06:00", repeat: { freq: "daily" } },
        },
      },
    });
    expect(created.status()).toBe(201);
    await openSchedule(page);

    await page.locator(".schedule-rule").filter({ hasText: "Old inverter" }).locator('[data-schedule-action="edit"]').click();
    await form(page).locator('input[name="name"]').fill("Old inverter, renamed");

    await expect(form(page).locator('select[name="device"]')).toHaveValue("Gone");
    await expect(form(page).locator('select[name="device"] option[value="Gone"]')).toHaveText("Gone (not available)");
  });
});
