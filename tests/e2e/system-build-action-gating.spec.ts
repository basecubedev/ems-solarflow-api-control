import { test, expect } from "./fixtures/admin";
import { expectValidSystemBuildAction } from "./helpers/system-build-action";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";

const DEVELOPMENT_TAG = "dev-development-deadbee-100-1";

test.describe("Fresh Install System Build action gating", { tag: ["@system-build"] }, () => {
  test.beforeEach(async ({ page }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();
  });

  test("an interrupted admin_aligned transition cannot lock both actions", async (
    { page, seedAdminScenario },
    testInfo,
  ) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await seedAdminScenario("system_build_admin_aligned");
    await setup.selectBuild("v9.9.10");

    expect(
      setup
        .validationHistory()
        .some(
          (entry) => entry.action_state?.transition_stage === "admin_aligned",
        ),
    ).toBe(true);
    await expectValidSystemBuildAction(page, setup, testInfo);
    const status = await page.request.get("/api/admin/system-alignment/status");
    const transition = await status.json();
    expect(transition.transition.stage).toBe("resources_verified");
  });

  test("Latest aligned enables Continue and advances", async ({ page }, testInfo) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await setup.selectBuild("latest");
    expect(await expectValidSystemBuildAction(page, setup, testInfo)).toBe(
      "continue",
    );
    await setup.continueToDevices();
  });

  test("Latest mismatch enables one Admin update action", async (
    { page, seedAdminScenario },
    testInfo,
  ) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await seedAdminScenario("system_build_latest_mismatch");
    await setup.selectBuild("latest");
    expect(await expectValidSystemBuildAction(page, setup, testInfo)).toBe(
      "admin_update",
    );
  });

  test("Latest Admin update reconnects, revalidates, and advances once", async (
    { page, seedAdminScenario },
    testInfo,
  ) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await seedAdminScenario("system_build_latest_mismatch");
    await setup.selectBuild("latest");
    expect(await expectValidSystemBuildAction(page, setup, testInfo)).toBe(
      "admin_update",
    );

    let updateRequests = 0;
    page.on("request", (request) => {
      if (request.url().includes("/api/setup/system-build/update-admin")) {
        updateRequests += 1;
      }
    });
    await setup.adminUpdateButton.click();
    await expect(setup.buildSelect).toHaveValue("latest");
    await expect(setup.status).toHaveText(/ready for the selected System Build/i);
    expect(await expectValidSystemBuildAction(page, setup, testInfo)).toBe(
      "continue",
    );
    expect(updateRequests).toBe(1);
    await setup.continueToDevices();
  });

  test("Development selects the exact development pair", async (
    { page },
    testInfo,
  ) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await setup.selectDevelopmentBuild(DEVELOPMENT_TAG);
    await expectValidSystemBuildAction(page, setup, testInfo);
    const validation = await setup.latestValidation();
    expect(validation.system_build.channel).toBe("development");
    expect(validation.system_build.canonical_tag).toBe(
      validation.system_build.build_id,
    );
  });

  test("Development aligned enables Continue and advances", async (
    { page, seedAdminScenario },
    testInfo,
  ) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await seedAdminScenario("system_build_development_aligned");
    await setup.selectDevelopmentBuild(DEVELOPMENT_TAG);
    expect(await expectValidSystemBuildAction(page, setup, testInfo)).toBe(
      "continue",
    );
    await setup.continueToDevices();
  });

  test("Development mismatch updates, reconnects, and advances once", async (
    { page },
    testInfo,
  ) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await setup.selectDevelopmentBuild(DEVELOPMENT_TAG);
    expect(await expectValidSystemBuildAction(page, setup, testInfo)).toBe(
      "admin_update",
    );

    let updateRequests = 0;
    page.on("request", (request) => {
      if (request.url().includes("/api/setup/system-build/update-admin")) {
        updateRequests += 1;
      }
    });
    await setup.adminUpdateButton.click();
    await expect(setup.status).toHaveText(/ready for the selected System Build/i);
    expect(await expectValidSystemBuildAction(page, setup, testInfo)).toBe(
      "continue",
    );
    expect(updateRequests).toBe(1);
    const validation = await setup.latestValidation();
    expect(validation.system_build.channel).toBe("development");
    await setup.continueToDevices();
  });

  test("v0.7.0 keeps the modern Admin and enables Continue", async (
    { page },
    testInfo,
  ) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await setup.selectBuild("v0.7.0");
    expect(await expectValidSystemBuildAction(page, setup, testInfo)).toBe(
      "continue",
    );
    const validation = await setup.latestValidation();
    expect(validation.compatibility_mode).toBe("legacy_release");
    expect(validation.resource_strategy).toBe("release_archive");
    await setup.continueToDevices();
  });

  test("v0.7.0 archive failure is terminal, visible, and does not advance", async (
    { page, seedAdminScenario },
    testInfo,
  ) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await seedAdminScenario("system_build_v070_resource_failure");
    await setup.selectBuild("v0.7.0");
    expect(await expectValidSystemBuildAction(page, setup, testInfo)).toBe(
      "continue",
    );

    const confirmation = page.waitForResponse((response) =>
      response.url().includes("/api/setup/system-build/confirm"),
    );
    await setup.continueButton.click();
    expect((await confirmation).ok()).toBe(false);
    await expect(setup.error).toBeVisible();
    await expect(setup.error).toContainText(/resources could not be prepared/i);
    await expect(setup.status).not.toHaveText(/ready|compatible|verified/i);
    await expect(setup.adminUpdateButton).toBeDisabled();
    await expect(setup.continueButton).toBeDisabled();
    await expect(setup.devicesTab()).toHaveAttribute("aria-selected", "false");
  });

  test("changing Latest to Development clears old identity", async (
    { page },
    testInfo,
  ) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await setup.selectBuild("latest");
    const latest = await setup.latestValidation();
    await setup.selectDevelopmentBuild(DEVELOPMENT_TAG);
    await expectValidSystemBuildAction(page, setup, testInfo);
    const development = await setup.latestValidation();
    expect(development.system_build.channel).toBe("development");
    expect(development.system_build.revision).not.toBe(
      latest.system_build.revision,
    );
    await expect(setup.progressRevision).toHaveText(
      development.system_build.revision,
    );
    await expect(setup.progressTag).toHaveText(
      development.system_build.canonical_tag,
    );
  });

  test("changing a confirmed Latest operation to v0.7.0 supersedes it automatically", async (
    { page },
    testInfo,
  ) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await setup.selectBuild("latest");
    await setup.continueToDevices();
    await page.locator('[data-setup-step="release"]').click();
    const before = await (await page.request.get("/api/setup/workflow")).json();

    // The build change retires the old Setup workflow as one backend
    // operation; the narrow transition primitive is never used for it.
    const superseded = page.waitForResponse(
      (response) =>
        response.url().includes("/api/setup/system-build/supersede") &&
        response.request().method() === "POST",
    );
    await setup.selectBuild("v0.7.0");
    const supersedeBody = await (await superseded).json();
    expect(supersedeBody.ok, JSON.stringify(supersedeBody)).toBe(true);
    expect(supersedeBody.superseded_workflow_id).toBe(before.workflow.workflow_id);
    const after = await (await page.request.get("/api/setup/workflow")).json();
    expect(after.workflow.status).toBe("active");
    expect(after.workflow.workflow_id).not.toBe(before.workflow.workflow_id);
    expect(await expectValidSystemBuildAction(page, setup, testInfo)).toBe(
      "continue",
    );
    const status = await page.request.get("/api/admin/system-alignment/status");
    const alignment = await status.json();
    expect(alignment.transition.system_tag).toBe("latest");
    expect(alignment.transition.stage).toBe("cancelled");
    await setup.continueToDevices();
  });

  test("a stale Latest verification cannot verify a newer Development selection", async (
    { page },
    testInfo,
  ) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();

    // Deterministically control response order: hold the explicit Latest
    // verification until Development is selected, then release it and prove the
    // late Latest response cannot mark the newer selection verified. No sleeps.
    let releaseLatest: () => void = () => {};
    const latestHeld = new Promise<void>((resolve) => {
      releaseLatest = resolve;
    });
    let markLatestSeen: () => void = () => {};
    const latestSeen = new Promise<void>((resolve) => {
      markLatestSeen = resolve;
    });
    await page.route("**/api/admin/system-alignment/validate", async (route) => {
      if (route.request().postDataJSON()?.tag === "latest") {
        markLatestSeen();
        await latestHeld;
      }
      await route.continue();
    });

    const developmentTag = DEVELOPMENT_TAG;

    // Select Latest and click Verify — its validation request is sent (and held)
    // before Development is selected, so a genuine stale response is in flight.
    await setup.previewBuild("latest");
    await setup.continueButton.click();
    await latestSeen;
    await setup.buildSelect.selectOption(developmentTag);
    await expect(setup.buildSelect).toHaveValue(developmentTag);
    // Development is only selected (previewed), never auto-verified: the primary
    // is still the explicit Verify action.
    await expect(setup.continueButton).toHaveText(/Verify System Build/i);

    // Release the stale Latest response; it must not verify Development.
    const latestResponse = page.waitForResponse(
      (response) =>
        response.url().includes("/api/admin/system-alignment/validate") &&
        response.request().postDataJSON()?.tag === "latest",
    );
    releaseLatest();
    await latestResponse;

    // The stale Latest verdict changed nothing: Development stays selected and
    // unverified, so Continue is not enabled and Verify is still required.
    await expect(setup.buildSelect).toHaveValue(developmentTag);
    await expect(setup.continueButton).toHaveText(/Verify System Build/i);
    await expect(setup.continueButton).toBeEnabled();

    // Verifying Development now resolves the correct build.
    await page.unroute("**/api/admin/system-alignment/validate");
    await setup.verifyBuild();
    await expect(setup.progressTag).toHaveText(developmentTag);
    const selected = await setup.latestValidation();
    expect(selected.system_build.channel).toBe("development");
    await expectValidSystemBuildAction(page, setup, testInfo);
  });

  test("changing Development to v0.7.0 clears development identity", async (
    { page },
    testInfo,
  ) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await setup.selectDevelopmentBuild(DEVELOPMENT_TAG);
    await setup.selectBuild("v0.7.0");
    expect(await expectValidSystemBuildAction(page, setup, testInfo)).toBe(
      "continue",
    );
    const selected = await setup.latestValidation();
    expect(selected.compatibility_mode).toBe("legacy_release");
    await expect(setup.progressTag).toHaveText("v0.7.0");
  });

  test("reload restores v0.7.0 and a clickable Continue", async (
    { page },
    testInfo,
  ) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await setup.selectBuild("v0.7.0");
    await setup.continueToDevices();
    await page.reload();
    await expect(setup.buildSelect).toBeVisible();
    await expect(setup.buildSelect).toHaveValue("v0.7.0");
    expect(await expectValidSystemBuildAction(page, setup, testInfo)).toBe(
      "continue",
    );
    await setup.continueToDevices();
  });

  test("validation network failure exits busy state with a visible retry", async ({
    page,
  }) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    // Selecting is side-effect free; the failure only happens on explicit Verify.
    await setup.previewBuild("v0.7.0");
    await page.route("**/api/admin/system-alignment/validate", (route) =>
      route.abort("connectionfailed"),
    );
    await setup.continueButton.click();
    await expect(setup.status).toHaveText(/validation failed/i);
    await expect(setup.error).toBeVisible();
    await expect(setup.error).toContainText(/failed|connection|fetch/i);
    await expect(setup.continueButton).toBeDisabled();
    await expect(setup.adminUpdateButton).toHaveText(/check again/i);
  });

  test("alignment polling failure exits busy state visibly", async ({
    page,
    seedAdminScenario,
  }) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    let abortPolling = false;
    let markPollAborted: (() => void) | undefined;
    const pollAborted = new Promise<void>((resolve) => {
      markPollAborted = resolve;
    });
    await page.route("**/api/admin/system-alignment/status", (route) => {
      if (abortPolling) {
        markPollAborted?.();
        return route.abort("connectionfailed");
      }
      return route.continue();
    });
    await seedAdminScenario("system_build_resource_verification_running");
    await setup.previewBuild("latest");
    const validation = page.waitForResponse(
      (response) =>
        response.url().includes("/api/admin/system-alignment/validate") &&
        response.request().method() === "POST",
    );
    await setup.continueButton.click();
    await validation;
    abortPolling = true;
    await pollAborted;
    await expect(setup.error).toBeVisible();
    await expect(setup.error).toContainText(/could not check System Build progress/i);
    await expect(setup.status).not.toHaveText(/Verifying selected/i);
  });
});
