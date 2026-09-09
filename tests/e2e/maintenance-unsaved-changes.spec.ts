import { type Page } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { MaintenancePage } from "./pages/maintenance-page";

// The settings editor is a draft: nothing reaches config.json until the operator
// reviews and applies it. Everything that reloads the panel therefore has to
// leave that draft alone — a reload that silently replaces it destroys work the
// operator can neither see going nor get back.

function cardInput(page: Page, card: ReturnType<Page["locator"]>, label: string) {
  return card
    .locator("label")
    .filter({ has: page.locator(".feature-field-label", { hasText: label }) })
    .locator('input[type="text"]')
    .first();
}

async function openInverterCard(page: Page) {
  const maintenance = new MaintenancePage(page);
  await maintenance.openEditor({ discovery: false });
  const card = page.locator('[data-source-id="maintenance-inverter-0"]');
  await card.locator(".hardware-card-summary").click();
  return { maintenance, card };
}

test.describe("Maintenance settings draft", { tag: ["@maintenance"] }, () => {
  test.beforeEach(async ({ page, seedAdminScenario }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();
    await seedAdminScenario("mixed_transports");
    await page.reload();
  });

  test("Refresh keeps an unsaved edit and the card it was made in", async ({
    page,
  }) => {
    const { maintenance, card } = await openInverterCard(page);
    const serial = cardInput(page, card, "Serial number");
    await expect(serial).toHaveValue("API-SERIAL");
    await serial.fill("EDITED-NOT-SAVED");

    await maintenance.refresh();

    await expect(serial).toHaveValue("EDITED-NOT-SAVED");
    await expect(serial).toBeVisible();
  });

  test("Refresh reloads the saved settings once nothing is unsaved", async ({
    page,
  }) => {
    const { maintenance, card } = await openInverterCard(page);
    const serial = cardInput(page, card, "Serial number");
    await serial.fill("EDITED-NOT-SAVED");
    await page.locator("#maintenance-config-reset-btn").click();
    await expect(cardInput(page, card, "Serial number")).toHaveValue(
      "API-SERIAL",
    );

    await maintenance.refresh();

    await expect(page.locator("#maintenance-config-message")).toHaveText("");
  });
});
