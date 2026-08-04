import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";

// Fresh Install Step 1 across the real browser + real Admin service. The
// deterministic catalog offers Latest (modern aligned), v9.9.10 (Admin update
// required) and v0.7.0 (legacy release installable by the modern Admin).

test.describe("Fresh Install — System Build selection", { tag: ["@smoke", "@setup"] }, () => {
  test.beforeEach(async ({ page }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();
  });

  test("modern aligned build enables Continue and reaches Devices", async ({
    page,
  }) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await setup.selectBuild("latest");

    await expect(setup.status).toHaveText(/ready for the selected System Build/i);
    await expect(setup.adminUpdateButton).toBeDisabled();
    await expect(setup.continueButton).toBeEnabled();

    // One click, one confirmation request.
    const confirmRequests: string[] = [];
    page.on("request", (r) => {
      if (r.url().includes("/api/setup/system-build/confirm")) {
        confirmRequests.push(r.url());
      }
    });
    await setup.continueToDevices();
    expect(confirmRequests.length).toBeLessThanOrEqual(1);
  });

  test("legacy v0.7.0 is Continue-able with the modern Admin (no deadlock)", async ({
    page,
  }) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await setup.selectBuild("v0.7.0");

    // The modern Admin can install the legacy release: no downgrade offered.
    await expect(setup.adminUpdateButton).toBeDisabled();
    await expect(setup.continueButton).toBeEnabled();
    await expect(setup.status).toHaveText(/legacy EMS release/i);

    // Not-applicable embedded resources are not rendered as a failure.
    await expect(setup.embeddedCheck).not.toHaveClass(/config-validation-item-error/);
    await expect(setup.embeddedCheck).toContainText(/verified release archive/i);

    // Mandatory deadlock invariant: a ready state never disables all actions.
    const updateEnabled = await setup.adminUpdateButton.isEnabled();
    const continueEnabled = await setup.continueButton.isEnabled();
    expect(updateEnabled || continueEnabled).toBeTruthy();

    await setup.continueToDevices();
  });

  test("modern build requiring an Admin update blocks Continue", async ({
    page,
  }) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await setup.selectBuild("v9.9.10");

    await expect(setup.adminUpdateButton).toBeEnabled();
    await expect(setup.continueButton).toBeDisabled();
    await expect(setup.status).toHaveText(/must be updated/i);
  });
});
