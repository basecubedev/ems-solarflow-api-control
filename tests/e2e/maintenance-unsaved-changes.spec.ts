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

// The settings page has no Refresh of its own — "Discard my changes" is the way
// back to the saved state. The reload that could still clobber a draft is the
// status page's Refresh, which reloads the config for the Control & safety
// stage and re-renders the editor in the hidden settings panel.
async function refreshFromTheStatusPage(page: Page) {
  const maintenance = new MaintenancePage(page);
  await maintenance.goTo("status");
  await maintenance.refresh();
  await maintenance.goTo("settings");
}

test.describe("Maintenance settings draft", { tag: ["@maintenance"] }, () => {
  test.beforeEach(async ({ page, seedAdminScenario }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();
    await seedAdminScenario("mixed_transports");
    await page.reload();
  });

  test("a refresh on the status page keeps an unsaved edit and its open card", async ({
    page,
  }) => {
    const { card } = await openInverterCard(page);
    const serial = cardInput(page, card, "Serial number");
    await expect(serial).toHaveValue("API-SERIAL");
    await serial.fill("EDITED-NOT-SAVED");

    await refreshFromTheStatusPage(page);

    await expect(serial).toHaveValue("EDITED-NOT-SAVED");
    await expect(serial).toBeVisible();
    await expect(page.locator("#maintenance-config-message")).toContainText(
      /unsaved changes were kept/,
    );
  });

  test("a refresh reloads the saved settings once nothing is unsaved", async ({
    page,
  }) => {
    const { card } = await openInverterCard(page);
    const serial = cardInput(page, card, "Serial number");
    await serial.fill("EDITED-NOT-SAVED");
    await page.locator("#maintenance-config-reset-btn").click();
    await expect(cardInput(page, card, "Serial number")).toHaveValue(
      "API-SERIAL",
    );

    await refreshFromTheStatusPage(page);

    await expect(page.locator("#maintenance-config-message")).toHaveText("");
    await expect(page.locator("#maintenance-settings-count")).toHaveText(
      "No unsaved changes.",
    );
  });
});
