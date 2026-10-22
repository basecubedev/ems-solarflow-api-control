// SPDX-License-Identifier: AGPL-3.0-or-later
// Guided Setup in the Modern Admin console, against the deterministic
// test-mode Admin whose catalogue offers Latest (aligned with the running
// Admin), v9.9.10 (needs an Admin update) and v0.7.0 (a legacy release the
// running Admin installs from its release archive).
import { expect, test, ADMIN_PASSWORD } from "./fixtures/admin";
import type { Page, Route } from "@playwright/test";
import { SHELLY_METER, apiInverter } from "./helpers/discovery-state";

test.use({ storageState: { cookies: [], origins: [] } });

async function openSetup(page: Page) {
  await page.goto("/modern/#/setup");
  await page.fill("#admin-password", ADMIN_PASSWORD);
  await page.getByTestId("sign-in-submit").click();
  await expect(page.locator(".app")).toBeVisible();
  await page.getByTestId("setup-begin").click();
  const replace = page.getByRole("dialog", { name: "Set up over the existing installation?" });
  if (await replace.isVisible().catch(() => false)) await replace.getByTestId("confirm").click();
  await expect(page.getByTestId("setup-release")).toBeVisible();
}

async function choose(page: Page, tag: string) {
  await page.getByTestId("setup-release-select").selectOption(tag);
  await expect(page.getByTestId("setup-build-message")).toHaveText("Verify the build to download and check it.");
  await page.getByTestId("setup-right").click();
}

test.describe("Modern Admin: guided setup, the System Build", { tag: ["@smoke", "@setup"] }, () => {
  test("an aligned build is verified, confirmed, and opens the next step", async ({ page }) => {
    await openSetup(page);
    await expect(page.getByTestId("setup-step-devices")).toBeDisabled();
    await choose(page, "latest");
    await expect(page.getByTestId("setup-build-message")).toHaveText("The Admin console is ready for this build.");
    await expect(page.getByTestId("setup-checks").locator("li[data-passed='yes']").first()).toBeVisible();
    const confirms: string[] = [];
    page.on("request", (request) => {
      if (request.url().includes("/api/setup/system-build/confirm")) confirms.push(request.headers()["x-setup-intent-id"] || "");
    });
    await page.getByTestId("setup-right").click();
    await expect(page.getByTestId("setup-step-devices")).toHaveAttribute("aria-selected", "true");
    expect(confirms).toHaveLength(1);
    expect(confirms[0]).not.toBe("");
    await page.getByTestId("setup-step-release").click();
    await expect(page.getByTestId("setup-pipeline").locator("li").nth(4)).toHaveAttribute("data-state", "done");
  });

  test("a build that needs a newer Admin console offers its update and nothing else", async ({ page }) => {
    await openSetup(page);
    await choose(page, "v9.9.10");
    await expect(page.getByTestId("setup-build-message")).toHaveText("The Admin console has to be updated before you can continue.");
    await expect(page.getByTestId("setup-left")).toBeEnabled();
    await expect(page.getByTestId("setup-left")).toHaveText("Update the Admin console");
    await expect(page.getByTestId("setup-right")).toBeDisabled();
  });

  test("a legacy release continues with the running Admin console, its resources from the release archive", async ({ page }) => {
    await openSetup(page);
    await choose(page, "v0.7.0");
    await expect(page.getByTestId("setup-build-message")).toContainText("older EMS version");
    await expect(page.getByTestId("setup-checks")).toContainText("Resources from the verified release archive");
    await expect(page.getByTestId("setup-right")).toBeEnabled();
  });

  test("starting over returns to the first step under a new workflow", async ({ page }) => {
    await openSetup(page);
    const before = await page.evaluate(() => JSON.parse(localStorage.getItem("ems-admin-setup-workflow") || "{}").workflow_id);
    await page.getByTestId("setup-restart").click();
    await page.getByRole("dialog", { name: "Start the guided setup over?" }).getByTestId("confirm").click();
    await expect(page.getByTestId("setup-release")).toBeVisible();
    await expect.poll(() => page.evaluate(() => JSON.parse(localStorage.getItem("ems-admin-setup-workflow") || "{}").workflow_id)).not.toBe(before);
  });
});

async function confirmLatest(page: Page) {
  await choose(page, "latest");
  await expect(page.getByTestId("setup-build-message")).toHaveText("The Admin console is ready for this build.");
  await page.getByTestId("setup-right").click();
  await expect(page.getByTestId("setup-step-devices")).toHaveAttribute("aria-selected", "true");
}

