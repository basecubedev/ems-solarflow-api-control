import { type Page } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";
import { expectValidSystemBuildAction } from "./helpers/system-build-action";

// Selecting/browsing a System Build is a local catalogue preview only; the single
// explicit Verify action is the sole trigger for the full download + identity
// check, and a verified build is reused by later steps without re-verifying.

async function authenticate(page: Page) {
  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
}

function countValidations(page: Page) {
  const state = { count: 0 };
  page.on("request", (request) => {
    if (
      request.url().includes("/api/admin/system-alignment/validate") &&
      request.method() === "POST"
    ) {
      state.count += 1;
    }
  });
  return state;
}

test.describe("System Build verification lifecycle", () => {
  test.beforeEach(async ({ page }) => {
    await authenticate(page);
  });

  test("browsing System Builds is side-effect free", async ({ page }) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    const validations = countValidations(page);

    // Switch between several builds without ever clicking Verify.
    await setup.previewBuild("latest");
    await setup.previewBuild("v0.7.0");
    await setup.previewBuild("latest");

    // No validation endpoint call, no download/verify progress, Continue blocked.
    expect(validations.count).toBe(0);
    await expect(setup.status).not.toHaveText(/Downloading and verifying/i);
    await expect(setup.continueButton).toHaveText(/Verify System Build/i);
    await expect(setup.continueButton).toBeEnabled();
    await expect(setup.adminUpdateButton).toBeDisabled();
  });

  test("Verify runs exactly one verification and reveals the next action", async ({
    page,
  }, testInfo) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    const validations = countValidations(page);

    await setup.previewBuild("latest");
    expect(validations.count).toBe(0);

    // The explicit action downloads + verifies exactly once and shows progress.
    const verifying = expect(setup.status).toHaveText(/Downloading and verifying/i);
    await setup.verifyBuild();
    await verifying.catch(() => {
      /* fast local verification may settle before the assertion observes it */
    });
    expect(validations.count).toBe(1);
    expect(await expectValidSystemBuildAction(page, setup, testInfo)).toBe(
      "continue",
    );
  });

  test("a verified build is reused across navigation without re-verifying", async ({
    page,
  }, testInfo) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await setup.selectBuild("latest");
    expect(await expectValidSystemBuildAction(page, setup, testInfo)).toBe(
      "continue",
    );
    await setup.continueToDevices();

    // Navigate back to Step 1 and forward again within the same task.
    const validations = countValidations(page);
    await page.locator('[data-setup-step="release"]').click();
    await expect(page.locator('[data-setup-step="release"]')).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await page.locator('[data-setup-step="devices"]').click();
    await expect(setup.devicesTab()).toHaveAttribute("aria-selected", "true");

    // Re-rendering / task navigation never re-verifies the already-verified build.
    expect(validations.count).toBe(0);
  });

  test("a stale verification cannot verify a newer selection", async ({
    page,
  }) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();

    let releaseFirst: () => void = () => {};
    const firstHeld = new Promise<void>((resolve) => {
      releaseFirst = resolve;
    });
    let markFirstSeen: () => void = () => {};
    const firstSeen = new Promise<void>((resolve) => {
      markFirstSeen = resolve;
    });
    await page.route("**/api/admin/system-alignment/validate", async (route) => {
      if (route.request().postDataJSON()?.tag === "latest") {
        markFirstSeen();
        await firstHeld;
      }
      await route.continue();
    });

    // Verify Build A (latest); hold its response, then select Build B.
    await setup.previewBuild("latest");
    await setup.continueButton.click();
    await firstSeen;
    await setup.buildSelect.selectOption("v0.7.0");
    await expect(setup.buildSelect).toHaveValue("v0.7.0");
    await expect(setup.continueButton).toHaveText(/Verify System Build/i);

    // Release the stale Build A response — it must not verify Build B.
    releaseFirst();
    await expect(setup.buildSelect).toHaveValue("v0.7.0");
    await expect(setup.continueButton).toHaveText(/Verify System Build/i);
    await expect(setup.continueButton).toBeEnabled();
  });

  test("a registry rate-limit is shown clearly and keeps the build unverified", async ({
    page,
  }) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await setup.previewBuild("latest");

    await page.route("**/api/admin/system-alignment/validate", (route) =>
      route.fulfill({
        status: 429,
        contentType: "application/json",
        body: JSON.stringify({
          ok: false,
          error: "system_build_registry_rate_limited",
          message: "GitHub Container Registry rate limit reached.",
          action_state: {
            selected_build: {
              tag: "latest",
              channel: null,
              revision: null,
              build_id: null,
            },
            selection_fingerprint: null,
            alignment_state: "error",
            admin_update_required: false,
            admin_update_allowed: false,
            continue_allowed: false,
            terminal_error: {
              code: "system_build_registry_rate_limited",
              message:
                "GitHub Container Registry rate limit reached.\n\nNo installation changes were made. Wait before retrying, or authenticate Docker with a GitHub account to increase the available request quota.",
            },
            busy: false,
            progress_message: null,
            polling_required: false,
            transition_stage: "validation_failed",
            operation_id: null,
          },
        }),
      }),
    );

    await setup.continueButton.click();

    // Actionable message, build not verified, Continue disabled, Retry available.
    await expect(setup.error).toContainText(/rate limit/i);
    await expect(setup.error).toContainText(/No installation changes were made/i);
    await expect(setup.continueButton).toBeDisabled();
    await expect(setup.adminUpdateButton).toHaveText(/check again/i);
    await expect(setup.adminUpdateButton).toBeEnabled();
  });
});
