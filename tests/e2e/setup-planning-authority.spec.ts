import { type Page, type Route } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";

// Guided Setup decides nothing about a device: POST /api/setup/device-plan does,
// from the server's own discovery state. These journeys are the operator-visible
// consequences of that boundary being real.
//
// * a card the browser invents never becomes a configured device;
// * a persisted entry nothing current confirms stays unresolved and untouched;
// * a transport change that would cost output control is a question, not an act;
// * and the plan that answered all of this is what Config Preview and Apply are
//   bound to, so a stale one blocks the write instead of silently changing it.

const SEEDED_SERIAL = "E2ESETUPSN0001";
const SEEDED_IP = "192.168.90.40";
const OTHER_SERIAL = "E2EPLANOTHER01";
const SHARED_DISPLAY = "SolarFlow 800 Pro 2";

function json(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

// Add cards to what the *browser* is told discovery found, on top of what the
// server actually serves. That is exactly the surface a forged card arrives on:
// the page renders it, and nothing behind the page has ever seen it.
async function injectBrowserCards(page: Page, extra: unknown[]) {
  const served = await page.request.get("/api/discovery/devices");
  const body = await served.json();
  const devices = [...(body.devices || []), ...extra];
  await page.route("**/api/discovery/devices", (route) =>
    json(route, { devices, ignored_devices: [] }),
  );
  await page.route("**/api/discovery/mdns/status", (route) =>
    json(route, { state: "enabled", message: "", devices_found: devices.length }),
  );
}

function forgedInverter(overrides: Record<string, unknown>) {
  return {
    role_suggestion: "inverter",
    port: 80,
    api_family: "zendure_local_http",
    device_type: "zendure_solarflow_800_pro2",
    display_name: SHARED_DISPLAY,
    model: SHARED_DISPLAY,
    verified: true,
    usable_for_config: true,
    config_ready: true,
    identity_status: "confirmed",
    ...overrides,
  };
}

async function seedLegacyDraft(page: Page, items: unknown[]) {
  await page.addInitScript((draft) => {
    const marker = "__ems_plan_authority_seeded";
    if (window.sessionStorage.getItem(marker)) return;
    window.sessionStorage.setItem(marker, "1");
    // Deliberately the *unversioned* legacy shape: a bare array.
    window.localStorage.setItem("ems-admin-config-draft", JSON.stringify(draft));
  }, items);
}

async function signIn(page: Page) {
  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
}

/** Walk an already-signed-in page from the start screen to Config. */
async function reachConfig(page: Page) {
  const setup = new SetupPage(page);
  await setup.chooseFreshInstall();
  await setup.selectBuild("latest");
  await expect(setup.continueButton).toBeEnabled();
  await setup.continueToDevices();
  await page.locator('[data-setup-step="config"]').click();
}

function draftInverterCards(page: Page) {
  return page.locator("#config-draft-list .hardware-card-inverter");
}

function switchConfirmations(page: Page) {
  return page.locator("#config-switch-confirmations .config-switch-confirmation");
}

async function devicePlan(page: Page, state: unknown = {}, candidates?: unknown) {
  const status = await page.request.get("/api/admin/auth/status");
  const auth = await status.json();
  const response = await page.request.post("/api/setup/device-plan", {
    headers: { "X-CSRF-Token": auth.csrf_token as string },
    data: candidates ? { state, candidates } : { state },
  });
  expect(response.ok()).toBeTruthy();
  return response.json();
}

test("Fresh Setup: an invented discovery card never enters the config", async ({
  page,
  seedLocalApiDevices,
}) => {
  test.setTimeout(90_000);
  await signIn(page);
  // The backend knows exactly one inverter.
  await seedLocalApiDevices([{ ip: SEEDED_IP, serial: SEEDED_SERIAL }]);
  // The browser is additionally told about one that does not exist, carrying
  // every trust flag and well-formed issued ids.
  await injectBrowserCards(page, [
    forgedInverter({
      observation_id: "obs:v1:E2EFORGEDCARD",
      physical_device_id: "opaque:v1:E2EFORGEDPHYS",
      connection_id: "conn:v1:E2EFORGEDCONN",
      serial_number: OTHER_SERIAL,
      ip: "192.168.99.99",
    }),
  ]);
  await page.reload();
  await reachConfig(page);

  // The plan the browser itself asked for: it submitted the forged card's
  // handle beside the real one, and only the real one came back.
  const plan = await devicePlan(page, {}, {
    observations: [
      { observation_id: "obs:v1:E2EFORGEDCARD", observation_ref: "forged-card" },
    ],
  });
  expect(
    plan.observations.map((entry: Record<string, unknown>) => entry.observation_id),
  ).not.toContain("obs:v1:E2EFORGEDCARD");
  expect(plan.unresolved_references).toContainEqual({
    kind: "observation",
    handle: "obs:v1:E2EFORGEDCARD",
  });
  // Only the one real device is configured; the forged card contributes
  // nothing at all.
  await expect(draftInverterCards(page)).toHaveCount(1);
  await expect(page.locator("#config-draft-list")).not.toContainText(
    "192.168.99.99",
  );
});

test("Fresh Setup: a legacy draft nothing confirms stays unresolved", async ({
  page,
}) => {
  test.setTimeout(90_000);
  // Nothing is seeded, so no trusted candidate matches the stored serial.
  await seedLegacyDraft(page, [
    {
      source_id: "zendure:" + OTHER_SERIAL,
      role: "inverter",
      config_name: "INV_LEGACY",
      display_name: SHARED_DISPLAY,
      enabled: true,
      serial_number: OTHER_SERIAL,
      ip: "192.168.100.77",
      port: 80,
    },
  ]);
  await signIn(page);
  await reachConfig(page);

  // Preserved with its values — never dropped, never merged, never identified.
  await expect(draftInverterCards(page)).toHaveCount(1);
  await expect(page.locator("#config-draft-list")).toContainText("INV_LEGACY");
  const plan = await devicePlan(page, {
    draft_items: [
      {
        draft_item_id: "item-legacy-0",
        role: "inverter",
        serial_number: OTHER_SERIAL,
        ip: "192.168.100.77",
        port: 80,
      },
    ],
  });
  expect(plan.draft_items[0].legacy_match).toBe("unmatched");
  expect(plan.draft_items[0].physical_device_id).toBeNull();
  expect(plan.operations.drop_draft_items).toEqual([]);
});

test("Fresh Setup: a legacy draft the server confirms is rehydrated", async ({
  page,
  seedLocalApiDevices,
}) => {
  test.setTimeout(90_000);
  await signIn(page);
  await seedLocalApiDevices([{ ip: SEEDED_IP, serial: SEEDED_SERIAL }]);

  const plan = await devicePlan(page, {
    draft_items: [
      {
        draft_item_id: "item-legacy-0",
        source_id: "zendure:" + SEEDED_SERIAL,
        role: "inverter",
        serial_number: SEEDED_SERIAL,
        ip: SEEDED_IP,
        port: 80,
      },
    ],
  });

  expect(plan.draft_items[0].legacy_match).toBe("matched");
  expect(plan.draft_items[0].physical_device_id).toBe(
    plan.observations[0].physical_device_id,
  );
  // The stored entry *is* the live observation, so nothing is adopted beside it.
  expect(plan.operations.adopt_observations).toEqual([]);
});

/** A workflow plus the scalar-broker scenario the capability cases need. */
async function reachCapabilityCase(
  page: Page,
  seedAdminScenario: (scenario: string) => Promise<void>,
): Promise<string> {
  await signIn(page);
  await seedAdminScenario("setup_api_and_local_scalar");
  const status = await page.request.get("/api/admin/auth/status");
  const auth = await status.json();
  const started = await page.request.post("/api/admin/start-path", {
    headers: { "X-CSRF-Token": auth.csrf_token as string },
    data: { choice: "setup_new", confirm: true },
  });
  expect(started.ok()).toBeTruthy();
  // Local MQTT first: the scalar broker becomes the preferred connection.
  const saved = await page.request.post("/api/discovery/preparation", {
    headers: { "X-CSRF-Token": auth.csrf_token as string },
    data: { discovery_priority: ["local_mqtt", "zendure_mqtt", "local_api"] },
  });
  expect(saved.ok()).toBeTruthy();
  return (await started.json()).setup_workflow_id as string;
}

const CONFIGURED_DRAFT = {
  draft_items: [
    {
      draft_item_id: "item-1",
      role: "inverter",
      serial_number: SEEDED_SERIAL,
      ip: SEEDED_IP,
      port: 80,
      auto_added: true,
    },
  ],
};

test("Fresh Setup: a capability-losing priority switch waits for an answer", async ({
  page,
  seedAdminScenario,
}) => {
  test.setTimeout(120_000);
  const workflowId = await reachCapabilityCase(page, seedAdminScenario);
  const plan = await devicePlan(page, CONFIGURED_DRAFT);

  expect(plan.confirmation_required).toBe(true);
  expect(plan.confirmations[0].control_continuity).toBe("lost");
  // The switch is described, never performed.
  expect(plan.operations.drop_draft_items).toEqual([]);
  expect(plan.proposed_operations.drop_draft_items).toEqual(["item-1"]);

  // Config Preview refuses to turn a plan that is still asking into config.
  const auth = await (await page.request.get("/api/admin/auth/status")).json();
  const preview = await page.request.post("/api/setup/config-preview", {
    headers: { "X-CSRF-Token": auth.csrf_token as string },
    data: {
      devices: [],
      setup_workflow_id: workflowId,
      device_plan_id: plan.plan_id,
    },
  });
  expect(preview.status()).toBe(409);
  expect((await preview.json()).error).toBe("device_plan_confirmation_required");
});

test("Fresh Setup: confirming the switch makes its operations executable", async ({
  page,
  seedAdminScenario,
}) => {
  test.setTimeout(120_000);
  await reachCapabilityCase(page, seedAdminScenario);
  const proposed = await devicePlan(page, CONFIGURED_DRAFT);
  const token = proposed.confirmations[0].token;

  const auth = await (await page.request.get("/api/admin/auth/status")).json();
  const confirmed = await page.request.post("/api/setup/device-plan", {
    headers: { "X-CSRF-Token": auth.csrf_token as string },
    data: { state: CONFIGURED_DRAFT, confirmed_switches: [token] },
  });
  const answered = await confirmed.json();

  expect(answered.confirmation_required).toBe(false);
  expect(answered.operations.drop_draft_items).toEqual(["item-1"]);
});

test("Fresh Setup: the confirmation is shown before anything changes", async ({
  page,
  seedAdminScenario,
}) => {
  test.setTimeout(120_000);
  await seedLegacyDraft(page, [
    {
      draft_item_id: "item-1",
      source_id: "zendure:" + SEEDED_SERIAL,
      role: "inverter",
      config_name: "INV_1",
      display_name: SHARED_DISPLAY,
      enabled: true,
      serial_number: SEEDED_SERIAL,
      ip: SEEDED_IP,
      port: 80,
      auto_added: true,
    },
  ]);
  await reachCapabilityCase(page, seedAdminScenario);
  await page.reload();
  await reachConfig(page);

  await expect(switchConfirmations(page)).toHaveCount(1);
  await expect(switchConfirmations(page)).toContainText(
    /output control would be lost/i,
  );
  // The inverter is still there, still on its API connection.
  await expect(draftInverterCards(page)).toHaveCount(1);
});

test("Fresh Setup: a superseded device plan cannot preview or apply", async ({
  page,
  seedLocalApiDevices,
}) => {
  test.setTimeout(120_000);
  await signIn(page);
  await seedLocalApiDevices([{ ip: SEEDED_IP, serial: SEEDED_SERIAL }]);
  const auth = await (await page.request.get("/api/admin/auth/status")).json();
  const started = await page.request.post("/api/admin/start-path", {
    headers: { "X-CSRF-Token": auth.csrf_token as string },
    data: { choice: "setup_new", confirm: true },
  });
  const workflowId = (await started.json()).setup_workflow_id;
  const stale = (await devicePlan(page)).plan_id;

  // Discovery moves on: another inverter appears.
  await seedLocalApiDevices([{ ip: "192.168.90.41", serial: OTHER_SERIAL }]);

  const preview = await page.request.post("/api/setup/config-preview", {
    headers: { "X-CSRF-Token": auth.csrf_token as string },
    data: {
      devices: [],
      setup_workflow_id: workflowId,
      device_plan_id: stale,
    },
  });
  expect(preview.status()).toBe(409);
  expect((await preview.json()).error).toBe("stale_device_plan");

  const applied = await page.request.post("/api/setup/config/apply", {
    headers: { "X-CSRF-Token": auth.csrf_token as string },
    data: {
      devices: [],
      setup_workflow_id: workflowId,
      config_preview_id: "forged-preview",
      device_plan_id: stale,
    },
  });
  expect(applied.status()).toBe(409);
});

// --- a plan authorizes one draft, in one run, over one candidate set ---------

async function plannedDraftItem(page: Page) {
  const served = await (await page.request.get("/api/discovery/devices")).json();
  const device = (served.devices || []).find(
    (entry: Record<string, unknown>) => entry.ip === SEEDED_IP,
  );
  expect(device, JSON.stringify(served)).toBeTruthy();
  return {
    source_id: device.observation_id,
    draft_item_id: "e2e-item-1",
    role: "inverter",
    enabled: true,
    config_name: "WR1",
    display_name: SHARED_DISPLAY,
    ip: SEEDED_IP,
    port: 80,
    serial_number: SEEDED_SERIAL,
    device_type: "zendure_solarflow_800_pro2",
    api_family: "zendure_local_http",
    auto_added: false,
  };
}

async function previewWith(
  page: Page,
  workflowId: string,
  planId: string,
  devices: unknown[],
) {
  const auth = await (await page.request.get("/api/admin/auth/status")).json();
  const response = await page.request.post("/api/setup/config-preview", {
    headers: { "X-CSRF-Token": auth.csrf_token as string },
    data: {
      devices,
      supported_grid_meter_count: 0,
      setup_workflow_id: workflowId,
      device_plan_id: planId,
    },
  });
  return { status: response.status(), body: await response.json() };
}

async function startedWorkflowId(page: Page) {
  const auth = await (await page.request.get("/api/admin/auth/status")).json();
  const started = await page.request.post("/api/admin/start-path", {
    headers: { "X-CSRF-Token": auth.csrf_token as string },
    data: { choice: "setup_new", confirm: true },
  });
  return (await started.json()).setup_workflow_id as string;
}

test("Fresh Setup: a valid device plan cannot authorize an invented device", async ({
  page,
  seedLocalApiDevices,
}) => {
  test.setTimeout(120_000);
  await signIn(page);
  await seedLocalApiDevices([{ ip: SEEDED_IP, serial: SEEDED_SERIAL }]);
  const workflowId = await startedWorkflowId(page);
  const planned = await plannedDraftItem(page);
  const plan = await devicePlan(page, { draft_items: [planned] });

  const forged = {
    ...planned,
    source_id: "obs:v1:invented",
    draft_item_id: "e2e-item-evil",
    config_name: "WR-EVIL",
    serial_number: "INVENTED999",
    ip: "192.0.2.77",
  };
  const refused = await previewWith(page, workflowId, plan.plan_id, [forged]);
  expect(refused.status, JSON.stringify(refused.body)).toBe(409);
  expect(refused.body.config_preview_id).toBeUndefined();

  // The draft the plan did cover is not refused, so the binding costs a real
  // review nothing. Whether it also becomes *applicable* is the System Build's
  // question, and belongs to the journeys that confirm one.
  const accepted = await previewWith(page, workflowId, plan.plan_id, [planned]);
  expect(accepted.status, JSON.stringify(accepted.body)).toBe(200);
  expect(accepted.body.error).toBeUndefined();
});

test("Fresh Setup: a server-side identity change blocks the old preview", async ({
  page,
  seedLocalApiDevices,
}) => {
  test.setTimeout(120_000);
  await signIn(page);
  await seedLocalApiDevices([{ ip: SEEDED_IP, serial: SEEDED_SERIAL }]);
  const workflowId = await startedWorkflowId(page);
  const planned = await plannedDraftItem(page);
  const plan = await devicePlan(page, { draft_items: [planned] });

  // Same address, different hardware: the handle survives, the decision does not.
  await seedLocalApiDevices([{ ip: SEEDED_IP, serial: OTHER_SERIAL }]);

  const refused = await previewWith(page, workflowId, plan.plan_id, [planned]);
  expect(refused.status, JSON.stringify(refused.body)).toBe(409);
  expect(refused.body.error).toBe("stale_device_plan");
});

test("Fresh Setup: a device plan cannot be spent in another run", async ({
  page,
  seedLocalApiDevices,
}) => {
  test.setTimeout(120_000);
  await signIn(page);
  await seedLocalApiDevices([{ ip: SEEDED_IP, serial: SEEDED_SERIAL }]);
  const firstWorkflowId = await startedWorkflowId(page);
  const planned = await plannedDraftItem(page);
  const plan = await devicePlan(page, { draft_items: [planned] });

  // The setup is restarted: the run the plan belongs to ends and a new one
  // takes over, exactly as "Restart setup" does.
  const auth = await (await page.request.get("/api/admin/auth/status")).json();
  const discarded = await page.request.post("/api/setup/abandon", {
    headers: { "X-CSRF-Token": auth.csrf_token as string },
    data: { setup_workflow_id: firstWorkflowId },
  });
  expect(discarded.status(), await discarded.text()).toBe(200);
  const nextWorkflowId = await startedWorkflowId(page);
  expect(nextWorkflowId).not.toBe(firstWorkflowId);

  const refused = await previewWith(page, nextWorkflowId, plan.plan_id, [planned]);
  expect(refused.status, JSON.stringify(refused.body)).toBe(409);
  expect(refused.body.error).toBe("stale_device_plan");
});
