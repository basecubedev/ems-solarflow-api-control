import { type Page } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";

// The seven-stage System Build pipeline is a task subworkflow, not an
// application-global card. These regressions pin that it renders only inside the
// authenticated task that owns the transition, and never leaks onto Login or
// Task Selection — and that the behaviour is identical before and after a
// browser refresh (the original bug was a stateful DOM leak that a refresh
// silently "repaired").

const pipeline = (page: Page) => page.locator("#system-alignment-workflow");
const pipelineInSetupSlot = (page: Page) =>
  page.locator("#setup-system-build-slot #system-alignment-workflow");

async function renderProgress(page: Page, payload: Record<string, unknown>) {
  await page.evaluate((value) => {
    const browser = window as typeof window & {
      renderSystemAlignmentStatus: (data: Record<string, unknown>) => void;
    };
    browser.renderSystemAlignmentStatus(value);
  }, payload);
}

// While the owning task is open, an active transition arms the status poll. Echo
// the transition-under-test from the status endpoint so a poll cannot overwrite
// the faked state with the server's real (empty) status mid-test — the same
// pattern the reload-preserves progress tests use.
async function mockAlignmentStatus(page: Page, payload: Record<string, unknown>) {
  await page.route("**/api/admin/system-alignment/status", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(payload),
    }),
  );
}

function freshInstall(stage: string): Record<string, unknown> {
  return {
    active: !["completed", "cancelled"].includes(stage),
    status: "validated",
    transition: {
      operation_id: "fresh-op",
      mode: "fresh_install",
      stage,
      system_tag: "latest",
      build_id: "latest-abc1234",
      revision: "abc1234def5678",
      admin_image: "ghcr.io/example/admin:latest",
      ems_image: "ghcr.io/example/ems:latest",
      admin_alignment_required: true,
    },
  };
}

async function expectNoLeakedBuildMetadata(page: Page) {
  // Before authentication (and outside an owning task) no release identity may
  // be exposed. The facts are reset to their neutral placeholders.
  await expect(pipeline(page)).toBeHidden();
  await expect(page.locator("#system-alignment-tag")).toHaveText("Not selected");
  await expect(page.locator("#system-alignment-revision")).toHaveText("Unknown");
  await expect(page.locator("#system-alignment-admin-image")).toHaveText("Unknown");
  await expect(page.locator("#system-alignment-ems-image")).toHaveText("Unknown");
}

test.describe("System Build ownership — Fresh Install", { tag: ["@authority", "@system-build"] }, () => {
  test.beforeEach(async ({ page }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();
  });

  test("pipeline stays inside Guided Setup and never leaks onto Login", async ({
    page,
  }) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();

    // A live transition renders the pipeline inside Guided Setup, not globally.
    await mockAlignmentStatus(page, freshInstall("admin_reconnect_pending"));
    await renderProgress(page, freshInstall("admin_reconnect_pending"));
    await expect(pipelineInSetupSlot(page)).toBeVisible();

    // A cancelled (terminal) transition must hide the full pipeline.
    await renderProgress(page, freshInstall("cancelled"));
    await expect(pipeline(page)).toBeHidden();

    // Logging out shows Login and clears every build reference immediately.
    await page.locator("#auth-logout").click();
    await expect(page.locator("#auth-login")).toBeVisible();
    await expectNoLeakedBuildMetadata(page);

    // Core assertion: a refresh does not change the visible behaviour.
    await page.reload();
    await expect(page.locator("#auth-login")).toBeVisible();
    await expectNoLeakedBuildMetadata(page);
  });

  test("session expiry hides the pipeline and clears metadata immediately", async ({
    page,
  }) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await mockAlignmentStatus(page, freshInstall("ems_operation_running"));
    await renderProgress(page, freshInstall("ems_operation_running"));
    await expect(pipelineInSetupSlot(page)).toBeVisible();

    // Force the next auth check to report an expired session, then trigger the
    // app's auth-loss path exactly as the wrapped fetch does on a 401.
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
    await expectNoLeakedBuildMetadata(page);
  });
});
