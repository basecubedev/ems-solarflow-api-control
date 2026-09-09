import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { MaintenancePage } from "./pages/maintenance-page";

// Maintenance is two pages now: a status page that reads and repairs, and a
// settings page that edits a draft. The split is only safe if the old address
// still lands somewhere real, if the tabs never cost an unsaved edit, and if
// the write gates are visible without hunting through a disclosure.

test.describe("Maintenance: two doors", { tag: ["@maintenance"] }, () => {
  test.beforeEach(async ({ page, seedAdminScenario }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();
    await seedAdminScenario("mixed_transports");
    await page.reload();
  });

  test("the published #maintenance-manual address still opens the status page", async ({
    page,
  }) => {
    // The start gate owns the first screen, so a hash routes once the workspace
    // is revealed — the same for #maintenance-manual as for #maintenance-status.
    const maintenance = new MaintenancePage(page);
    await maintenance.openSettings();
    await page.evaluate(() => {
      window.location.hash = "maintenance-manual";
    });
    await expect(page.locator("#maintenance-status-panel")).toBeVisible();
    await expect(page.locator("#maintenance-settings-panel")).toBeHidden();
    await expect(page.locator("#maintenance-control-state")).toBeVisible();
    // The address is kept as it was bookmarked, not rewritten underneath.
    expect(new URL(page.url()).hash).toBe("#maintenance-manual");
  });

  test("the status page hands the owner over to the tab that can change it", async ({
    page,
  }) => {
    const maintenance = new MaintenancePage(page);
    await maintenance.openStatus();
    await page
      .locator('[data-open-maintenance-path="settings-safety"]')
      .click();
    await expect(page.locator("#maintenance-settings-panel")).toBeVisible();
    await expect(page.locator('[data-settings-pane="safety"]')).toBeVisible();
    await expect(page.locator('[data-settings-tab="safety"]')).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  test("the write gates are on the safety tab without opening a disclosure", async ({
    page,
  }) => {
    const maintenance = new MaintenancePage(page);
    await maintenance.openSettings("safety");
    const pane = page.locator('[data-settings-pane="safety"]');
    for (const path of [
      "system.allow_hardware_writes",
      "system.allow_mqtt_local_control_writes",
      "system.allow_mqtt_zendure_control_writes",
      "system.allow_state_reconciliation_writes",
      "system.dry_run",
      "system.simulation_mode",
      "system.max_total_power",
    ]) {
      await expect(pane.locator(`[data-path="${path}"]`)).toBeVisible();
    }
    // No <details> stands between the operator and a gate.
    await expect(pane.locator("details")).toHaveCount(0);
  });

  test("a gate is rendered on the safety tab and nowhere else", async ({ page }) => {
    const maintenance = new MaintenancePage(page);
    await maintenance.openSettings();
    await expect(
      page.locator('[data-path="system.allow_hardware_writes"]'),
    ).toHaveCount(1);
  });

  test("switching tabs keeps an unsaved edit", async ({ page }) => {
    const maintenance = new MaintenancePage(page);
    await maintenance.openSettings();
    const card = page.locator('[data-source-id="maintenance-inverter-0"]');
    await card.locator(".hardware-card-summary").click();
    const serial = card
      .locator("label")
      .filter({
        has: page.locator(".feature-field-label", { hasText: "Serial number" }),
      })
      .locator('input[type="text"]')
      .first();
    await serial.fill("EDITED-ACROSS-TABS");
    await expect(page.locator("#maintenance-settings-count")).toContainText(
      /unsaved change/,
    );

    await maintenance.openTab("safety");
    await maintenance.openTab("expert");
    await maintenance.openTab("devices");

    await expect(serial).toHaveValue("EDITED-ACROSS-TABS");
  });

  test("the search finds a setting by its config path", async ({ page }) => {
    const maintenance = new MaintenancePage(page);
    await maintenance.openSettings("expert");
    const pane = page.locator('[data-settings-pane="expert"]');
    const row = pane.locator('[data-path="system.loop_interval"]');
    await page.locator("#maintenance-settings-search").fill("loop_interval");
    await expect(row).toBeVisible();
    await expect(
      pane.locator('[data-path="system.log_level"]'),
    ).toBeHidden();
    await expect(page.locator("#maintenance-settings-search-count")).toContainText(
      /setting/,
    );
  });
});
