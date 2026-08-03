import { type Page } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";
import { SHELLY_METER } from "./helpers/discovery-state";

// Two discovered inverters the backend could not physically identify must stay
// two devices in the browser. Before the fix the discovery Map was keyed on
// `api_family + ":" + serial_number`, so two observations that only *displayed*
// the same masked serial produced one key, the second silently overwrote the
// first, and that same key also drove DOM identity, dismissal and selection.
//
// The browser now keys on the server-issued `observation_id` only, which is why
// these devices are seeded into the deterministic Admin's own discovery state
// and their ids are read back from the rendered cards. They differ solely in
// the address they answered on.
//
// The unidentifiable pair here answers without a serial at all. A device that
// reports a *placeholder* serial cannot be used for this journey: the mDNS
// store merges on the reported serial, one layer below the browser, so two of
// them never reach it as two observations. The placeholder rule itself is
// pinned in tests/test_admin_setup_identity_migration.py.

const MASKED_SERIAL = "••••";
const IP_A = "192.168.100.11";
const IP_B = "192.168.100.12";

function unidentifiedInverter(ip: string) {
  return {
    ip,
    serial: "",
    role: "inverter" as const,
    api_family: "zendure_local_http",
    device_type: "zendure_solarflow_800_pro_2",
    model: "SolarFlow 800 Pro 2",
    config_ready: true,
  };
}

function draftInverterCards(page: Page) {
  return page.locator("#config-draft-list .hardware-card-inverter");
}

/** Reach Config over a seeded discovery state and return the rendered card ids. */
async function reachConfig(
  page: Page,
  seed: (spec: Record<string, unknown>) => Promise<void>,
  devices: Record<string, unknown>[],
): Promise<string[]> {
  const login = new LoginPage(page);
  await login.open();
  await login.authenticate();
  await seed({ local_api_devices: [...devices, SHELLY_METER] });
  await page.reload();
  const setup = new SetupPage(page);
  await setup.chooseFreshInstall();
  await setup.selectBuild("latest");
  await expect(setup.continueButton).toBeEnabled();
  await setup.continueToDevices();
  await page.locator('[data-setup-step="config"]').click();
  await expect(draftInverterCards(page)).toHaveCount(devices.length);
  const ids = await page
    .locator("#config-draft-list [data-source-id]")
    .evaluateAll((nodes) =>
      nodes.map((node) => node.getAttribute("data-source-id") || ""),
    );
  return ids.filter(Boolean);
}

test("Fresh Setup: two unidentified observations stay two devices", { tag: ["@setup"] }, async ({
  page,
  seedDiscoveryState,
}) => {
  test.setTimeout(90_000);
  const ids = await reachConfig(page, seedDiscoveryState, [
    unidentifiedInverter(IP_A),
    unidentifiedInverter(IP_B),
  ]);

  // The collision: one entry would mean the second observation overwrote the
  // first. Each card carries its own server-issued observation id.
  expect(new Set(ids).size).toBe(2);
  for (const id of ids) {
    expect(id).toMatch(/^obs:v1:/);
  }
});

test("Fresh Setup: dismissing one unidentified observation keeps the other", { tag: ["@setup"] }, async ({
  page,
  seedDiscoveryState,
}) => {
  test.setTimeout(90_000);
  const [first, second] = await reachConfig(page, seedDiscoveryState, [
    unidentifiedInverter(IP_A),
    unidentifiedInverter(IP_B),
  ]);

  await page
    .locator(`#config-draft-list [data-source-id="${first}"]`)
    .locator(".config-draft-remove")
    .click();

  // Dismissal is scoped to one observation id: the sibling the backend could
  // not identify either must survive.
  await expect(
    page.locator(`#config-draft-list [data-source-id="${first}"]`),
  ).toHaveCount(0);
  await expect(
    page.locator(`#config-draft-list [data-source-id="${second}"]`),
  ).toHaveCount(1);
  await expect(draftInverterCards(page)).toHaveCount(1);
});

test("Fresh Setup: a masked serial never becomes a browser collection key", { tag: ["@setup"] }, async ({
  page,
  seedDiscoveryState,
}) => {
  test.setTimeout(90_000);
  const ids = await reachConfig(page, seedDiscoveryState, [
    { ...unidentifiedInverter(IP_A), serial: MASKED_SERIAL },
  ]);

  expect(ids).toHaveLength(1);
  expect(ids[0]).toMatch(/^obs:v1:/);
  expect(ids[0]).not.toContain(MASKED_SERIAL);
});
