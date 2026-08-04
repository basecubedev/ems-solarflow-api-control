import { type Page } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";

// Task-scope coverage for the System Build pipeline: Guided Upgrade ownership,
// cross-task isolation (a transition never mounts into the wrong task's slot),
// and the global reconnect overlay staying independent of the task-local
// pipeline.

const pipeline = (page: Page) => page.locator("#system-alignment-workflow");
const pipelineInSetupSlot = (page: Page) =>
  page.locator("#setup-system-build-slot #system-alignment-workflow");
const pipelineInUpgradeSlot = (page: Page) =>
  page.locator("#upgrade-system-build-slot #system-alignment-workflow");

async function renderProgress(page: Page, payload: Record<string, unknown>) {
  await page.evaluate((value) => {
    (window as typeof window & {
      renderSystemAlignmentStatus: (data: Record<string, unknown>) => void;
    }).renderSystemAlignmentStatus(value);
  }, payload);
}

// Echo the transition-under-test from the status endpoint so the poll (armed
// while the owning task is open) cannot overwrite the faked state with the
// server's real (empty) status mid-test.
async function mockAlignmentStatus(page: Page, payload: Record<string, unknown>) {
  await page.route("**/api/admin/system-alignment/status", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(payload),
    }),
  );
}

function transitionFor(mode: string, tag: string, stage: string) {
  return {
    active: !["completed", "cancelled"].includes(stage),
    status: "validated",
    transition: {
      operation_id: `${mode}-op`,
      mode,
      stage,
      system_tag: tag,
      build_id: `${tag}-abc1234`,
      revision: "abc1234def5678",
      admin_image: `ghcr.io/example/admin:${tag}`,
      ems_image: `ghcr.io/example/ems:${tag}`,
      admin_alignment_required: false,
    },
  };
}

const freshInstall = (stage: string) => transitionFor("fresh_install", "latest", stage);
const guidedUpgrade = (stage: string) => transitionFor("guided_upgrade", "v9.9.10", stage);

test.describe("System Build ownership — Guided Upgrade", { tag: ["@authority", "@system-build"] }, () => {
  test("pipeline is scoped to Guided Upgrade and restores on return", async ({
    page,
    seedAdminScenario,
  }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();
    // Seed an existing install so the manage-existing path opens Maintenance.
    await seedAdminScenario("mqtt_migration");
    await page.reload();
    // Wait for the post-reload resume to settle against the real (empty) status
    // so it never races the durable-transition mock installed next.
    await expect(page.locator("#view-start")).toBeVisible();

    // The server now reports a durable Guided Upgrade transition.
    await page.route("**/api/admin/system-alignment/status", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(guidedUpgrade("resources_verified")),
      }),
    );

    await page.locator('[data-start-path="manage_existing"]').click();
    await page.locator('[data-open-maintenance-path="upgrade"]').click();

    // The pipeline is inside Guided Upgrade, never inside Guided Setup.
    await expect(pipelineInUpgradeSlot(page)).toBeVisible();
    await expect(pipelineInSetupSlot(page)).toHaveCount(0);

    // Navigating to an unrelated maintenance sub-panel hides the pipeline. The
    // sub-panel cards live in the (now hidden) hub, so switch via the hash the
    // app itself uses.
    await page.evaluate(() => {
      window.location.hash = "maintenance-backup";
    });
    await expect(page.locator("#maintenance-backup-panel")).toBeVisible();
    await expect(pipeline(page)).toBeHidden();

    // Returning to Guided Upgrade restores the pipeline from backend state.
    await page.evaluate(() => {
      window.location.hash = "maintenance-upgrade";
    });
    await expect(pipelineInUpgradeSlot(page)).toBeVisible();
  });
});

