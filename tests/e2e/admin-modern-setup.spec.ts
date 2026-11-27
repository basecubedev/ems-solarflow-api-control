// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
// Guided Setup in the Modern Admin console, against the deterministic
// test-mode Admin whose catalogue offers Latest (aligned with the running
// Admin), v9.9.10 (needs an Admin update) and v0.7.0 (a legacy release the
// running Admin installs from its release archive).
import { expect, test, ADMIN_PASSWORD } from "./fixtures/admin";
import type { Page, Route } from "@playwright/test";
import { SHELLY_METER, apiInverter, cloudInverter, setDiscoveryPriority } from "./helpers/discovery-state";


async function openSetup(page: Page) {
  await page.goto("/#/setup");
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

async function atSwitchableInverter(page: Page, seed: (state: Record<string, unknown>) => Promise<void>) {
  await page.route("**/api/discovery/zendure-cloud-mqtt/settings", (route: Route) =>
    route.fulfill({ json: { token_saved: true, broker: "mqtt.zen-iot.com", tls_mode: "system_ca" } }),
  );
  await page.goto("/#/status");
  await page.fill("#admin-password", ADMIN_PASSWORD);
  await page.getByTestId("sign-in-submit").click();
  await expect(page.locator(".app")).toBeVisible();
  await seed({ local_api_devices: [apiInverter("SNE2ESWITCH1", "192.168.100.78"), SHELLY_METER], cloud_devices: [cloudInverter("SNE2ESWITCH1")] });
  await setDiscoveryPriority(page, ["local_api", "local_mqtt", "zendure_mqtt"]);
  await page.goto("/#/setup");
  await page.reload();
  await page.getByTestId("setup-begin").click();
  await expect(page.getByTestId("setup-release")).toBeVisible();
  await confirmLatest(page);
  await expect(page.getByTestId("setup-unified").locator("li[data-role='inverter']")).not.toHaveCount(0, { timeout: 60_000 });
  await page.getByTestId("setup-next").click();
  await expect(page.getByTestId("setup-step-config")).toHaveAttribute("aria-selected", "true");
  const inverters = page.getByTestId("setup-inverters");
  await expect(inverters.locator("[data-device]")).toHaveCount(1);
  await expect(inverters).toContainText("API");
  return inverters;
}

async function useOtherConnection(page: Page) {
  const use = page.getByTestId("setup-use-connection").first();
  if (!(await use.isVisible())) await page.getByTestId("setup-add-more").locator("summary").first().click();
  await use.click();
}

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
    await page.goto("/#/setup");
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
    await page.getByTestId("setup-fold-safety").getByRole("button", { name: "Control and safety" }).click();
    await expect(page.getByTestId("setup-safety").locator('[data-path="system.allow_hardware_writes"] input')).toBeChecked();
    const inverter = page.getByTestId("setup-inverters").locator("[data-device]").first();
    await inverter.locator(".summary").click();
    await inverter.locator("details").filter({ hasText: "Advanced settings" }).locator("summary").click();
    await expect(inverter.getByTestId("pv-input-2")).toBeChecked();
    await inverter.getByTestId("pv-input-2").uncheck();
    await expect(inverter.getByTestId("pv-input-2")).not.toBeChecked();

    const stored = await page.evaluate(() => JSON.parse(localStorage.getItem("ems-admin-config-draft") || "{}"));
    expect(stored.schema_version).toBe(1);
    expect(stored.items.map((item: { role: string }) => item.role).sort()).toEqual(["grid_meter", "inverter"]);
    expect(stored.items.find((item: { role: string }) => item.role === "inverter").config_values.unused_pv_inputs).toEqual([2]);

    await page.getByTestId("setup-next").click();
    await expect(page.getByTestId("setup-step-deployment")).toHaveAttribute("aria-selected", "true");
    await expect(page.getByTestId("setup-deployment")).toContainText("Saved");
  });

  test("a check the server refuses for a stale device plan withdraws the check, and checks again only on a new plan", async ({ page, seedDiscoveryState }) => {
    await openSetup(page);
    await seedDiscoveryState({ local_api_devices: [SHELLY_METER, apiInverter("SNE2EMODERN1", "192.168.100.21")] });
    await confirmLatest(page);
    await expect(page.getByTestId("setup-discovery-run")).toContainText("Search again");
    await expect(page.getByTestId("setup-unified").locator("li[data-role='inverter']")).toHaveCount(1);
    const order: string[] = [];
    let refused = false;
    await page.route("**/api/setup/config-preview", async (route: Route) => {
      order.push("preview");
      if (refused) return route.continue();
      refused = true;
      return route.fulfill({ status: 409, json: { ok: false, error: "stale_device_plan", message: "The device plan changed." } });
    });
    page.on("request", (request) => {
      if (request.url().includes("/api/setup/device-plan") && request.method() === "POST") order.push("plan");
    });
    await page.getByTestId("setup-next").click();
    await expect(page.getByTestId("setup-step-config")).toHaveAttribute("aria-selected", "true");
    await expect.poll(() => order.filter((step) => step === "preview").length).toBeGreaterThan(0);
    await expect(page.getByTestId("setup-next")).toBeDisabled();
    const after = order.slice(order.indexOf("preview") + 1);
    if (after.includes("preview")) expect(after.slice(0, after.indexOf("preview"))).toContain("plan");
  });

  test("an inverter moves to its other connection and stays switched off", async ({ page, seedDiscoveryState }) => {
    test.setTimeout(90_000);
    const inverters = await atSwitchableInverter(page, seedDiscoveryState);
    await inverters.getByRole("button", { name: /^Inverter 1 / }).click();
    await inverters.getByRole("checkbox", { name: "On" }).uncheck();
    await expect(inverters.getByRole("checkbox", { name: "On" })).not.toBeChecked();
    const planned = page.waitForResponse((response) => response.url().includes("/api/setup/device-plan") && Boolean(response.request().postData()?.includes('"switch"')));
    await useOtherConnection(page);
    await planned;
    await expect(inverters.locator("[data-device]")).toHaveCount(1);
    await expect(inverters).not.toContainText("API");
    await inverters.getByRole("button", { name: /^Inverter 1 / }).click();
    await expect(inverters.getByRole("checkbox", { name: "On" })).not.toBeChecked();
  });

  test("an inverter keeps its connection until the plan answers the switch, then moves and stays moved", async ({ page, seedDiscoveryState }) => {
    test.setTimeout(90_000);
    const inverters = await atSwitchableInverter(page, seedDiscoveryState);
    let release: () => void = () => {};
    const held = new Promise<void>((resolve) => (release = resolve));
    let asked = false;
    await page.route("**/api/setup/device-plan", async (route: Route) => {
      if (route.request().postData()?.includes('"switch"')) {
        asked = true;
        await held;
      }
      await route.continue();
    });
    await useOtherConnection(page);
    await expect.poll(() => asked).toBe(true);
    await expect(inverters).toContainText("API");
    await expect(inverters.locator("[data-device]")).toHaveCount(1);
    release();
    await expect(inverters).not.toContainText("API", { timeout: 15_000 });
    await expect(inverters.locator("[data-device]")).toHaveCount(1);
    await page.reload();
    await expect(page.getByTestId("setup-inverters").locator("[data-device]")).toHaveCount(1, { timeout: 15_000 });
    await expect(page.getByTestId("setup-inverters")).not.toContainText("API");
  });

  test("a removed inverter stays removed, and the draft can be cleared", async ({ page, seedDiscoveryState }) => {
    test.setTimeout(90_000);
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
    expect(href).toBe("#/overview");
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
    await page.goto("/#/setup");
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

test.describe("Modern Admin: guided setup, a page that fits the screen", { tag: ["@setup"] }, () => {
  test.use({ viewport: { width: 1366, height: 768 } });

  const folded = (page: Page, id: string) => page.getByTestId(`setup-fold-${id}`).locator(".card-fold");
  const summary = (page: Page, id: string) => page.getByTestId(`setup-fold-${id}`).locator(".card-summary");

  async function devicesStep(page: Page) {
    await openSetup(page);
    await confirmLatest(page);
  }

  async function settingsStep(page: Page) {
    await devicesStep(page);
    await page.getByTestId("setup-next").click();
    await expect(page.getByTestId("setup-step-config")).toHaveAttribute("aria-selected", "true");
  }

  test("the optional cards of the search are folded with their state, open on a click, and Next stays in view", async ({ page }) => {
    await devicesStep(page);

    for (const id of ["networks", "manual", "details", "sources", "broker", "mapping"]) await expect(folded(page, id)).toHaveAttribute("aria-expanded", "false");
    await expect(summary(page, "networks")).toHaveText(/^\d+ networks? · \d+ through a router$/);
    await expect(summary(page, "sources")).toHaveText("0 broker credentials · no key saved · 0 bridge passwords");
    await expect(summary(page, "broker")).toHaveText("Off");
    await expect(summary(page, "mapping")).toHaveText("0 mapped devices");
    await expect(page.getByTestId("setup-manual-scan")).toBeHidden();
    await expect(page.getByTestId("setup-next")).toBeInViewport();

    await page.getByTestId("setup-fold-manual").getByRole("button", { name: "Scan one network" }).click();
    await expect(folded(page, "manual")).toHaveAttribute("aria-expanded", "true");
    await expect(page.getByTestId("setup-manual-scan")).toBeVisible();
    await expect(summary(page, "manual")).toHaveCount(0);

    const counts = (await summary(page, "details").innerText()).match(/\d+/g);
    await page.getByTestId("setup-sources").locator("li[data-source='local_api']").getByRole("button", { name: "Configure" }).click();
    await page.getByRole("button", { name: "Show the results" }).click();
    await expect(folded(page, "details")).toHaveAttribute("aria-expanded", "true");
    await expect(page.locator("#setup-detail-local_api")).toHaveJSProperty("open", true);
    await expect(page.locator("#setup-detail-local_api > summary")).toContainText(`(${counts![0]} device`);
    await expect(page.locator("#setup-detail-local_mqtt > summary")).toContainText(`· ${counts![1]} device`);
    await expect(page.locator("#setup-detail-zendure_mqtt > summary")).toContainText(`(${counts![2]} device`);
  });

  test("a step without a network found, or a broker with an issue, opens that card on its own", async ({ page }) => {
    await page.route("**/api/discovery/networks", (route) => route.fulfill({ json: { networks: [], warnings: [] } }));
    await page.route(/\/api\/discovery\/mqtt-broker$/, (route) => route.request().method() === "GET"
      ? route.fulfill({ json: { configured: true, issues: [{ code: "mqtt_broker_port_in_use", message: "port in use" }], running: false, host_address: "192.0.2.40", settings: { schema_version: 1, enabled: true, port: 1883, anonymous: false, accounts: [] }, accounts: [], containers: {} } })
      : route.fallback());
    await devicesStep(page);

    await expect(folded(page, "networks")).toHaveAttribute("aria-expanded", "true");
    await expect(folded(page, "manual")).toHaveAttribute("aria-expanded", "true");
    await expect(page.getByTestId("setup-manual-scan")).toBeVisible();
    await expect(folded(page, "broker")).toHaveAttribute("aria-expanded", "true");
    await expect(folded(page, "sources")).toHaveAttribute("aria-expanded", "false");
  });

  test("a network warning opens the networks and the scan by hand on their own", async ({ page }) => {
    await page.route("**/api/discovery/networks", (route) => route.fulfill({ json: {
      networks: [{ cidr: "10.20.0.0/24", original_cidr: "10.20.0.0/24", interface: "eth9", address: "10.20.0.5", is_docker_like: false, scan_recommended: false, label: "Other network", reason: "Not the default route" }],
      warnings: ["No obvious LAN network was found; enter your LAN CIDR manually."],
    } }));
    await devicesStep(page);

    await expect(folded(page, "networks")).toHaveAttribute("aria-expanded", "true");
    await expect(page.getByTestId("setup-networks")).toContainText("enter your LAN CIDR manually");
    await expect(folded(page, "manual")).toHaveAttribute("aria-expanded", "true");
    await expect(page.getByTestId("setup-manual-scan")).toBeVisible();
  });

  test("the safety and advanced settings are folded with what they say, and a value away from its default opens its card on its own", async ({ page }) => {
    await settingsStep(page);

    await expect(folded(page, "safety")).toHaveAttribute("aria-expanded", "false");
    await expect(folded(page, "advanced")).toHaveAttribute("aria-expanded", "false");
    await expect(summary(page, "safety")).toHaveText(/^EMS controls · writes to devices allowed · max 800\sW · per device 800\sW · min 35\sW$/);
    await expect(summary(page, "advanced")).toHaveText("All at their defaults");
    await expect(page.getByTestId("setup-next")).toBeInViewport();

    await folded(page, "safety").click();
    await page.getByTestId("setup-safety").locator('[data-path="system.dry_run"] input').check();
    await page.getByTestId("setup-safety").locator('[data-path="system.allow_mqtt_zendure_control_writes"] input').uncheck();
    await expect(folded(page, "advanced")).toHaveAttribute("aria-expanded", "false");
    await folded(page, "safety").click();
    await expect(summary(page, "safety")).toHaveText(/^EMS controls · writes only over Local API and Local MQTT · dry run · max 800\sW · per device 800\sW · min 35\sW$/);

    await page.getByRole("button", { name: "Back" }).click();
    await expect(page.getByTestId("setup-step-devices")).toHaveAttribute("aria-selected", "true");
    await page.getByTestId("setup-next").click();
    await expect(page.getByTestId("setup-step-config")).toHaveAttribute("aria-selected", "true");
    await expect(folded(page, "safety")).toHaveAttribute("aria-expanded", "true");
    await expect(page.getByTestId("setup-safety").locator('[data-path="system.dry_run"] input')).toBeChecked();
    await expect(folded(page, "advanced")).toHaveAttribute("aria-expanded", "false");
  });

  test("a field the keyboard reaches is never hidden under the bar with Next", async ({ page }) => {
    await settingsStep(page);
    await folded(page, "safety").click();
    await folded(page, "advanced").click();
    const target = page.getByTestId("setup-safety").locator('[data-path="system.simulation_mode"] input');
    await page.evaluate(() => window.scrollTo(0, 0));
    await folded(page, "safety").focus();
    for (let step = 0; step < 40 && !(await target.evaluate((node) => node === document.activeElement)); step += 1) await page.keyboard.press("Tab");
    await expect(target).toBeFocused();
    const field = await target.boundingBox();
    const bar = await page.locator(".step-nav").boundingBox();
    expect(field!.y + field!.height).toBeLessThanOrEqual(bar!.y);
  });

  test("on a phone the bar with Next sits right above the menu bar", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await devicesStep(page);

    await expect(page.getByTestId("setup-next")).toBeInViewport();
    const bar = await page.locator(".step-nav").boundingBox();
    const menu = await page.locator(".bottom-nav").boundingBox();
    expect(bar && menu).toBeTruthy();
    expect(Math.round(bar!.y + bar!.height)).toBe(Math.round(menu!.y));
  });
});
