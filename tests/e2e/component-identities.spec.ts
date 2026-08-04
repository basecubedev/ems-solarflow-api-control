import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";

const ADMIN_IMAGE = "ghcr.io/basecubedev/ems-solarflow-admin:v0.9.0";

for (const scenario of [
  {
    name: "modern Admin and modern EMS",
    emsImage: "ghcr.io/basecubedev/ems-solarflow-api-control:v0.9.0",
    emsTag: "v0.9.0",
  },
  {
    name: "modern Admin and legacy EMS",
    emsImage: "ghcr.io/basecubedev/ems-solarflow-api-control:v0.7.0",
    emsTag: "v0.7.0",
  },
]) {
  test(`Maintenance shows ${scenario.name} as separate identities`, { tag: ["@smoke"] }, async ({
    page,
    seedAdminScenario,
  }) => {
    const login = new LoginPage(page);
    await login.open();
    await login.authenticate();
    await seedAdminScenario("mixed_transports");
    await page.route("**/api/admin/maintenance/overview", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          install_state: {
            state: "standard_install",
            label: "Standard installation",
            message: "A standard EMS installation was found.",
          },
          paths: {
            config: { path: "/install/config/config.json", exists: true },
            data: { path: "/install/data", exists: true },
            compose: { path: "/install/docker-compose.yml", exists: true },
          },
          docker: { available: true, server_version: "e2e" },
          containers: {
            ems: {
              found: true,
              running: true,
              name: "ems",
              image: scenario.emsImage,
              tag: scenario.emsTag,
              status: "running",
            },
            influxdb: {
              found: false,
              running: false,
              name: "influxdb",
              image: null,
              tag: null,
              status: "missing",
            },
          },
          components: {
            admin: { image: ADMIN_IMAGE, tag: "v0.9.0" },
            ems: { image: scenario.emsImage, tag: scenario.emsTag },
          },
          links: { dashboard_url: "http://localhost:8080" },
          warnings: [],
        }),
      });
    });

    await page.reload();
    await page.locator('[data-start-path="manage_existing"]').click();
    await page.locator('[data-open-maintenance-path="manual"]').click();
    await page.locator('[data-maintenance-toggle="maintenance-versions"]').click();

    await expect(page.locator("#maintenance-admin-image")).toHaveText(ADMIN_IMAGE);
    await expect(page.locator("#maintenance-ems-image")).toHaveText(
      scenario.emsImage,
    );
    await expect(page.locator("#maintenance-versions-summary")).toContainText(
      `Admin v0.9.0 · EMS ${scenario.emsTag}`,
    );
  });
}
