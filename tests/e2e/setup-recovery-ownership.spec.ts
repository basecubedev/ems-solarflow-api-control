import { type Page } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";
import {
  authorizeSetupMutation,
  currentWorkflow,
  post,
} from "./helpers/setup-authority";

// Recovery belongs to the workflow that owns the transition. A Setup-owned
// transition discards its own artifacts; an upgrade cancellation must never
// adopt those semantics.

const DRAFT = {
  devices: [
    {
      role: "inverter",
      enabled: true,
      config_name: "WR1",
      display_name: "Inv",
      ip: "192.168.1.100",
      serial_number: "SN1",
    },
  ],
  supported_grid_meter_count: 0,
};

async function generatedConfig(page: Page) {
  return (await (await page.request.get("/api/setup/config/status")).json()) as any;
}

async function alignment(page: Page) {
  return (await (await page.request.get("/api/admin/system-alignment/status")).json()) as any;
}

/** Stage the Setup-owned artifacts a recovery has to clean up.
 *
 * The wizard re-previews on its own poll cycle, so the page is parked first:
 * otherwise it would race the exact preview this staging just obtained.
 */
async function stageSetupArtifacts(page: Page) {
  await page.goto("about:blank");
  const written = await post(
    page,
    "/api/setup/config/write",
    await authorizeSetupMutation(page, { ...DRAFT, overwrite: true }),
  );
  expect(written.status, JSON.stringify(written.body)).toBe(200);
  expect((await generatedConfig(page)).exists).toBe(true);
}

test.describe("Recovery ownership", { tag: ["@setup", "@authority"] }, () => {
  test.beforeEach(async ({ page }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();
  });

  test("a Setup-owned recovery discards its artifacts and spares the system", async ({
    page,
  }) => {
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await setup.selectBuild("v0.7.0");
    await setup.continueToDevices();
    await stageSetupArtifacts(page);

    const before = await alignment(page);
    expect(before.transition.mode).toBe("fresh_install");

    // The workflow on record must be named exactly; an empty request is never
    // authority over whatever workflow happens to be stored.
    const discarded = await post(page, "/api/setup/abandon", {
      setup_workflow_id: (await currentWorkflow(page))?.workflow_id,
    });
    expect(discarded.status, JSON.stringify(discarded.body)).toBe(200);
    expect(discarded.body.ok).toBe(true);

    // Transition terminal, Setup artifacts gone, workflow marked terminal.
    const after = await alignment(page);
    expect(["cancelled", undefined]).toContain(after.transition?.stage);
    expect((await generatedConfig(page)).exists).toBe(false);
    expect(discarded.body.deployment_marker.exists).toBe(false);
    expect(discarded.body.workflow.status).toBe("abandoned");
    expect((await currentWorkflow(page))?.status).toBe("abandoned");

    // Nothing outside Setup's ownership was touched.
    const maintenance = await (
      await page.request.get("/api/admin/maintenance/config")
    ).json();
    expect(["ok", "missing"]).toContain(maintenance.status);
  });

  test("an upgrade cancellation never clears Setup-owned artifacts", async ({
    page,
    seedAdminScenario,
  }) => {
    // A Setup run staged its artifacts…
    const setup = new SetupPage(page);
    await setup.chooseFreshInstall();
    await setup.selectBuild("v0.7.0");
    await setup.continueToDevices();
    await stageSetupArtifacts(page);

    // …and the narrow primitive refuses to end that Setup-owned transition,
    // because cancelling it alone would orphan exactly those artifacts.
    const refused = await post(page, "/api/admin/system-alignment/cancel", {
      operation_id: (await alignment(page)).transition.operation_id,
      confirm: true,
    });
    expect(refused.status, JSON.stringify(refused.body)).toBe(409);
    expect(refused.body.error).toBe("setup_abandon_required");
    expect((await generatedConfig(page)).exists).toBe(true);

    // …then an unrelated Guided Upgrade transition is cancelled.
    await seedAdminScenario("mqtt_migration");
    await page.reload();
    const upgrade = await alignment(page);
    if (upgrade.transition?.operation_id) {
      await post(page, "/api/admin/system-alignment/cancel", {
        operation_id: upgrade.transition.operation_id,
        confirm: true,
      });
    }

    // The upgrade cancellation owns none of Setup's state.
    expect((await generatedConfig(page)).exists).toBe(true);
  });
});
