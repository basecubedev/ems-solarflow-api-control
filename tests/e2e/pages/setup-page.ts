import { type Page, type Locator, expect } from "@playwright/test";

// Guided Setup Step 1 (System Build selection). Drives the real browser controls
// and reads the server-backed compatibility state; it holds no assertions of its
// own beyond the small wait helpers, so tests stay readable.
export class SetupPage {
  readonly buildSelect: Locator;
  readonly continueButton: Locator;
  readonly adminUpdateButton: Locator;
  readonly status: Locator;
  readonly embeddedCheck: Locator;
  readonly error: Locator;
  readonly progressTag: Locator;
  readonly progressRevision: Locator;
  readonly progressAdminImage: Locator;
  readonly progressEmsImage: Locator;
  private validationResponse: Record<string, any> | null = null;
  private validationResponses: Record<string, any>[] = [];

  constructor(private readonly page: Page) {
    this.buildSelect = page.getByTestId("system-build-select");
    this.continueButton = page.getByTestId("continue-button");
    this.adminUpdateButton = page.getByTestId("admin-update-button");
    this.status = page.getByTestId("system-build-status");
    this.embeddedCheck = page.getByTestId("embedded-resources-check");
    this.error = page.locator("#setup-system-build-error");
    this.progressTag = page.locator("#system-alignment-tag");
    this.progressRevision = page.locator("#system-alignment-revision");
    this.progressAdminImage = page.locator("#system-alignment-admin-image");
    this.progressEmsImage = page.locator("#system-alignment-ems-image");
    page.on("response", async (response) => {
      if (
        response.url().includes("/api/admin/system-alignment/validate") &&
        response.request().method() === "POST"
      ) {
        const body = await response.json().catch(() => null);
        if (body && typeof body === "object") {
          this.validationResponse = body;
          this.validationResponses.push(body);
        }
      }
    });
  }

  async chooseFreshInstall() {
    await this.page.getByTestId("start-fresh-install").click();
    await expect(this.buildSelect).toBeVisible();
  }

  async previewBuild(tag: string) {
    // Selecting a build is side-effect free: it shows the local catalogue preview
    // and the single explicit "Verify System Build" primary. No validation runs.
    await this.buildSelect.selectOption(tag);
    await expect(this.continueButton).toHaveText(/Verify System Build/i);
  }

  async verifyBuild() {
    // The explicit verification: click Verify System Build and wait for the one
    // validation response it triggers.
    await expect(this.continueButton).toHaveText(/Verify System Build/i);
    await expect(this.continueButton).toBeEnabled();
    const validated = this.page.waitForResponse(
      (r) =>
        r.url().includes("/api/admin/system-alignment/validate") &&
        r.request().method() === "POST",
    );
    await this.continueButton.click();
    const response = await validated;
    this.validationResponse = await response.json();
    // Let the render settle after the fetch resolves.
    await expect(this.status).not.toHaveText(/Downloading and verifying/i);
  }

  async selectBuild(tag: string) {
    // Convenience for tests that need the verified verdict: select, then verify.
    await this.previewBuild(tag);
    await this.verifyBuild();
  }

  async selectBuildByLabel(label: RegExp) {
    const option = this.buildSelect.locator("option").filter({ hasText: label });
    await expect(option).toHaveCount(1);
    await this.selectBuild(await option.getAttribute("value") as string);
  }

  developmentOptions(): Locator {
    return this.buildSelect.locator('option[data-channel="development"]');
  }

  async selectDevelopmentBuild(tag: string) {
    await expect(
      this.buildSelect.locator(
        `option[value="${tag}"][data-channel="development"]`,
      ),
    ).toHaveCount(1);
    await this.selectBuild(tag);
  }

  async latestValidation(): Promise<Record<string, any>> {
    const selected = await this.buildSelect.inputValue();
    const matching = [...this.validationResponses]
      .reverse()
      .find(
        (entry) =>
          entry.action_state?.selected_build?.tag === selected ||
          entry.system_build?.canonical_tag === selected,
      );
    const response = matching || this.validationResponse;
    expect(response, "a System Build validation response is required").not.toBeNull();
    return response as Record<string, any>;
  }

  validationHistory(): Record<string, any>[] {
    return [...this.validationResponses];
  }

  resetValidationHistory() {
    this.validationResponse = null;
    this.validationResponses = [];
  }

  devicesTab(): Locator {
    return this.page.locator('[data-setup-step="devices"]');
  }

  progressStage(key: string): Locator {
    return this.page.locator(`[data-system-alignment-stage="${key}"]`);
  }

  async continueToDevices() {
    await this.continueButton.click();
    await expect(this.devicesTab()).toHaveAttribute("aria-selected", "true");
  }
}
