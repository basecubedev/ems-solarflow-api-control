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
    await this.waitForStatusSettled();
    await Promise.all([
      this.page.waitForResponse(
        (response) =>
          new URL(response.url()).pathname === "/api/admin/maintenance/config" &&
          response.request().method() === "GET",
      ),
      this.refreshButton.click(),
    ]);
  }

  // A reload keeps the address, and a maintenance address now opens its page
  // directly, so the start gate is no longer guaranteed to be the first screen.
  // Public because any spec that reloads while a maintenance page is open needs
  // it; a private copy per spec is how the assumption spread in the first place.
  async enterMaintenance() {
    const gate = this.page.locator("#view-start");
    const workspace = this.page.locator("#view-maintenance");
    // Both exist in the document at all times, so wait for whichever is shown
    // rather than for one of them to appear.
    await expect
      .poll(async () => (await gate.isVisible()) || (await workspace.isVisible()))
      .toBe(true);
    if (await gate.isVisible()) {
      await this.page.locator('[data-start-path="manage_existing"]').click();
    }
  }

  async openStatus() {
    await this.enterMaintenance();
    await this.goTo("status");
    await this.waitForStatusSettled();
  }

  /**
   * Wait until the status page is shown and has stopped moving.
   *
   * Its sections appear and grow while their reads return, so a press made
   * then can start on one control and end on another, and neither acts. The
   * page is shown in the same task that marks it busy, so once it is visible
   * its busy state is the current load's, not one left by an earlier visit.
   */
  async waitForStatusSettled() {
    const panel = this.page.locator("#maintenance-status-panel");
    await expect(panel).toBeVisible();
    await expect(panel).toHaveAttribute("aria-busy", "false");
  }

  /**
   * Expand one status-page card once the page has settled. The body check
   * reports a lost press here rather than at a later click on a control that
   * was never shown.
   */
  async openStatusCard(id: string) {
    await this.waitForStatusSettled();
    const body = this.page.locator("#" + id + "-body");
    if (!(await body.isVisible())) {
      await this.page.locator('[data-maintenance-toggle="' + id + '"]').click();
    }
    await expect(body).toBeVisible();
  }

  async openUpgrade() {
    await this.enterMaintenance();
    await this.goTo("upgrade");
  }

  async openSettings(tab?: string) {
    await this.enterMaintenance();
    await this.goTo("settings");
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
