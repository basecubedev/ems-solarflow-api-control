import { type Page } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";

type ProgressPayload = Record<string, unknown>;

const BUILD = {
  canonical_tag: "latest",
  build_id: "latest-f7265fc",
  revision: "f7265fc747c2223f126f0ee7801e030c6226edf4",
  admin_image: "ghcr.io/basecubedev/solarflow-control-admin:latest",
  ems_image: "ghcr.io/basecubedev/solarflow-control:latest",
};

async function authenticate(page: Page) {
  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
}

async function renderProgress(page: Page, payload: ProgressPayload) {
  await page.evaluate((value) => {
    const browser = window as typeof window & {
      renderSystemAlignmentStatus: (data: ProgressPayload) => void;
    };
    browser.renderSystemAlignmentStatus(value);
  }, payload);
}

async function expectState(setup: SetupPage, key: string, state: string) {
  await expect(setup.progressStage(key)).toHaveAttribute("data-state", state);
}

function transition(stage: string, adminRequired: boolean): ProgressPayload {
  return {
    active: !["completed", "cancelled"].includes(stage),
    status: "validated",
    transition: {
      operation_id: "progress-op",
      mode: "fresh_install",
      stage,
      system_tag: BUILD.canonical_tag,
      build_id: BUILD.build_id,
      revision: BUILD.revision,
      admin_image: BUILD.admin_image,
      ems_image: BUILD.ems_image,
      admin_alignment_required: adminRequired,
    },
  };
}

function validationPayload(tag: string): ProgressPayload {
  return {
    ok: true,
    status: "validated",
    valid: true,
    alignment: "aligned",
    admin_update_required: false,
    next_allowed: true,
    confirmation_allowed: true,
    transition_in_progress: false,
    resources_verified: false,
    system_build: {
      ...BUILD,
      canonical_tag: tag,
      build_id: `${tag}-abcdef1`,
      revision: "abcdef1234567890abcdef1234567890abcdef12",
      admin_image: `admin:${tag}`,
      ems_image: `ems:${tag}`,
    },
    action_state: {
      selected_build: {
        tag,
        channel: tag === "latest" ? "latest" : "stable",
        revision: "abcdef1234567890abcdef1234567890abcdef12",
        build_id: `${tag}-abcdef1`,
      },
      admin_update_required: false,
      admin_update_allowed: false,
      continue_allowed: true,
      terminal_error: null,
      busy: false,
      polling_required: false,
      progress_message: null,
      transition_stage: null,
    },
    checks: {},
  };
}

test.describe("Authoritative System Build progress", { tag: ["@system-build"] }, () => {
  test.beforeEach(async ({ page }) => {
    await authenticate(page);
  });

  test("successful Continue advances to resources verified", async ({ page }) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await setup.selectBuild("latest");
    await setup.continueToDevices();

    await expectState(setup, "verify-resources", "done");
    await expectState(setup, "install-ems", "active");
    await expectState(setup, "validate", "done");
  });

  test("already-compatible Admin skips alignment and reconnect", async ({ page }) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await setup.selectBuild("latest");
    await setup.continueToDevices();

    await expectState(setup, "align-admin", "skipped");
    await expectState(setup, "reconnect", "skipped");
    await expect(setup.progressStage("align-admin")).toContainText("Not required");
    await expect(setup.progressStage("reconnect")).toContainText("Not required");
  });

  test("Admin replacement advances through reconnect", async ({ page }) => {
    const setup = new SetupPage(page);
    await renderProgress(page, transition("admin_update_pending", true));
    await expectState(setup, "align-admin", "active");
    await expectState(setup, "reconnect", "pending");

    await renderProgress(page, transition("admin_reconnect_pending", true));
    await expectState(setup, "align-admin", "done");
    await expectState(setup, "reconnect", "active");
  });

  test("deployment start activates step 06", async ({ page }) => {
    const setup = new SetupPage(page);
    await renderProgress(page, transition("ems_operation_running", false));
    await expectState(setup, "verify-resources", "done");
    await expectState(setup, "install-ems", "active");
    await expectState(setup, "verify-system", "pending");
  });

  test("healthcheck activates step 07", async ({ page }) => {
    const setup = new SetupPage(page);
    await renderProgress(page, transition("healthcheck_pending", false));
    await expectState(setup, "install-ems", "done");
    await expectState(setup, "verify-system", "active");
  });

  test("completion marks the workflow complete", async ({ page }) => {
    const setup = new SetupPage(page);
    await renderProgress(page, transition("completed", false));
    for (const key of ["select", "validate", "verify-resources", "install-ems", "verify-system"]) {
      await expectState(setup, key, "done");
    }
    await expectState(setup, "align-admin", "skipped");
    await expectState(setup, "reconnect", "skipped");
  });

  for (const [stage, activeStep] of [
    ["resources_verified", "install-ems"],
    ["ems_operation_running", "install-ems"],
    ["healthcheck_pending", "verify-system"],
  ] as const) {
    test(`reload preserves ${stage}`, async ({ page }) => {
      await page.route("**/api/admin/system-alignment/status", async (route) => {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(transition(stage, false)),
        });
      });
      await page.route("**/api/admin/system-alignment/validate", async (route) => {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            ...validationPayload(BUILD.canonical_tag),
            status: "validated",
            transition_stage: stage,
            transition_in_progress: true,
            operation_id: "progress-op",
            resources_verified: true,
          }),
        });
      });

      await page.reload();
      const setup = new SetupPage(page);
      await expectState(setup, activeStep, "active");
      await expectState(setup, "validate", "done");
      await expect(setup.progressTag).toHaveText(BUILD.canonical_tag);
    });
  }

  test("validation failure clears stale build information", async ({ page }) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await setup.selectBuild("latest");
    await expect(setup.progressRevision).toHaveText(BUILD.revision);

    await page.route("**/api/admin/system-alignment/validate", async (route) => {
      await route.fulfill({
        status: 422,
        contentType: "application/json",
        body: JSON.stringify({ valid: false, message: "Selected build is invalid." }),
      });
    });
    await setup.selectBuild("v9.9.10");

    await expect(setup.progressTag).toHaveText("v9.9.10");
    await expect(setup.progressRevision).toHaveText("Unknown");
    await expect(setup.progressAdminImage).toHaveText("Unknown");
    await expect(setup.progressEmsImage).toHaveText("Unknown");
    await expectState(setup, "validate", "failed");
  });

  test("switching builds clears the previous progress state", async ({ page }) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await setup.selectBuild("latest");
    await expect(setup.progressRevision).toHaveText(BUILD.revision);

    // Switching selection is side-effect free: it immediately clears the previous
    // build's identity without contacting the registry, before any verification.
    await setup.previewBuild("v9.9.10");
    await expect(setup.progressTag).toHaveText("v9.9.10");
    await expect(setup.progressRevision).toHaveText("Unknown");

    // The explicit verification then fills in the newly selected build's facts.
    await page.route("**/api/admin/system-alignment/validate", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(validationPayload("v9.9.10")),
      });
    });
    await setup.verifyBuild();
    await expect(setup.progressRevision).toHaveText(
      "abcdef1234567890abcdef1234567890abcdef12",
    );
  });
});
