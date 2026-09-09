import { type Page, type Locator, expect } from "@playwright/test";

// The Maintenance "Your system" panel. Five specs carried a private copy of the
// same opening sequence, so every change to the panel's shape was a five-file
// sweep that was easy to do in four places.
export class MaintenancePage {
  readonly configSummary: Locator;
  readonly editor: Locator;
  readonly discoverySources: Locator;
  readonly refreshButton: Locator;

  constructor(private readonly page: Page) {
    this.configSummary = page.locator("#maintenance-config-summary");
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

  async openStatus() {
    await expect(this.page.locator("#view-start")).toBeVisible();
    await this.page.locator('[data-start-path="manage_existing"]').click();
    await this.page.locator('[data-open-maintenance-path="status"]').click();
    await expect(this.page.locator("#maintenance-status-panel")).toBeVisible();
  }

  async openSettings(tab?: string) {
    await expect(this.page.locator("#view-start")).toBeVisible();
    await this.page.locator('[data-start-path="manage_existing"]').click();
    await this.page.locator('[data-open-maintenance-path="settings"]').click();
    await expect(this.page.locator("#maintenance-settings-panel")).toBeVisible();
    // The editor appears only once the config load has rendered it; waiting for
    // the summary means a later render cannot replace a node under a test.
    await expect(this.editor).toBeVisible();
    await expect(this.configSummary).toContainText(/inverter/);
    if (tab) await this.openTab(tab);
  }

  // Navigate between maintenance pages the way a deep link does, from wherever
  // the test currently is (the hub cards are hidden while a page is open).
  async goTo(path: string) {
    await this.page.evaluate((hash) => {
      window.location.hash = hash;
    }, "maintenance-" + path);
    await expect(
      this.page.locator("#maintenance-" + path.split("-")[0] + "-panel"),
    ).toBeVisible();
  }

  async openTab(tab: string) {
    await this.page.locator('[data-settings-tab="' + tab + '"]').click();
    await expect(
      this.page.locator('[data-settings-pane="' + tab + '"]'),
    ).toBeVisible();
  }

  // The settings page is the editor now, so the card no longer collapses. The
  // add-devices disclosure still remembers its state across a re-render.
  async openEditor(options: { discovery?: boolean } = {}) {
    await this.openSettings();
    await expect(this.configSummary).toContainText(/inverter/);
    await expect(this.editor).toBeVisible();
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
