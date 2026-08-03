import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { type Page } from "@playwright/test";

const SECRET_SENTINEL = "e2e-super-secret-broker-password";

async function openMigration(
  page: Page,
  seedAdminScenario: (scenario: string) => Promise<void>,
  scenario = "mqtt_migration",
) {
  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  await seedAdminScenario(scenario);
  await page.reload();
  await expect(page.locator("#view-start")).toBeVisible();
  await page.locator('[data-start-path="manage_existing"]').click();
  await expect(page.locator("#maintenance-hub")).toBeVisible();
  await page.locator('[data-open-maintenance-path="manual"]').click();
  await expect(page.locator("#maintenance-manual-panel")).toBeVisible();
  await page
    .locator('[data-maintenance-toggle="maintenance-mqtt-migration"]')
    .click();
  await expect(page.locator("#maintenance-mqtt-migration-required")).toHaveText(
    "required",
  );
}

test.describe("Zendure MQTT migration", { tag: ["@maintenance"] }, () => {
  test("review and apply succeeds without rendering secrets", async ({
    page,
    seedAdminScenario,
  }) => {
    await openMigration(page, seedAdminScenario);

    const backup = page.locator("#maintenance-mqtt-migration-backup");
    const apply = page.locator("#maintenance-mqtt-migration-apply");
    await expect(backup).toBeChecked();
    await expect(apply).toBeEnabled();
    await expect(page.locator("body")).not.toContainText(SECRET_SENTINEL);

    let applyRequest: { csrf: string | null; body: unknown } | null = null;
    page.on("request", (request) => {
      if (request.url().endsWith("/zendure-mqtt/migration-apply")) {
        applyRequest = {
          csrf: request.headers()["x-csrf-token"] || null,
          body: request.postDataJSON(),
        };
      }
    });
    page.once("dialog", (dialog) => dialog.accept());
    await apply.click();

    await expect(page.locator("#maintenance-mqtt-migration-required")).toHaveText(
      "not required",
    );
    await expect(page.locator('[data-mqtt-migration-stage="validate"]')).toHaveAttribute(
      "data-state",
      "done",
    );
    expect(applyRequest).not.toBeNull();
    expect(applyRequest!.csrf).toBeTruthy();
    expect(applyRequest!.body).toMatchObject({ confirm: true, backup: true });
    await expect(page.locator("body")).not.toContainText(SECRET_SENTINEL);
  });

  test("stale review and missing CSRF are rejected", async ({
    page,
    seedAdminScenario,
  }) => {
    await openMigration(page, seedAdminScenario);

    const reviewResponse = await page.request.get(
      "/api/admin/maintenance/zendure-mqtt/migration-review",
    );
    const review = await reviewResponse.json();
    const noCsrf = await page.request.post(
      "/api/admin/maintenance/zendure-mqtt/migration-apply",
      {
        data: { revision: review.revision, confirm: true, backup: true },
      },
    );
    expect(noCsrf.status()).toBe(403);

    await seedAdminScenario("mqtt_mutate");
    page.once("dialog", (dialog) => dialog.accept());
    await page.locator("#maintenance-mqtt-migration-apply").click();
    await expect(page.locator("#maintenance-mqtt-migration-status")).toContainText(
      /review is stale/i,
    );
    await expect(page.locator("#maintenance-mqtt-migration-apply")).toBeEnabled();
  });

  test("failed backup leaves the reviewed config unchanged", async ({
    page,
    seedAdminScenario,
  }) => {
    await openMigration(page, seedAdminScenario, "mqtt_backup_failure");

    page.once("dialog", (dialog) => dialog.accept());
    await page.locator("#maintenance-mqtt-migration-apply").click();
    await expect(page.locator('[data-mqtt-migration-stage="backup"]')).toHaveAttribute(
      "data-state",
      "failed",
    );
    await expect(page.locator("#maintenance-mqtt-migration-status")).toContainText(
      /backup failure/i,
    );

    const response = await page.request.get(
      "/api/admin/maintenance/zendure-mqtt/migration-review",
    );
    const body = await response.json();
    expect(body.review.needs_migration).toBe(true);
  });
});
