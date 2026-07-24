import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";

// The deterministic server rotates its process identity when the replacement
// launcher succeeds, so this covers the complete browser reconnect handoff.

test.describe("Admin update required", () => {
  test.beforeEach(async ({ page }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();
  });

  test("successful replacement reconnects and revalidates the selected build", async ({
    page,
  }) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await setup.selectBuild("v9.9.10");

    await expect(setup.adminUpdateButton).toBeEnabled();
    await expect(setup.continueButton).toBeDisabled();

    const updateRequests: string[] = [];
    let postReplacementValidations = 0;
    page.on("request", (r) => {
      if (r.url().includes("/api/setup/system-build/update-admin")) {
        updateRequests.push(r.url());
      }
    });
    page.on("response", (r) => {
      if (
        r.url().includes("/api/admin/system-alignment/validate") &&
        r.request().method() === "POST"
      ) {
        postReplacementValidations += 1;
      }
    });
    const updateResponse = page.waitForResponse((r) =>
      r.url().includes("/api/setup/system-build/update-admin"),
    );
    await setup.adminUpdateButton.click();
    const response = await updateResponse;

    // The server accepted the alignment start (reconnect handoff).
    expect(response.status()).toBe(202);
    expect(updateRequests.length).toBe(1);

    await expect(setup.buildSelect).toHaveValue("v9.9.10");
    await expect(setup.status).toHaveText(/ready for the selected System Build/i);
    await expect(setup.continueButton).toBeEnabled();
    await expect(setup.adminUpdateButton).toBeDisabled();
    expect(postReplacementValidations).toBeGreaterThanOrEqual(1);

    // A single durable transition for the target build now exists.
    const status = await page.request.get(
      "/api/admin/system-alignment/status",
    );
    const body = await status.json();
    expect(body.transition).not.toBeNull();
    expect(body.transition.system_tag).toBe("v9.9.10");
    expect(body.transition.mode).toBe("fresh_install");
    expect(body.transition.stage).toBe("resources_verified");
  });

  test("failed replacement leaves Continue blocked and retry available", async ({
    page,
  }) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await setup.selectBuild("v9.9.11");

    await expect(setup.adminUpdateButton).toBeEnabled();
    await setup.adminUpdateButton.click();

    await expect(setup.error).toBeVisible();
    await expect(setup.status).toHaveText(/update failed/i);
    await expect(setup.continueButton).toBeDisabled();
    await expect(setup.adminUpdateButton).toBeEnabled();
    await expect(setup.adminUpdateButton).toHaveText(/Try again/i);
  });
});
