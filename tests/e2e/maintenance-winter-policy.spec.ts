import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { MaintenancePage } from "./pages/maintenance-page";

// Each inverter carries a winter policy: "auto" follows its device type, the
// others name a plan from the EMS policy registry. The field comes from the
// config catalog, so the editor offers exactly the registered plans.

test.describe("Maintenance winter policy", { tag: ["@maintenance"] }, () => {
  test.beforeEach(async ({ page, seedAdminScenario }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();
    await seedAdminScenario("mixed_transports");
    await page.reload();
  });

  test("an inverter offers the registered winter policies and keeps the choice as a draft", async ({
    page,
  }) => {
    const maintenance = new MaintenancePage(page);
    await maintenance.openEditor({ discovery: false });
    const card = page.locator('[data-source-id="maintenance-inverter-0"]');
    await card.locator(".hardware-card-summary").click();
    await card.locator("details.feature-advanced > summary").first().click();

    const select = card
      .locator("label")
      .filter({ has: page.locator(".feature-field-label", { hasText: "Winter policy" }) })
      .locator("select")
      .first();
    await expect(select).toBeVisible();
    const values = await select.locator("option").evaluateAll((options) =>
      options.map((option) => (option as HTMLOptionElement).value).filter(Boolean),
    );
    expect(values).toEqual(["auto", "solar_morning_step", "noon_step", "none"]);

    await select.selectOption("noon_step");
    await expect(select).toHaveValue("noon_step");
    await expect(page.locator("#maintenance-settings-count")).not.toHaveText(
      "No unsaved changes.",
    );
  });
});
