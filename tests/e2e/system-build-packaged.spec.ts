import { type Page, type TestInfo } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { expectValidSystemBuildAction } from "./helpers/system-build-action";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";

const PACKAGED_DEVELOPMENT_TAG = "dev-development-deadbee-100-1";

async function openFreshInstall(page: Page) {
  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  const setup = new SetupPage(page);
  await setup.chooseFreshInstall();
  return setup;
}

async function clickAuthorizedAction(
  page: Page,
  setup: SetupPage,
  testInfo: TestInfo,
) {
  const action = await expectValidSystemBuildAction(page, setup, testInfo);
  if (action === "admin_update") {
    await setup.adminUpdateButton.click();
    await expect(setup.status).toHaveText(/ready for the selected System Build/i);
    expect(await expectValidSystemBuildAction(page, setup, testInfo)).toBe("continue");
  } else {
    expect(action).toBe("continue");
  }
  await setup.continueToDevices();
}

test.describe("system_build_browser_gate packaged Admin", { tag: ["@system-build"] }, () => {
  test("production catalogue labels immutable Development builds newest first", async ({
    page,
  }) => {
    const setup = await openFreshInstall(page);
    const options = setup.developmentOptions();
    await expect(options).toHaveCount(2);
    await expect(options.nth(0)).toHaveAttribute("value", PACKAGED_DEVELOPMENT_TAG);
    await expect(options.nth(0)).toHaveAttribute("data-revision", /^deadbee/);
    await expect(options.nth(0)).toHaveAttribute("data-build-id", PACKAGED_DEVELOPMENT_TAG);
    await expect(options.nth(0)).toContainText(
      "Development — system-build-action-gating · deadbee",
    );
    await expect(options.nth(1)).toContainText(
      "Development — system-build-action-gating · cafebad",
    );
    await expect(setup.buildSelect.locator('option[value="dev-development"]')).toHaveCount(0);
    await expect(
      setup.buildSelect.locator('option[value="dev-development-feedbad-98-1"]'),
    ).toHaveCount(0);
  });

  test("Latest produces and executes a valid packaged action", async ({ page }, testInfo) => {
    const setup = await openFreshInstall(page);
    await setup.selectBuild("latest");
    const validation = await setup.latestValidation();
    expect(validation.action_state.selected_build.tag).toBe("latest");
    await clickAuthorizedAction(page, setup, testInfo);
  });

  test("Development consumes its packaged descriptor and advances", async (
    { page },
    testInfo,
  ) => {
    const setup = await openFreshInstall(page);
    await setup.selectDevelopmentBuild(PACKAGED_DEVELOPMENT_TAG);
    const validation = await setup.latestValidation();
    expect(validation.action_state.selected_build.tag).toBe(PACKAGED_DEVELOPMENT_TAG);
    expect(validation.action_state.selected_build.channel).toBe("development");
    expect(validation.action_state.selected_build.build_id).toBe(PACKAGED_DEVELOPMENT_TAG);
    expect(validation.action_state.resource_strategy).toBe("embedded");
    expect(validation.action_state.alignment_state).toBe("aligned");
    await clickAuthorizedAction(page, setup, testInfo);
  });

  test("v0.7.0 keeps the packaged modern Admin and advances", async ({ page }, testInfo) => {
    const setup = await openFreshInstall(page);
    await setup.selectBuild("v0.7.0");
    const validation = await setup.latestValidation();
    expect(validation.action_state.compatibility_mode).toBe("legacy_release");
    expect(validation.action_state.resource_strategy).toBe("release_archive");
    expect(validation.action_state.admin_update_allowed).toBe(false);
    await clickAuthorizedAction(page, setup, testInfo);
  });
});
