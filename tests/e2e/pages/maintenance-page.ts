import { type Page, type Locator, expect } from "@playwright/test";

// The Maintenance "Your system" panel. Five specs carried a private copy of the
// same opening sequence, so every change to the panel's shape was a five-file
// sweep that was easy to do in four places.
export class MaintenancePage {
  readonly configCardToggle: Locator;
  readonly editor: Locator;
  readonly discoverySources: Locator;
  readonly refreshButton: Locator;

  constructor(private readonly page: Page) {
    this.configCardToggle = page.locator(
      '[data-maintenance-toggle="maintenance-config-card"]',
    );
    this.editor = page.locator("#maintenance-config-editor");
    this.discoverySources = page.locator("#maintenance-discovery-sources");
    this.refreshButton = page.locator("#maintenance-refresh");
  }

  // Refresh reloads the whole panel; the config read is the last one that can
  // still redraw the editor, so waiting for it is what makes this deterministic.
  async refresh() {
    await Promise.all([
      this.page.waitForResponse(
        (response) =>
          new URL(response.url()).pathname === "/api/admin/maintenance/config" &&
          response.request().method() === "GET",
      ),
      this.refreshButton.click(),
    ]);
  }

  async openManualPanel() {
    await expect(this.page.locator("#view-start")).toBeVisible();
    await this.page.locator('[data-start-path="manage_existing"]').click();
    await this.page.locator('[data-open-maintenance-path="manual"]').click();
  }

  // The card and the add-devices disclosure both remember their state across a
  // re-render, so both are opened by "open it unless it already is".
  async openEditor(options: { discovery?: boolean } = {}) {
    await this.openManualPanel();
    await expect(this.configCardToggle).toContainText(/inverter/);
    await expect(async () => {
      if (!(await this.editor.isVisible())) await this.configCardToggle.click();
      await expect(this.editor).toBeVisible({ timeout: 1_000 });
    }).toPass();
    if (options.discovery !== false) await this.openAddDevices();
  }

  async openAddDevices() {
    await expect(async () => {
      if (!(await this.discoverySources.isVisible())) {
        await this.page.locator("#maintenance-add-devices > summary").click();
      }
      await expect(this.discoverySources).toBeVisible({ timeout: 1_000 });
    }).toPass();
  }
}
