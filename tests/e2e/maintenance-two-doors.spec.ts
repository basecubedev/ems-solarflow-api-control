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

  test("the hub reports the state of each door", async ({ page }) => {
    await page.locator('[data-start-path="manage_existing"]').click();
    await expect(page.locator("#maintenance-hub")).toBeVisible();
    // Read from the live overview, not from a placeholder.
    await expect(page.locator("#maintenance-hub-verdict")).not.toHaveText(
      /Checking this installation/,
    );
    await expect(page.locator("#maintenance-hub-status-state")).not.toHaveText("…");
    await expect(page.locator("#maintenance-hub-backup-state")).not.toHaveText("…");
    // Nothing was edited yet, so the settings card has nothing to report.
    await expect(page.locator("#maintenance-hub-settings-state")).toBeHidden();
  });

  test("an unsaved edit follows the owner back to the hub", async ({ page }) => {
    const maintenance = new MaintenancePage(page);
    await maintenance.openSettings();
    const card = page.locator('[data-source-id="maintenance-inverter-0"]');
    await card.locator(".hardware-card-summary").click();
    await card
      .locator("label")
      .filter({
        has: page.locator(".feature-field-label", { hasText: "Serial number" }),
      })
      .locator('input[type="text"]')
      .first()
      .fill("EDITED-NOT-SAVED");

    await page.locator("#maintenance-back-settings").click();

    await expect(page.locator("#maintenance-hub")).toBeVisible();
    await expect(page.locator("#maintenance-hub-settings-state")).toBeVisible();
    await expect(page.locator("#maintenance-hub-settings-state")).toHaveText(
      /unsaved/,
    );
  });

  test("the review separates what is live at once from what waits", async ({
    page,
  }) => {
    const maintenance = new MaintenancePage(page);
    await maintenance.openSettings("safety");
    // One change of each kind: a mirrored limit and a write gate.
    const ceiling = page
      .locator('[data-path="system.max_total_power"]')
      .locator("input")
      .first();
    await ceiling.fill("1400");
    // The gate is absent from this config, so it renders at its catalog
    // default (on). Turning it off is the change that needs a restart.
    const gate = page
      .locator('[data-path="system.allow_hardware_writes"]')
      .locator("input")
      .first();
    await expect(gate).toBeChecked();
    await expect(
      page.locator('[data-path="system.allow_hardware_writes"]'),
    ).toHaveAttribute("data-from-default", "true");
    await gate.uncheck();

    await page.locator("#maintenance-config-preview-btn").click();
    await expect(page.locator("#maintenance-config-validation")).toHaveText("valid");

    const live = page.locator('.mconfig-diff-group[data-when="live"]');
    const restart = page.locator('.mconfig-diff-group[data-when="restart"]');
    await expect(live).toContainText("system.max_total_power");
    await expect(restart).toContainText("system.allow_hardware_writes");
    // The split is a grouping, not a filter: every row the server returned is
    // rendered, and the two groups account for all of them.
    const rendered = await page.locator(".mconfig-change").count();
    const grouped =
      (await live.locator(".mconfig-change").count()) +
      (await restart.locator(".mconfig-change").count());
    expect(grouped).toBe(rendered);
    await expect(page.locator("#maintenance-config-change-summary")).toContainText(
      /immediate/,
    );
    await expect(page.locator("#maintenance-config-change-summary")).toContainText(
      /need a restart/,
    );
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