test.describe("Modern Admin: guided setup, refusals", { tag: ["@setup"] }, () => {
  test("an existing installation is set up over only after a confirmation", async ({ page }) => {
    const asked: boolean[] = [];
    await page.route("**/api/admin/start-path", async (route: Route) => {
      const body = route.request().postDataJSON();
      asked.push(Boolean(body.confirm));
      if (!body.confirm) return route.fulfill({ status: 409, json: { ok: false, requires_confirmation: true, message: "An existing installation was found." } });
      return route.continue();
    });
    await page.goto("/modern/#/setup");
    await page.fill("#admin-password", ADMIN_PASSWORD);
    await page.getByTestId("sign-in-submit").click();
    await page.getByTestId("setup-begin").click();
    await page.getByRole("dialog", { name: "Set up over the existing installation?" }).getByTestId("confirm").click();
    await expect(page.getByTestId("setup-release")).toBeVisible();
    expect(asked).toEqual([false, true]);
  });

  test("a registry rate limit while verifying can be checked again", async ({ page }) => {
    await page.route("**/api/admin/system-alignment/validate", (route: Route) =>
      route.fulfill({ status: 429, json: { ok: false, error: "system_build_registry_rate_limited", action_state: { terminal_error: { code: "system_build_registry_rate_limited", message: "The container registry limited the requests." } } } }),
    );
    await openSetup(page);
    await choose(page, "latest");
    await expect(page.getByTestId("setup-left")).toHaveText("Check again");
    await expect(page.getByTestId("setup-left")).toBeEnabled();
  });
});

test.describe("Modern Admin: guided setup, devices to start", { tag: ["@setup"] }, () => {
  test("what the search finds reaches the settings, which the server checks and saves for the deployment", async ({ page, seedDiscoveryState }) => {
    await openSetup(page);
    await seedDiscoveryState({ local_api_devices: [SHELLY_METER, apiInverter("SNE2EMODERN1", "192.168.100.21")] });
    await confirmLatest(page);

    const unified = page.getByTestId("setup-unified");
    await expect(unified.locator("li[data-role='grid_meter']")).toHaveCount(1);
    await expect(unified.locator("li[data-role='inverter']")).toHaveCount(1);
    await expect(page.getByTestId("setup-verdict")).toContainText("Step 02 of 05 · Find your devices");

    const plans: string[] = [];
    page.on("request", (request) => {
      if (request.url().includes("/api/setup/device-plan")) plans.push(request.url());
    });
    await page.getByTestId("setup-next").click();
    await expect(page.getByTestId("setup-step-config")).toHaveAttribute("aria-selected", "true");
    const hardware = page.getByTestId("setup-hardware");
    await expect(hardware.locator("[data-device]")).toHaveCount(2);
    await expect(hardware).toContainText("Chosen on its own");
    await expect(page.getByTestId("setup-inverters")).toContainText("INV_1");
    await expect(page.getByTestId("setup-verdict-state")).toHaveText("Ready");
    expect(plans.length).toBeGreaterThan(0);
    await expect(page.getByTestId("setup-safety").locator('[data-path="system.allow_hardware_writes"] input')).toBeChecked();

    const stored = await page.evaluate(() => JSON.parse(localStorage.getItem("ems-admin-config-draft") || "{}"));
    expect(stored.schema_version).toBe(1);
    expect(stored.items.map((item: { role: string }) => item.role).sort()).toEqual(["grid_meter", "inverter"]);

    await page.getByTestId("setup-next").click();
    await expect(page.getByTestId("setup-step-deployment")).toHaveAttribute("aria-selected", "true");
    await expect(page.getByTestId("setup-deployment")).toContainText("Saved");
  });

  test("a removed inverter stays removed, and the draft can be cleared", async ({ page, seedDiscoveryState }) => {
    await openSetup(page);
    await seedDiscoveryState({ local_api_devices: [SHELLY_METER, apiInverter("SNE2EMODERN2", "192.168.100.22")] });
    await confirmLatest(page);
    await expect(page.getByTestId("setup-unified").locator("li[data-role='inverter']")).toHaveCount(1, { timeout: 60_000 });
    await page.getByTestId("setup-step-config").click();
    const inverters = page.getByTestId("setup-inverters");
    await expect(inverters.locator("[data-device]")).toHaveCount(1);
    await expect(page.getByTestId("setup-validation")).not.toHaveAttribute("data-tone", "pending");
    await inverters.getByTestId("device-remove").click();
    await expect(page.getByText("No inverters in the draft yet.")).toBeVisible();
    await page.getByTestId("setup-step-devices").click();
    await page.getByTestId("setup-step-config").click();
    await expect(page.getByText("No inverters in the draft yet.")).toBeVisible();

    await page.getByTestId("setup-add-more").locator("summary").first().click();
    await page.getByTestId("setup-add-more").locator("li[data-role='inverter']").getByTestId("setup-add-device").click();
    await expect(page.getByTestId("setup-inverters").locator("[data-device]")).toHaveCount(1);
    await expect(page.getByTestId("setup-validation")).not.toHaveAttribute("data-tone", "pending");

    await page.getByTestId("setup-clear-draft").click();
    await page.getByRole("dialog", { name: "Clear the setup draft?" }).getByTestId("confirm").click();
    await expect(page.getByText("No inverters in the draft yet.")).toBeVisible();
    await expect(page.getByTestId("setup-hardware")).not.toContainText("Chosen on its own");
  });

  test("the prepared deployment starts, a stopped container in its way is removed first, and the dashboard opens", async ({ page }) => {
    let started = 0;
    await page.route("**/api/setup/config/status", (route: Route) => route.fulfill({ json: { exists: true, path: "/data/generated/config.json" } }));
    await page.route("**/api/setup/deployment/plan", (route: Route) =>
      route.fulfill({ json: { prepared: true, can_prepare: true, workspace: "/srv/ems", generated_config: { ready: true, path: "/data/generated/config.json" }, docker: { state: "ready", mode: "deployment_controller", server_version: "27.0", message: "Docker is available." }, images: [{ service: "ems", image: "ghcr.io/x/ems:latest" }] } }),
    );
    await page.route("**/api/setup/deployment/status", (route: Route) => route.fulfill({ json: { prepared: true, running: started > 1, services: [], errors: [], dashboard_url: "http://localhost:8080" } }));
    await page.route("**/api/setup/deployment/start", (route: Route) => {
      started += 1;
      return route.fulfill({ json: { ok: true, job_id: `job-${started}`, status: "running", steps: [{ label: "Start containers", status: "running" }] } });
    });
    await page.route("**/api/setup/deployment/start/jobs/*", (route: Route) =>
      route.fulfill({
        json: started === 1
          ? { job_id: "job-1", status: "failed", error: { code: "container_conflict", message: "A container blocks the start." }, conflict: { container_name: "ems-solarflow", image: "old:1", selected_image: "ghcr.io/x/ems:latest", status: "exited", safe_fix_available: true } }
          : { job_id: "job-2", status: "succeeded", steps: [{ label: "Start containers", status: "done" }], dashboard_url: "http://localhost:8080" },
      }),
    );
    let resolved: Record<string, unknown> | null = null;
    await page.route("**/api/setup/deployment/resolve-container-conflict", async (route: Route) => {
      resolved = route.request().postDataJSON();
      await route.fulfill({ json: { ok: true, continue: true, conflict: null } });
    });

    await openSetup(page);
    await confirmLatest(page);
    await page.getByTestId("setup-step-deployment").click();
    await expect(page.getByTestId("setup-deploy-status")).toHaveText("The deployment is ready. Continue to start EMS.");
    await page.getByTestId("setup-next").click();
    await expect(page.getByTestId("setup-step-start")).toHaveAttribute("aria-selected", "true");

    await page.getByTestId("setup-start-ems").click();
    const conflict = page.getByTestId("setup-container-conflict");
    await expect(conflict).toContainText("An EMS container already exists");
    await expect(page.getByTestId("setup-start-ems")).toBeDisabled();
    await conflict.getByTestId("setup-conflict-resolve").click();
    await expect(page.getByTestId("setup-start-success")).toBeVisible();
    expect(resolved).toMatchObject({ container_name: "ems-solarflow", action: "remove_stopped_and_continue" });
    const href = await page.getByTestId("setup-start-success").getByRole("link").getAttribute("href");
    expect(href).toMatch(/^http:\/\/[^/]+:8080\/$/);
  });
});

