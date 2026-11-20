// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import { type Page, type Locator, expect } from "@playwright/test";

const CHANNEL_GROUPS: Record<string, string> = { stable: "Stable" };
export const ADMIN_UPDATE_LABEL = "Update the Admin console";
export const READY_MESSAGE = "The Admin console is ready for this build.";

/**
 * The guided setup's first step, the System Build: drives the console's own
 * controls and records the server's verification answers. It holds no
 * assertions of its own beyond the small waits, so tests stay readable.
 */
export class SetupPage {
  readonly buildSelect: Locator;
  readonly continueButton: Locator;
  readonly adminUpdateButton: Locator;
  readonly status: Locator;
  readonly error: Locator;
  private validationResponse: Record<string, any> | null = null;
  private validationResponses: Record<string, any>[] = [];

  constructor(private readonly page: Page) {
    this.buildSelect = page.getByTestId("setup-release-select");
    this.continueButton = page.getByTestId("setup-right");
    this.adminUpdateButton = page.getByTestId("setup-left").filter({ hasText: ADMIN_UPDATE_LABEL });
    this.status = page.getByTestId("setup-build-message");
    this.error = page.getByTestId("setup-release").locator(".form-error");
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
    await this.page.getByTestId("setup-begin").click();
    await expect(this.buildSelect).toBeVisible();
  }

  /** Choose a build and verify it: the one validation request the verification sends. */
  async selectBuild(tag: string) {
    await this.buildSelect.selectOption(tag);
    await expect(this.continueButton).toBeEnabled();
    const validated = this.page.waitForResponse(
      (r) =>
        r.url().includes("/api/admin/system-alignment/validate") &&
        r.request().method() === "POST",
    );
    await this.continueButton.click();
    this.validationResponse = await (await validated).json();
  }

  async selectChannelBuild(tag: string, channel: string) {
    await expect(
      this.buildSelect.locator(`optgroup[label="${CHANNEL_GROUPS[channel]}"] option[value="${tag}"]`),
    ).toHaveCount(1);
    await this.selectBuild(tag);
  }

  /** The status line as it stands, or "" while the step shows none; never waits for one to appear. */
  async statusText(): Promise<string> {
    if (!(await this.status.count())) return "";
    return ((await this.status.textContent({ timeout: 1_000 }).catch(() => "")) ?? "").trim();
  }

  /** Whether the step offers the Admin update, enabled: the button exists only while it may be needed. */
  async adminUpdateOffered() {
    return (await this.adminUpdateButton.count()) > 0 && (await this.adminUpdateButton.isEnabled());
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

  /**
   * The verification the Admin that answers now gives for ``tag``, asked with
   * the console's session. The console verifies again on its own once it
   * resumes, but a verification the replaced Admin answered before the reload
   * may be the one recorded; this one can only come from the new Admin.
   */
  async revalidate(tag: string): Promise<Record<string, any>> {
    const auth = await (await this.page.request.get("/api/admin/auth/status")).json();
    const response = await this.page.request.post("/api/admin/system-alignment/validate", {
      headers: { "X-CSRF-Token": auth.csrf_token as string },
      data: { tag },
    });
    expect(response.ok(), `validate ${tag}: ${response.status()}`).toBeTruthy();
    const body = await response.json();
    this.validationResponse = body;
    this.validationResponses.push(body);
    return body;
  }

  validationHistory(): Record<string, any>[] {
    return [...this.validationResponses];
  }

  resetValidationHistory() {
    this.validationResponse = null;
    this.validationResponses = [];
  }

  devicesTab(): Locator {
    return this.page.getByRole("tab", { name: /Devices/ });
  }

  async continueToDevices() {
    await this.continueButton.click();
    await expect(this.devicesTab()).toHaveAttribute("aria-selected", "true");
  }
}
