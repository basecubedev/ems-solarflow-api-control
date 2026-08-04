import { type Page } from "@playwright/test";
import { test, expect } from "./fixtures/admin";
import { LoginPage } from "./pages/login-page";
import { SetupPage } from "./pages/setup-page";
import { SHELLY_METER, apiInverter } from "./helpers/discovery-state";

// Guided Setup state written by an earlier release carries no issued identity:
// the draft's source id is the old `<api_family>:<serial>` key, dismissals are
// bare serials, and nothing holds an opaque token. The browser can no longer
// relate such an entry to a current observation — it compares issued ids only —
// so POST /api/setup/device-plan matches it against the server's own current
// discovery state and hands back typed ids.
//
// Which is why the devices are seeded into the deterministic Admin rather than
// answered for the page: a stored serial only resolves through a device the
// server is actually observing.
//
// These journeys are the operator-visible consequences: a configured inverter
// must not reappear as a second one, a bare-serial removal must not spread to an
// unrelated device that merely shows the same name, and an entry the backend
// cannot identify must stay distinct rather than merge.

const LEGACY_SERIAL = "E2ELEGACY001";
const OTHER_SERIAL = "E2ELEGACY002";
const MASKED_SERIAL = "••••";
const SHARED_DISPLAY = "SolarFlow 800 Pro 2";

// Seed the stores exactly as the previous release wrote them: an array of draft
// items with no form id and a serial-derived source id, and a dismissal store of
// bare serials.
async function seedLegacyState(
  page: Page,
  { draft = [] as unknown[], dismissedSerials = [] as string[] } = {},
) {
  await page.addInitScript(
    ([items, serials]) => {
      const marker = "__ems_legacy_seeded";
      if (window.sessionStorage.getItem(marker)) return;
      window.sessionStorage.setItem(marker, "1");
      window.localStorage.setItem("ems-admin-config-draft", JSON.stringify(items));
      if ((serials as string[]).length) {
        window.localStorage.setItem(
          "ems-admin-config-dismissed-serials",
          JSON.stringify(serials),
        );
      }
    },
    [draft, dismissedSerials] as const,
  );
}

async function reachConfig(
  page: Page,
  seed: (spec: Record<string, unknown>) => Promise<void>,
  devices: Record<string, unknown>[],
) {
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
}

function draftInverterCards(page: Page) {
  return page.locator("#config-draft-list .hardware-card-inverter");
}

test("Fresh Setup: a legacy draft is rehydrated instead of duplicated", { tag: ["@setup"] }, async ({
  page,
  seedDiscoveryState,
}) => {
  test.setTimeout(90_000);
  await seedLegacyState(page, {
    draft: [
      {
        // The pre-identity shape: no draft_item_id, no tokens, and the old
        // `<api_family>:<serial>` collection key.
        source_id: "zendure:" + LEGACY_SERIAL,
        role: "inverter",
        config_name: "INV_LEGACY",
        display_name: SHARED_DISPLAY,
        enabled: true,
        serial_number: LEGACY_SERIAL,
        ip: "192.168.100.21",
        port: 80,
        api_family: "zendure_local_http",
        device_type: "zendure_solarflow_800_pro2",
      },
    ],
  });
  await reachConfig(page, seedDiscoveryState, [
    apiInverter(LEGACY_SERIAL, "192.168.100.21"),
  ]);

  // One inverter, still under the operator's name: the backend recognized the
  // stored entry as the device discovery is currently reporting.
  await expect(draftInverterCards(page)).toHaveCount(1);
  await expect(page.locator("#config-draft-list")).toContainText("INV_LEGACY");
});

test("Fresh Setup: a legacy bare-serial removal spares a same-name device", { tag: ["@setup"] }, async ({
  page,
  seedDiscoveryState,
}) => {
  test.setTimeout(90_000);
  await seedLegacyState(page, { dismissedSerials: [LEGACY_SERIAL] });
  await reachConfig(page, seedDiscoveryState, [
    apiInverter(LEGACY_SERIAL, "192.168.100.21"),
    apiInverter(OTHER_SERIAL, "192.168.100.22"),
  ]);

  // The dismissal resolves to one issued physical identity. The second inverter
  // shows the same model name and is a different device, so it is adopted.
  await expect(draftInverterCards(page)).toHaveCount(1);
  await expect(page.locator("#config-draft-list")).toContainText(OTHER_SERIAL);
  await expect(page.locator("#config-draft-list")).not.toContainText(LEGACY_SERIAL);
});

test("Fresh Setup: a masked legacy entry stays distinct and unresolved", { tag: ["@setup"] }, async ({
  page,
  seedDiscoveryState,
}) => {
  test.setTimeout(90_000);
  await seedLegacyState(page, {
    draft: [
      {
        source_id: "zendure:" + MASKED_SERIAL,
        role: "inverter",
        config_name: "INV_MASKED",
        display_name: SHARED_DISPLAY,
        enabled: true,
        serial_number: MASKED_SERIAL,
        ip: "192.168.100.32",
        port: 80,
        api_family: "zendure_local_http",
        device_type: "zendure_solarflow_800_pro2",
      },
    ],
  });
  await reachConfig(page, seedDiscoveryState, [
    {
      ...apiInverter(MASKED_SERIAL, "192.168.100.31"),
      device_type: "zendure_solarflow_800_pro2",
    },
  ]);

  // Neither entry proves which hardware it is, and they answer on different
  // endpoints: the stored one and the discovered one stay two rows. A shared
  // placeholder must never merge them, and never dismiss one through the other.
  await expect(draftInverterCards(page)).toHaveCount(2);
  const sourceIds = await page
    .locator("#config-draft-list [data-source-id]")
    .evaluateAll((nodes) =>
      nodes.map((node) => node.getAttribute("data-source-id") || ""),
    );
  expect(new Set(sourceIds).size).toBe(sourceIds.length);
});
