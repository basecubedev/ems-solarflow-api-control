import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { test, expect, type Page } from "@playwright/test";
import { expectValidSystemBuildAction } from "./helpers/system-build-action";
import { ADMIN_PASSWORD } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";

const SOURCE_TAG = process.env.CANARY_SOURCE_TAG as string;
const SOURCE_REVISION = process.env.CANARY_SOURCE_REVISION as string;
const SOURCE_BUILD_ID = process.env.CANARY_SOURCE_BUILD_ID as string;
const SOURCE_ADMIN_DIGEST = process.env.CANARY_SOURCE_ADMIN_DIGEST as string;
const TAG = process.env.CANARY_TAG as string;
const REVISION = process.env.CANARY_REVISION as string;
const BUILD_ID = process.env.CANARY_BUILD_ID as string;
const ADMIN_DIGEST = process.env.CANARY_ADMIN_DIGEST as string;
const EMS_DIGEST = process.env.CANARY_EMS_DIGEST as string;
const RUNTIME = process.env.ADMIN_REPLACEMENT_RUNTIME as string;
const EVENTS = process.env.ADMIN_REPLACEMENT_EVENTS as string;
const CONTAINER = "ems-solarflow-admin";
const ADMIN_REPO = "ghcr.io/basecubedev/ems-solarflow-admin";
const EMS_REPO = "ghcr.io/basecubedev/ems-solarflow-api-control";

function docker(...args: string[]) {
  return execFileSync("docker", args, { encoding: "utf8" }).trim();
}

function imageId(reference: string) {
  return docker("image", "inspect", "--format", "{{.Id}}", reference);
}

function imageLabels(reference: string): Record<string, string> {
  return JSON.parse(docker("image", "inspect", "--format", "{{json .Config.Labels}}", reference));
}

function expectBuildIdentity(reference: string, revision: string, buildId: string) {
  const labels = imageLabels(reference);
  expect(labels["org.opencontainers.image.revision"], `${reference} revision`).toBe(revision);
  expect(labels["de.basecubedev.ems.build_id"], `${reference} build id`).toBe(buildId);
  expect(labels["de.basecubedev.ems.channel"], `${reference} channel`).toBe("development");
}

async function adminInstanceId(page: Page) {
  return (await (await page.request.get("/api/admin/auth/status")).json()).admin_instance_id;
}

async function reauthenticateAfterReconnect(page: Page, setup: SetupPage) {
  let outcome = "waiting";
  await expect
    .poll(
      async () => {
        const loginVisible = await page
          .locator("#auth-login:not([hidden]), #auth-create:not([hidden])")
          .isVisible();
        const ready = /ready for the selected System Build/i.test(
          (await setup.status.textContent()) || "",
        );
        outcome = loginVisible ? "login" : ready ? "ready" : "waiting";
        return outcome;
      },
      { timeout: 120_000 },
    )
    .toMatch(/login|ready/);
  if (outcome === "login") {
    await expect(setup.continueButton).toBeDisabled();
    const login = page.locator("#auth-login:not([hidden])");
    await page.fill("#auth-login-password", ADMIN_PASSWORD);
    await login.locator('button[type="submit"]').click();
    await expect(login).toBeHidden();
  }
}

