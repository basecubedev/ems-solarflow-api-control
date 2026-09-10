import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";

// Guided Setup buries the write gates: they are level="advanced" on purpose, so
// they landed inside an "Advanced settings" disclosure, inside the "System
// basics" row, inside the "Advanced / System settings" card. Somebody
// installing this for the first time never saw that EMS was about to write to
// their inverters.

test.describe("Guided Setup: control and safety", { tag: ["@setup"] }, () => {
  test("the write gates are visible on the config step without opening anything", async ({
    page,
  }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await setup.selectBuild("latest");
    await expect(setup.continueButton).toBeEnabled();
    await setup.continueToDevices();
    await page.locator('[data-setup-step="config"]').click();

    const block = page.locator('[data-setup-group="safety"]');
    await expect(block).toBeVisible();
    for (const path of [
      "system.allow_hardware_writes",
      "system.allow_mqtt_local_control_writes",
      "system.allow_mqtt_zendure_control_writes",
      "system.max_total_power",
    ]) {
      await expect(
        block.locator('[data-feature-path="' + path + '"]'),
      ).toBeVisible();
    }
    // On by default, so a finished setup actually controls the system.
    await expect(
      block.locator('[data-feature-path="system.allow_hardware_writes"]'),
    ).toBeChecked();
    // The two polarities are named, not mixed into one list of checkboxes.
    await expect(block).toContainText("What EMS may change");
    await expect(block).toContainText("Hold EMS back");
  });

  test("a safety setting is not also offered inside the advanced card", async ({
    page,
  }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await setup.selectBuild("latest");
    await setup.continueToDevices();
    await page.locator('[data-setup-step="config"]').click();
    await expect(page.locator('[data-setup-group="safety"]')).toBeVisible();
    await expect(
      page.locator('[data-feature-path="system.allow_hardware_writes"]'),
    ).toHaveCount(1);
  });
});
