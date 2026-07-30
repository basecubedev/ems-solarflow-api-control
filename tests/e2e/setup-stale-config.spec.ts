import { type Page } from "@playwright/test";
import { test as adminTest, expect as adminExpect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";
import {
  authorizeSetupMutation,
  post,
  startSetupWorkflow,
} from "./helpers/setup-authority";

// Scenario B: a live-config change made after a Setup config was generated
// must block that stale config from being deployed over it.
//
// The rejection happens before any workspace write, so "nothing was deployed" is
// observable as an absent prepared marker rather than a rolled-back one.

const INVERTER = {
  role: "inverter",
  enabled: true,
  config_name: "WR1",
  display_name: "Inv",
  ip: "192.168.1.100",
  serial_number: "SN1",
};
const SETUP_DRAFT = { devices: [INVERTER], supported_grid_meter_count: 0 };

async function maintenanceConfig(page: Page) {
  return (await (await page.request.get("/api/admin/maintenance/config")).json()) as any;
}

adminTest.describe("Stale generated config", () => {
  adminTest(
    "a live-config change blocks the stale Setup deployment and survives it",
    async ({ page, seedAdminScenario }) => {
      const login = new LoginPage(page);
      await login.open();
      await login.authenticate();

      // 02 — stage a generated config derived from the live config.
      const setup = new SetupPage(page);
      await setup.chooseFreshInstall();
      await setup.selectBuild("v0.7.0");
      await setup.continueToDevices();
      // The wizard re-previews on its own poll cycle; parking the page keeps
      // the API-driven staging below from racing the preview it just obtained.
      await page.goto("about:blank");
      const workflow = await startSetupWorkflow(page);
      const applied = await post(
        page,
        "/api/setup/config/apply",
        await authorizeSetupMutation(page, SETUP_DRAFT, workflow),
      );
      adminExpect(applied.status, JSON.stringify(applied.body)).toBe(200);
      const staged = await post(
        page,
        "/api/setup/config/write",
        await authorizeSetupMutation(
          page,
          { ...SETUP_DRAFT, overwrite: true },
          workflow,
        ),
      );
      adminExpect(staged.status, JSON.stringify(staged.body)).toBe(200);

      // 03 — the live config changes underneath the still-open Setup workflow.
      // The writer's identity is not the point (Maintenance is one such
      // writer); applying it directly keeps the Setup workflow active so it
      // still owns the generated artifact under test.
      const before = await maintenanceConfig(page);
      await seedAdminScenario("mqtt_mutate");
      const afterEdit = await maintenanceConfig(page);
      adminExpect(afterEdit.revision).not.toBe(before.revision);

      // 04/05 — attempt the deployment on the verified Setup transition.
      const stale = await post(page, "/api/setup/deployment/prepare", {
        setup_workflow_id: workflow,
      });
      adminExpect(stale.status, JSON.stringify(stale.body)).toBe(409);
      adminExpect(stale.body.reason).toBe("stale_generated_config");
      adminExpect(stale.body.message).toMatch(/changed after this configuration/i);

      // 06 — the external change is intact.
      const preserved = await maintenanceConfig(page);
      adminExpect(preserved.revision).toBe(afterEdit.revision);

      // 07 — nothing was deployed: no prepared marker, no job.
      adminExpect(stale.body.job).toBeUndefined();
      const plan = await (await page.request.get("/api/setup/deployment/plan")).json();
      adminExpect(plan.prepared).toBeNull();

      // 08 — the browser surfaces the conflict as an actionable error.
      await page.goto("/#setup");
      const deploymentTab = page.locator('[data-setup-step="deployment"]');
      await adminExpect(deploymentTab).toBeEnabled();
      await deploymentTab.click();
      const prepare = page.locator("#deployment-prepare");
      await adminExpect(prepare).toBeVisible();
      await prepare.click();
      const deploymentError = page.locator("#deployment-error");
      await adminExpect(deploymentError).toBeVisible();
      await adminExpect(deploymentError).toContainText(/changed after this configuration/i);

      // 09/10 — regenerating against the current live config clears the block.
      const regenerated = await post(
        page,
        "/api/setup/config/write",
        await authorizeSetupMutation(page, { ...SETUP_DRAFT, overwrite: true }),
      );
      adminExpect(regenerated.status, JSON.stringify(regenerated.body)).toBe(200);
      const retried = await post(page, "/api/setup/deployment/prepare", {});
      adminExpect(
        retried.body.reason,
        `freshness must no longer block: ${JSON.stringify(retried.body)}`,
      ).not.toBe("stale_generated_config");
      const untouched = await maintenanceConfig(page);
      adminExpect(untouched.revision).toBe(afterEdit.revision);
    },
  );
});
