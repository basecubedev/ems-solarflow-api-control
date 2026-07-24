import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { test, expect, type Page } from "@playwright/test";
import { expectValidSystemBuildAction } from "./helpers/system-build-action";
import { ADMIN_PASSWORD } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";

const TAG = process.env.CANARY_TAG as string;
const ADMIN_DIGEST = process.env.CANARY_ADMIN_DIGEST as string;
const RUNTIME = process.env.ADMIN_REPLACEMENT_RUNTIME as string;
const EVENTS = process.env.ADMIN_REPLACEMENT_EVENTS as string;
const CONTAINER = "ems-solarflow-admin";

function docker(...args: string[]) {
  return execFileSync("docker", args, { encoding: "utf8" }).trim();
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
  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  const setup = new SetupPage(page);
  await setup.chooseFreshInstall();
  await setup.selectDevelopmentBuild(TAG);
  expect(await expectValidSystemBuildAction(page, setup, testInfo)).toBe("admin_update");

  const oldContainer = docker("container", "inspect", "--format", "{{.Id}}", CONTAINER);
  let updateRequests = 0;
  page.on("request", (request) => {
    if (request.url().includes("/api/setup/system-build/update-admin")) updateRequests += 1;
  });

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
  const targetImage = docker(
    "image",
    "inspect",
    "--format",
    "{{.Id}}",
    `ghcr.io/basecubedev/ems-solarflow-admin@${ADMIN_DIGEST}`,
  );
  expect(currentImage, "new Admin target digest").toBe(targetImage);

  const compose = readFileSync(`${RUNTIME}/install/docker-compose.admin.yml`, "utf8");
  expect(compose, "persistent Admin reference").toContain(
    `ghcr.io/basecubedev/ems-solarflow-admin:${TAG}`,
  );
  const auth = await (await page.request.get("/api/admin/auth/status")).json();
  expect(auth.authenticated, "session remains authenticated after reconnect").toBe(true);

  const validation = await setup.latestValidation();
  expect(validation.action_state.resource_state).toBe("verified");
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