test("real Admin replacement reconnects and preserves the selected build", async ({
  page,
}, testInfo) => {
  test.setTimeout(180_000);
  for (const [name, value] of Object.entries({
    CANARY_SOURCE_TAG: SOURCE_TAG,
    CANARY_SOURCE_REVISION: SOURCE_REVISION,
    CANARY_SOURCE_BUILD_ID: SOURCE_BUILD_ID,
    CANARY_SOURCE_ADMIN_DIGEST: SOURCE_ADMIN_DIGEST,
    CANARY_TAG: TAG,
    CANARY_REVISION: REVISION,
    CANARY_BUILD_ID: BUILD_ID,
    CANARY_ADMIN_DIGEST: ADMIN_DIGEST,
    CANARY_EMS_DIGEST: EMS_DIGEST,
  })) {
    expect(value, `${name} is required`).toBeTruthy();
  }
  expect(SOURCE_ADMIN_DIGEST, "source and target Admin must differ").not.toBe(ADMIN_DIGEST);
  for (const digest of [SOURCE_ADMIN_DIGEST, ADMIN_DIGEST, EMS_DIGEST]) {
    expect(digest, "canary images are digest-pinned").toMatch(/^sha256:[0-9a-f]{64}$/);
  }

  const sourceAdmin = `${ADMIN_REPO}@${SOURCE_ADMIN_DIGEST}`;
  const targetAdmin = `${ADMIN_REPO}@${ADMIN_DIGEST}`;
  const targetEms = `${EMS_REPO}@${EMS_DIGEST}`;
  expectBuildIdentity(sourceAdmin, SOURCE_REVISION, SOURCE_BUILD_ID);
  expectBuildIdentity(targetAdmin, REVISION, BUILD_ID);
  expectBuildIdentity(targetEms, REVISION, BUILD_ID);
  expect(
    docker("container", "inspect", "--format", "{{.Image}}", CONTAINER),
    "journey starts on the published source Admin",
  ).toBe(imageId(sourceAdmin));
  expect(
    readFileSync(`${RUNTIME}/install/docker-compose.admin.yml`, "utf8"),
    "source Admin reference",
  ).toContain(`${ADMIN_REPO}:${SOURCE_TAG}`);

  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  const sourceInstance = await adminInstanceId(page);
  const setup = new SetupPage(page);
  await setup.chooseFreshInstall();
  await setup.selectDevelopmentBuild(TAG);
  expect(await expectValidSystemBuildAction(page, setup, testInfo)).toBe("admin_update");

  const oldContainer = docker("container", "inspect", "--format", "{{.Id}}", CONTAINER);
  let updateRequests = 0;
  page.on("request", (request) => {
    if (request.url().includes("/api/setup/system-build/update-admin")) updateRequests += 1;
  });

  // Only a validation the replaced Admin served may back the assertions below.
  setup.resetValidationHistory();
  await setup.adminUpdateButton.click();
  await expect(setup.continueButton).toBeDisabled();
  await reauthenticateAfterReconnect(page, setup);
  await expect(setup.buildSelect).toHaveValue(TAG);
  await expect(setup.status).toHaveText(/ready for the selected System Build/i, {
    timeout: 120_000,
  });
  expect(await expectValidSystemBuildAction(page, setup, testInfo)).toBe("continue");
  expect(updateRequests).toBe(1);

  const activeOld = docker("ps", "-aq", "--no-trunc", "--filter", `id=${oldContainer}`);
  expect(activeOld, "old container no longer active").toBe("");
  const currentImage = docker("container", "inspect", "--format", "{{.Image}}", CONTAINER);
  expect(currentImage, "new Admin target digest").toBe(imageId(targetAdmin));
  expect(await adminInstanceId(page), "a replaced Admin process serves the reconnect").not.toBe(
    sourceInstance,
  );

  const compose = readFileSync(`${RUNTIME}/install/docker-compose.admin.yml`, "utf8");
  expect(compose, "persistent Admin reference").toContain(`${ADMIN_REPO}:${TAG}`);
  const auth = await (await page.request.get("/api/admin/auth/status")).json();
  expect(auth.authenticated, "session remains authenticated after reconnect").toBe(true);

  const validation = await setup.latestValidation();
  // resource_status(verified=True) reports "prepared"; the verified stage itself
  // is asserted through the transition below.
  expect(validation.action_state.resource_state).toBe("prepared");
  expect(validation.system_build, "target EMS identity").toMatchObject({
    canonical_tag: TAG,
    channel: "development",
    revision: REVISION,
    build_id: BUILD_ID,
    admin_image: `${ADMIN_REPO}:${TAG}`,
    admin_digest: ADMIN_DIGEST,
    ems_image: `${EMS_REPO}:${TAG}`,
    ems_digest: EMS_DIGEST,
  });
  const status = await (await page.request.get("/api/admin/system-alignment/status")).json();
  expect(status.transition.stage).toBe("resources_verified");
  await setup.continueToDevices();
  await expect(setup.devicesTab()).toHaveText(/Devices/i);

  await expect
    .poll(() => readFileSync(EVENTS, "utf8").trim().split("\n").filter(Boolean).length, {
      message: "replacement events must contain exactly one Admin destroy",
      timeout: 10_000,
    })
    .toBe(1);
});