test.describe("System Build cross-task isolation", { tag: ["@authority", "@system-build"] }, () => {
  test.beforeEach(async ({ page }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();
  });

  test("a transition only ever mounts into its own task's slot", async ({ page }) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();

    // A Fresh Install transition mounts into Guided Setup only.
    await mockAlignmentStatus(page, freshInstall("admin_reconnect_pending"));
    await renderProgress(page, freshInstall("admin_reconnect_pending"));
    await expect(pipelineInSetupSlot(page)).toHaveCount(1);
    await expect(pipelineInUpgradeSlot(page)).toHaveCount(0);

    // A Guided Upgrade transition — even while Guided Setup is the open view —
    // is owned by its mode and must never mount into Guided Setup.
    await renderProgress(page, guidedUpgrade("resources_verified"));
    await expect(pipelineInSetupSlot(page)).toHaveCount(0);
    await expect(pipelineInUpgradeSlot(page)).toHaveCount(1);
  });

  test("a transition whose mode has no owner parks instead of leaking", async ({
    page,
  }) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();

    // The align-existing rollback transition has no owning task; it must park in
    // the hidden container, never show inside whatever task is open.
    await renderProgress(
      page,
      transitionFor("align_existing_install", "latest", "admin_reconnect_pending"),
    );
    await expect(pipeline(page)).toBeHidden();
    await expect(pipelineInSetupSlot(page)).toHaveCount(0);
  });

  test("a synthetic preview from one task never returns into another", async ({
    page,
  }) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();

    // A synthetic validation preview carries no backend transition. active:false
    // keeps the status poll off, isolating the drop-on-navigation behaviour.
    await renderProgress(page, {
      active: false,
      status: "validation_failed",
      selected_tag: "v9.9.10",
      message: "This System Build cannot be installed.",
    });
    await expect(pipelineInSetupSlot(page)).toBeVisible();

    // Leaving to Task Selection drops the preview; re-opening a task must not
    // resurrect it from the cache.
    await page.evaluate(() => {
      (window as typeof window & { showLanding: () => void }).showLanding();
    });
    await expect(pipeline(page)).toBeHidden();
    await page.evaluate(() => {
      (window as typeof window & { setAdminView: (v: string) => void }).setAdminView(
        "setup",
      );
    });
    await expect(pipeline(page)).toBeHidden();
  });
});

test.describe("System Build reconnect overlay", { tag: ["@authority", "@system-build"] }, () => {
  test.beforeEach(async ({ page }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();
  });

  test("global overlay is independent and the pipeline never leaks onto Login", async ({
    page,
  }) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await mockAlignmentStatus(page, freshInstall("admin_reconnect_pending"));
    await renderProgress(page, freshInstall("admin_reconnect_pending"));
    await expect(pipelineInSetupSlot(page)).toBeVisible();

    // Admin replacement raises the global reconnect overlay.
    await page.evaluate(() => {
      (window as typeof window & { showReconnectOverlay: (m?: string) => void })
        .showReconnectOverlay("Admin Console update started.");
    });
    await expect(page.locator("#admin-update-overlay")).toBeVisible();
    // The pipeline is still task-local, not a global sibling above login.
    await expect(pipelineInSetupSlot(page)).toBeVisible();

    // If the session drops while the replacement Admin comes up, Login shows and
    // the pipeline must not leak onto it — while the overlay stays functional.
    await page.route("**/api/admin/auth/status", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          authenticated: false,
          auth_configured: true,
          requires_initial_password: false,
          recovery_required: false,
        }),
      }),
    );
    await page.evaluate(() => {
      (window as typeof window & { onAuthLost: () => void }).onAuthLost();
    });

    await expect(page.locator("#auth-login")).toBeVisible();
    await expect(pipeline(page)).toBeHidden();
    await expect(page.locator("#admin-update-overlay")).toBeVisible();

    // The overlay remains fully controllable (global recovery is preserved).
    await page.evaluate(() => {
      (window as typeof window & { hideReconnectOverlay: () => void })
        .hideReconnectOverlay();
    });
    await expect(page.locator("#admin-update-overlay")).toBeHidden();
  });
});