test.describe("Modern Admin: guided setup, a stuck transition", { tag: ["@setup"] }, () => {
  test("is resumed or discarded from the first step, the discard after a confirmation", async ({ page }) => {
    const transition = { mode: "fresh_install", stage: "failed_recoverable", system_tag: "latest", operation_id: "op-stuck", resume_available: true, cancel_available: true, worker_active: false, worker_status_available: true };
    await page.route("**/api/admin/system-alignment/status", async (route: Route) => {
      const response = await route.fetch();
      const data = await response.json();
      await route.fulfill({ json: { ...data, transition } });
    });
    const abandons: unknown[] = [];
    await page.route("**/api/setup/abandon", async (route: Route) => {
      abandons.push(route.request().postDataJSON());
      await route.fulfill({ json: { ok: true } });
    });
    await page.goto("/modern/#/setup");
    await page.fill("#admin-password", ADMIN_PASSWORD);
    await page.getByTestId("sign-in-submit").click();
    const recovery = page.getByTestId("setup-recovery");
    await expect(recovery).toContainText("Resume it, or discard the setup");
    await expect(recovery.getByTestId("setup-recovery-resume")).toBeEnabled();
    await recovery.getByTestId("setup-recovery-discard").click();
    await page.getByRole("dialog", { name: "Discard this setup?" }).getByTestId("confirm").click();
    await expect.poll(() => abandons.length).toBe(1);
  });
});
