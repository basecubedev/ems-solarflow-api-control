import { test, expect } from "@playwright/test";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";
import { expectValidSystemBuildAction } from "./helpers/system-build-action";

const TAG = process.env.CANARY_TAG as string;
const REVISION = process.env.CANARY_REVISION as string;
const BUILD_ID = process.env.CANARY_BUILD_ID as string;
const DISPLAY_NAME = process.env.CANARY_DISPLAY_NAME as string;
const ADMIN_DIGEST = process.env.CANARY_ADMIN_DIGEST as string;
const EMS_DIGEST = process.env.CANARY_EMS_DIGEST as string;

test("remote packaged Admin selects and executes the exact catalogue pair", { tag: ["@system-build"] }, async ({
  page,
}, testInfo) => {
  for (const [name, value] of Object.entries({
    TAG,
    REVISION,
    BUILD_ID,
    DISPLAY_NAME,
    ADMIN_DIGEST,
    EMS_DIGEST,
  })) {
    expect(value, `${name} is required`).toBeTruthy();
  }

  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  const setup = new SetupPage(page);
  await setup.chooseFreshInstall();

  const option = setup.buildSelect.locator(
    `option[value="${TAG}"][data-channel="development"]`,
  );
  await expect(option).toHaveCount(1);
  await expect(option).toHaveAttribute("data-revision", REVISION);
  await expect(option).toHaveAttribute("data-build-id", BUILD_ID);
  await expect(option).toContainText(`Development — ${DISPLAY_NAME} · ${REVISION.slice(0, 7)}`);

  const releases = await page.request.get("/api/setup/releases");
  expect(releases.ok()).toBe(true);
  const catalogue = await releases.json();
  const selected = catalogue.releases.find((entry: { tag: string }) => entry.tag === TAG);
  expect(selected).toMatchObject({
    tag: TAG,
    channel: "development",
    revision: REVISION,
    build_id: BUILD_ID,
    admin_digest: ADMIN_DIGEST,
    ems_digest: EMS_DIGEST,
    installable: true,
    selectable: true,
  });

  await setup.selectDevelopmentBuild(TAG);
  const validation = await setup.latestValidation();
  expect(validation.system_build).toMatchObject({
    canonical_tag: TAG,
    channel: "development",
    revision: REVISION,
    build_id: BUILD_ID,
    admin_image: `ghcr.io/basecubedev/ems-solarflow-admin:${TAG}`,
    admin_digest: ADMIN_DIGEST,
    ems_image: `ghcr.io/basecubedev/ems-solarflow-api-control:${TAG}`,
    ems_digest: EMS_DIGEST,
  });
  expect(validation.action_state.resource_strategy).toBe("embedded");
  expect(await expectValidSystemBuildAction(page, setup, testInfo)).toBe("continue");
  if (process.env.PUBLIC_CATALOGUE_READ_ONLY === "1") return;
  await setup.continueToDevices();
});
