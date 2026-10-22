// SPDX-License-Identifier: AGPL-3.0-or-later
// The Modern Admin console at /modern/, beside Classic on the same server. It
// is driven here against the deterministic test-mode Admin: the switch between
// the two, the sign-in, the language, and the pages whose writes it runs
// itself -- a backup from creation through details, restore and deletion.
import { expect, test, ADMIN_PASSWORD } from "./fixtures/admin";
import type { Page } from "@playwright/test";
import { post } from "./helpers/setup-authority";
import { ADMIN_UI_KEY as UI_KEY } from "./helpers/classic-ui";
import { SetupPage } from "./pages/setup-page";
import { E3DC_SYSTEM, apiInverter } from "./helpers/discovery-state";

async function signIn(page: Page, password = ADMIN_PASSWORD) {
  await expect(page.getByTestId("sign-in-screen")).toBeVisible();
  await expect(page.getByTestId("sign-in-screen")).toHaveAttribute("data-mode", "login");
  await page.fill("#admin-password", password);
  await page.getByTestId("sign-in-submit").click();
}

async function openModern(page: Page, hash = "") {
  await page.goto(`/modern/${hash}`);
  await signIn(page);
  await expect(page.locator(".app")).toBeVisible();
}

test.use({ storageState: { cookies: [], origins: [] } });

test.describe("Modern Admin: the switch", { tag: ["@smoke"] }, () => {
  test("the console opens in Modern, Classic is one click away and stays chosen, New design comes back", async ({ page }) => {
    await page.goto("/");
    await expect(page).toHaveURL(/\/modern\/$/);
    await signIn(page);
    await expect(page.locator(".app")).toBeVisible();
    expect(await page.evaluate((key) => localStorage.getItem(key), UI_KEY)).toBe("modern");

    await page.getByTestId("switch-classic").click();
    await expect(page).toHaveURL(/\/#maintenance$/);
    await expect(page.locator("#modern-admin-link")).toBeVisible();
    expect(await page.evaluate((key) => localStorage.getItem(key), UI_KEY)).toBe("classic");
    await page.goto("/");
    await expect(page).toHaveURL(/\/$/);
    await expect(page.locator("#modern-admin-link")).toBeVisible();

    await page.locator("#modern-admin-link").click();
    await expect(page).toHaveURL(/\/modern\/$/);
    await expect(page.locator(".app")).toBeVisible();
    expect(await page.evaluate((key) => localStorage.getItem(key), UI_KEY)).toBe("modern");
    await page.goto("/");
    await expect(page).toHaveURL(/\/modern\/$/);
  });

  test("going back to the Classic page after New design does not choose Classic again", async ({ page }) => {
    await openModern(page);
    await page.getByTestId("switch-classic").click();
    await expect(page).toHaveURL(/\/#maintenance$/);
    await page.locator("#modern-admin-link").click();
    await expect(page).toHaveURL(/\/modern\/$/);
    await page.goBack();
    await expect(page).toHaveURL(/\/#maintenance$/);
    await expect(page.locator("#modern-admin-link")).toBeVisible();
    expect(await page.evaluate((key) => localStorage.getItem(key), UI_KEY)).toBe("modern");
  });

  test("a guided setup begun in Classic stays in Classic across a reload, though Modern is the default", async ({ page }) => {
    const signedIn = await page.request.post("/api/admin/auth/login", { data: { password: ADMIN_PASSWORD } });
    expect(signedIn.ok()).toBeTruthy();
    await page.goto("/#maintenance");
    await page.locator('.workspace-back[data-back="landing"]:visible').first().click();
    await expect(page.locator("#view-start")).toBeVisible();
    await expect(page).toHaveURL(/\/$/);
    await new SetupPage(page).chooseFreshInstall();
    await expect(page).toHaveURL(/\/#setup$/);
    await page.reload();
    await expect(page).toHaveURL(/\/#setup$/);
    await expect(page.locator("#modern-admin-link")).toBeVisible();
    expect(await page.evaluate((key) => localStorage.getItem(key), UI_KEY)).toBeNull();
  });

  test("the guided upgrade's Classic page stays in Classic across a reload", async ({ page }) => {
    const signedIn = await page.request.post("/api/admin/auth/login", { data: { password: ADMIN_PASSWORD } });
    expect(signedIn.ok()).toBeTruthy();
    await page.goto("/#maintenance-upgrade");
    await expect(page.locator("#modern-admin-link")).toBeVisible();
    await page.reload();
    await expect(page).toHaveURL(/\/#maintenance-upgrade$/);
    await expect(page.locator("#modern-admin-link")).toBeVisible();
  });

  test("a Classic page Modern hands over to opens in Classic whatever the choice", async ({ page }) => {
    await page.goto("/?ui=modern");
    await expect(page).toHaveURL(/\/modern\/$/);
    await page.goto("/#maintenance-settings");
    await expect(page).toHaveURL(/\/#maintenance-settings$/);
    await expect(page.locator("#modern-admin-link")).toBeVisible();
    expect(await page.evaluate((key) => localStorage.getItem(key), UI_KEY)).toBe("modern");
  });
});

test.describe("Modern Admin: signing in", { tag: ["@smoke"] }, () => {
  test("a wrong password is refused in words, the right one opens the console, signing out closes it", async ({ page }) => {
    await page.goto("/modern/");
    await signIn(page, "not-the-password");
    await expect(page.getByRole("alert")).toHaveText("Wrong password.");
    await expect(page.locator(".app")).toHaveCount(0);

    await page.fill("#admin-password", ADMIN_PASSWORD);
    await page.getByTestId("sign-in-submit").click();
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Overview");

    await page.getByTestId("sign-out").click();
    await expect(page.getByTestId("sign-in-screen")).toBeVisible();
    const status = await (await page.request.get("/api/admin/auth/status")).json();
    expect(status.authenticated).toBe(false);
  });

  test("a session that ended elsewhere brings the sign-in back", async ({ page }) => {
    await openModern(page);
    await page.request.post("/api/admin/auth/logout");
    await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
    await expect(page.getByTestId("sign-in-screen")).toBeVisible();
  });

  test("a run the ended session refused is not left behind as a failure once signed in again", async ({ page }) => {
    await openModern(page, "#/diagnostics");
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Diagnostics");
    await page.request.post("/api/admin/auth/logout");
    await page.getByTestId("run-checks").click();
    await signIn(page);
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Diagnostics");
    await expect(page.getByTestId("run-checks")).toBeEnabled();
    await expect(page.getByRole("alert")).toHaveCount(0);
  });

  test("the language chosen at the sign-in carries into the console", async ({ page }) => {
    await page.goto("/modern/");
    await page.getByTestId("sign-in-screen").getByRole("button", { name: "DE" }).click();
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Anmelden");
    await signIn(page);
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Übersicht");
    await expect(page.locator("html")).toHaveAttribute("lang", "de");
  });
});

// The seed hook runs behind the page's own session, so the console is signed
// in first and read again once the scenario is in place.
async function seededAt(page: Page, seed: (scenario: string) => Promise<void>, hash: string) {
  await openModern(page);
  await seed("mixed_transports");
  await page.goto(`/modern/${hash}`);
  await page.reload();
  await expect(page.locator(".app")).toBeVisible();
}

test.describe("Modern Admin: overview and status", { tag: ["@smoke"] }, () => {

  test("an incomplete installation is named, with the way out of it", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "");
    const hero = page.getByTestId("overview-hero");
    await expect(hero).toHaveAttribute("data-level", "error");
    const finding = page.locator('[data-code="installation_incomplete"]');
    await expect(finding).toContainText("Settings without container setup");
    await expect(finding).toContainText("Missing here: Container setup");
    await expect(finding.getByRole("link", { name: "Set up in the Classic view" })).toHaveAttribute("href", "/#setup");
    await expect(page.getByTestId("health-state")).toHaveText("Needs attention");
    await expect(page.getByTestId("tile-control")).toContainText("Calculates only");
  });

  test("services show what should run and refuse a restart they cannot do", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/services");
    await expect(page.locator('[data-service="ems"]')).toContainText("Not set up");
    await expect(page.getByTestId("sync")).toBeDisabled();
    await expect(page.getByText("Docker or the container setup is not available")).toBeVisible();
  });

  test("the settings edit the devices here, with the network search beside them and no way to Classic", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/settings");
    await expect(page.getByTestId("settings-features")).toContainText("Winter mode");
    await page.getByRole("button", { name: "Devices", exact: true }).click();
    const editor = page.getByTestId("devices-editor");
    await expect(editor.locator("[data-device^='inverter-'], [data-device^='mqtt-']")).toHaveCount(3);
    await expect(editor).toContainText("Zendure via the Zendure cloud");
    await expect(editor.locator('[data-device="zendure-broker"]')).toContainText("read only");
    await expect(page.getByTestId("discovery-start")).toBeVisible();
    await expect(page.locator(".content a[href^='/#']")).toHaveCount(0);
  });

  test("the checks say plainly when there is no EMS to check", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/diagnostics");
    await page.getByTestId("run-checks").click();
    await expect(page.getByTestId("diagnostics-summary")).toContainText("Not available");
    await expect(page.getByText("No running EMS container and no emsctl.py were found")).toBeVisible();
  });

  test("the system page lists the files and stays quiet about a workflow that is idle", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/system");
    await expect(page.getByText("Settings file", { exact: true })).toBeVisible();
    await expect(page.getByText("local_mixed")).toBeVisible();
    await expect(page.getByTestId("recovery-summary")).toHaveCount(0);
  });
});

test.describe("Modern Admin: settings", { tag: ["@smoke"] }, () => {
  test("a feature is switched on, checked and saved, and the saved settings say so", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/settings");
    const winter = page.locator('[data-section="winter"]');
    const toggle = winter.getByRole("checkbox", { name: "Winter mode on or off" });
    const before = await toggle.isChecked();
    await toggle.click();
    await expect(page.getByTestId("change-count")).toHaveText("1 unsaved change");

    await page.getByTestId("review").click();
    const review = page.getByTestId("settings-review");
    await expect(review).toContainText("Settings are valid");
    await expect(review).toContainText("winter.enabled");
    await page.getByTestId("apply").click();
    await page.getByRole("dialog", { name: "Save the checked changes?" }).getByTestId("confirm").click();
    await expect(page.getByTestId("settings-applied")).toContainText("Saved, with a backup of the previous settings:");

    await page.reload();
    await expect(page.locator('[data-section="winter"]').getByRole("checkbox", { name: "Winter mode on or off" })).toBeChecked({ checked: !before });
    await expect(page.getByTestId("settings-changes")).toHaveCount(0);
  });

  test("the overview's Control & safety card opens the safety settings", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "");
    await page.getByRole("link", { name: "Change", exact: true }).click();
    await expect(page).toHaveURL(/#\/settings\/safety$/);
    await expect(page.getByTestId("settings-safety")).toContainText("Allow hardware writes");
  });

  test("a device is changed, another added, both checked and saved", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/settings/devices");
    const editor = page.getByTestId("devices-editor");
    const first = editor.locator('[data-device="inverter-0"]');
    await first.locator(".summary").click();
    await first.locator('[data-path="devices[].max_power"] input').fill("700");
    await page.getByTestId("add-e3dc").click();
    const e3dc = editor.locator('[data-device="e3dc-3"]');
    await expect(e3dc).toContainText("E3DC");
    await e3dc.locator('[data-path="e3dcHost"] input').fill("192.168.50.30");
    await expect(page.getByTestId("change-count")).toHaveText("2 unsaved changes");

    await page.getByTestId("review").click();
    const review = page.getByTestId("settings-review");
    await expect(review).toContainText("Settings are valid");
    await expect(review).toContainText("devices");
    await page.getByTestId("apply").click();
    await page.getByRole("dialog", { name: "Save the checked changes?" }).getByTestId("confirm").click();
    await expect(page.getByTestId("settings-applied")).toBeVisible();

    await page.reload();
    await expect(editor.locator('[data-device="inverter-0"]')).toContainText("700 W");
    await expect(editor.locator('[data-device="e3dc-3"]')).toContainText("192.168.50.30");
  });

  test("the grid meter's type changes its fields, and removing it leaves none", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/settings/devices");
    const meter = page.getByTestId("devices-editor").locator('[data-device="grid-meter"]');
    await meter.locator(".summary").click();
    await expect(meter.locator('[data-path="grid_meter.ip"] input')).toHaveValue("192.168.50.2");
    await page.getByTestId("grid-type").selectOption("zendure_smartmeter_d0");
    await expect(meter.locator('[data-path="d0Serial"]')).toBeVisible();
    await expect(page.getByTestId("change-count")).toHaveText("1 unsaved change");
    await meter.getByTestId("device-remove").click();
    await expect(meter).toContainText("No grid meter configured");
  });

  test("an edit survives a reload, the search finds it, and discard leaves the file alone", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/settings");
    await page.getByRole("button", { name: "Expert", exact: true }).click();
    await page.getByTestId("settings-search").fill("loop interval");
    const field = page.locator('[data-path="system.loop_interval"] input');
    await expect(field).toBeVisible();
    await field.fill("7");
    await expect(page.getByTestId("change-count")).toHaveText("1 unsaved change");

    await page.reload();
    await expect(page.getByTestId("change-count")).toHaveText("1 unsaved change");
    await page.getByTestId("discard").click();
    await expect(page.getByTestId("settings-changes")).toHaveCount(0);
    await page.getByRole("button", { name: "Expert", exact: true }).click();
    await page.getByTestId("settings-search").fill("loop interval");
    await expect(page.locator('[data-path="system.loop_interval"] input')).not.toHaveValue("7");
  });
});

test.describe("Modern Admin: older MQTT setup", { tag: ["@smoke"] }, () => {
  test("the migration is reviewed and applied here, without showing a secret", async ({ page, seedAdminScenario }) => {
    await openModern(page);
    await seedAdminScenario("mqtt_migration");
    await page.goto("/modern/#/system");
    await page.reload();
    await expect(page.getByTestId("migration-devices").locator("li").first()).toBeVisible();
    await expect(page.locator("body")).not.toContainText("e2e-super-secret-broker-password");
    await expect(page.getByTestId("migration-backup")).toBeChecked();
    const request = page.waitForRequest((req) => req.url().endsWith("/zendure-mqtt/migration-apply"));
    const devices = await page.getByTestId("migration-devices").locator("li").count();
    await page.getByTestId("migration-apply").click();
    const dialog = page.getByRole("dialog", { name: "Apply the migration?" });
    await expect(dialog).toContainText(devices === 1 ? "1 device is set up anew." : `${devices} devices are set up anew.`);
    await expect(dialog).toContainText("A backup of the current settings is made first.");
    await dialog.getByTestId("confirm").click();
    const sent = await request;
    expect(sent.headers()["x-csrf-token"]).toBeTruthy();
    expect(sent.postDataJSON()).toMatchObject({ confirm: true, backup: true });
    await expect(page.getByTestId("migration-result")).toHaveText("The MQTT setup is up to date.");
    await expect(page.getByTestId("migration-apply")).toHaveCount(0);
  });
});

const EXTERNAL = "E2EMODERN0001";

function externalBroker() {
  const host = "192.168.100.72";
  return {
    host,
    port: 1883,
    devices: [
      {
        broker_id: `mqtt:${host}:1883`,
        broker_host: host,
        broker_port: 1883,
        source_type: "local_mqtt",
        topic_family: "ems_solarflow",
        device_id: EXTERNAL,
        serial_number: null,
        display_name: "External device",
        metrics_seen: ["electricLevel", "outputHomePower"],
        topics_seen: [`ems-solarflow/${EXTERNAL}/inverterPower`, `ems-solarflow/${EXTERNAL}/batterySoc`],
      },
    ],
  };
}

async function quietNetwork(page: Page) {
  const reply = (body: unknown) => (route: import("@playwright/test").Route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  await page.route("**/api/discovery/networks**", reply({ networks: [] }));
  await page.route("**/api/discovery/scan**", reply({ scan_id: "e2e-scan" }));
  await page.route("**/api/discovery/result/**", reply({ status: "complete", devices: [] }));
  await page.route("**/api/discovery/mqtt-brokers/refresh**", reply({ ok: true }));
  await page.route("**/api/discovery/zendure-cloud-mqtt/settings**", reply({ token_saved: false, tls_mode: "system_ca" }));
}

async function searched(page: Page, seedAdminScenario: (scenario: string) => Promise<void>, seedDiscoveryState: (state: object) => Promise<void>) {
  await quietNetwork(page);
  await openModern(page);
  await seedAdminScenario("mixed_transports");
  await seedDiscoveryState({ local_api_devices: [apiInverter("E2ENEWINV01", "192.168.50.41"), E3DC_SYSTEM], local_mqtt_brokers: [externalBroker()] });
  await page.goto("/modern/#/settings/devices");
  await page.reload();
  await page.getByTestId("discovery-start").click();
  await expect(page.getByTestId("discovery-outcome")).toContainText("The search finished");
  return page.getByTestId("discovery-items");
}

test.describe("Modern Admin: network search", { tag: ["@smoke"] }, () => {
  test("found devices are added from the search, checked and saved", async ({ page, seedAdminScenario, seedDiscoveryState }) => {
    test.setTimeout(120_000);
    const items = await searched(page, seedAdminScenario, seedDiscoveryState);
    const inverter = items.locator("li").filter({ hasText: "192.168.50.41" });
    await inverter.getByRole("button", { name: "Add inverter" }).click();
    await expect(inverter.getByRole("button", { name: "Added" })).toBeDisabled();
    await items.locator('li[data-role="read_only_device"]').getByRole("button", { name: "Add as read-only device" }).click();
    await items.locator("li").filter({ hasText: EXTERNAL }).getByRole("button", { name: "Add inverter" }).click();
    await expect(items.locator('li[data-role="read_only_device"]').getByRole("button", { name: "Added" })).toBeDisabled();
    const editor = page.getByTestId("devices-editor");
    await expect(editor).toContainText("192.168.50.41");
    await expect(editor).toContainText(`${E3DC_SYSTEM.ip}:502`);
    await expect(editor).toContainText("added from discovery");
    await expect(page.getByTestId("change-count")).toHaveText("3 unsaved changes");

    await page.getByTestId("review").click();
    await expect(page.getByTestId("settings-review")).toContainText("Settings are valid");
    await page.getByTestId("apply").click();
    await page.getByRole("dialog", { name: "Save the checked changes?" }).getByTestId("confirm").click();
    await expect(page.getByTestId("settings-applied")).toBeVisible();
    await page.reload();
    await expect(page.getByTestId("devices-editor")).toContainText("192.168.50.41");
    await expect(page.getByTestId("devices-editor")).toContainText(`${E3DC_SYSTEM.ip}:502`);
  });

  test("a found device is ignored for this search, and clearing leaves the changes alone", async ({ page, seedAdminScenario, seedDiscoveryState }) => {
    test.setTimeout(120_000);
    const items = await searched(page, seedAdminScenario, seedDiscoveryState);
    const inverter = items.locator("li").filter({ hasText: "192.168.50.41" });
    await inverter.getByTestId("discovery-ignore").click();
    await expect(inverter.getByRole("button", { name: "Ignored" })).toBeDisabled();
    await items.locator('li[data-role="read_only_device"]').getByRole("button", { name: "Add as read-only device" }).click();
    await page.getByTestId("discovery-clear").click();
    await expect(page.getByTestId("discovery-items")).toHaveCount(0);
    await expect(page.getByTestId("discovery-outcome")).toContainText("Results cleared");
    await expect(page.getByTestId("change-count")).toHaveText("1 unsaved change");
  });
});

test.describe("Modern Admin: what the search may use", { tag: ["@smoke"] }, () => {
  test("a broker credential is saved and removed, and the password never comes back", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/settings/devices");
    await page.getByTestId("credential-label").fill("garage broker");
    await page.getByTestId("credential-username").fill("garage-user");
    await page.getByTestId("credential-password").fill("e2e-modern-secret");
    await page.getByTestId("credential-save").click();
    const list = page.getByTestId("credentials");
    await expect(list).toContainText("garage broker");
    await expect(page.getByTestId("credential-password")).toHaveValue("");
    await expect(page.locator("body")).not.toContainText("e2e-modern-secret");
    await list.locator("li").filter({ hasText: "garage broker" }).getByRole("button", { name: "Remove" }).click();
    await page.getByRole("dialog", { name: "Remove this credential?" }).getByTestId("confirm").click();
    await expect(page.getByTestId("sources-message")).toContainText("Credential removed.");
    await expect(page.getByTestId("search-sources")).not.toContainText("garage broker");
  });

  test("the Zendure cloud key is saved, tested, used and forgotten", async ({ page, seedAdminScenario }) => {
    let saved = false;
    const sent: string[] = [];
    const reply = (route: import("@playwright/test").Route, body: unknown) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    await page.route("**/api/discovery/zendure-cloud-mqtt/**", async (route) => {
      const url = route.request().url();
      const method = route.request().method();
      sent.push(`${method} ${url.replace(/^.*zendure-cloud-mqtt/, "")}`);
      if (url.endsWith("/settings")) return reply(route, { token_saved: saved, tls_mode: "system_ca", last_broker: saved ? "mqtt.zen-iot.com" : "", last_status: saved ? "ok" : "" });
      if (url.endsWith("/token") && method === "POST") {
        saved = true;
        return reply(route, { ok: true });
      }
      if (url.endsWith("/token") && method === "DELETE") {
        saved = false;
        return reply(route, { ok: true });
      }
      if (url.endsWith("/test")) return reply(route, { ok: true, devices_found: 2, broker: "mqtt.zen-iot.com", tls_mode: "system_ca" });
      return reply(route, { ok: true, candidates: [{ display_name: "SolarFlow 800 Pro 2", serial_number: "CLOUDSN1" }], device_list_count: 1, mqtt_observed_count: 1 });
    });
    await seededAt(page, seedAdminScenario, "#/settings/devices");
    await expect(page.getByTestId("cloud-key-state")).toHaveText("Not saved");
    await page.getByTestId("cloud-key").fill("e2e-cloud-key");
    await page.getByTestId("cloud-save").click();
    await expect(page.getByTestId("cloud-key-state")).toHaveText("Saved");
    await page.getByTestId("cloud-test").click();
    await expect(page.getByTestId("sources-message")).toContainText("The key works: 2 devices via mqtt.zen-iot.com");
    await page.getByTestId("cloud-refresh").click();
    await expect(page.getByTestId("search-sources")).toContainText("CLOUDSN1");
    await page.getByTestId("cloud-forget").click();
    await page.getByRole("dialog", { name: "Forget the Zendure cloud key?" }).getByTestId("confirm").click();
    await expect(page.getByTestId("cloud-key-state")).toHaveText("Not saved");
    expect(sent).toEqual(expect.arrayContaining(["POST /token", "POST /test", "POST /refresh", "DELETE /token"]));
  });

  test("an inverter over MQTT is added by hand on a typed local broker, its password kept out of the draft", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/settings/devices");
    await page.getByTestId("add-mqtt-inverter").click();
    const form = page.getByTestId("mqtt-form");
    await form.locator('[data-path="broker.name"] input').fill("garage");
    await form.locator('[data-path="host"] input').fill("192.168.50.60");
    await form.locator('[data-path="username"] input').fill("garage-user");
    await page.getByTestId("mqtt-form-add").click();
    await expect(form).toContainText("Enter both a username and a password, or neither.");
    await form.locator('[data-path="broker.password"] input').fill("e2e-broker-secret");
    await page.getByTestId("mqtt-form-add").click();
    await expect(form).toHaveCount(0);
    await expect(page.getByTestId("devices-editor").locator('[data-device="mqtt-3"]')).toBeVisible();
    await expect(page.getByTestId("credentials")).toContainText("local_mqtt_garage");
    const stored = await page.evaluate(() => sessionStorage.getItem("ems-admin.settings-hardware") || "");
    expect(stored).toContain("local_mqtt_garage");
    expect(stored).not.toContain("e2e-broker-secret");
  });
});

test.describe("Modern Admin: backups", { tag: ["@smoke"] }, () => {
  test("a backup is created, inspected, restored and deleted from the console", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/backups");
    const names = page.getByTestId("backup-list").locator(".name");
    await expect(page.getByTestId("create-backup")).toBeEnabled();
    const before = new Set(await names.allTextContents());

    await page.locator('[data-scope="config"]').click();
    await page.getByTestId("create-backup").click();
    await expect(page.getByTestId("create-outcome")).toHaveText("The backup was created and verified.");
    await expect(names).toHaveCount(before.size + 1);
    const created = (await names.allTextContents()).find((name) => !before.has(name)) as string;
    expect(created).toMatch(/^ems-config-manual-/);
    const row = page.getByTestId("backup-list").locator("li").filter({ hasText: created });
    await expect(row).toContainText("Settings");

    await row.getByRole("button", { name: "Details" }).click();
    const details = page.getByRole("dialog");
    await expect(details).toContainText("config/config.json");
    await expect(details).toContainText("This backup holds passwords or keys.");
    await details.getByRole("button", { name: "Close" }).click();
    await expect(details).toBeHidden();

    await row.getByTestId("restore-open").click();
    await expect(page.getByTestId("restore-summary")).toContainText("Files unchanged");
    await page.getByTestId("restore-execute").click();
    await page.getByRole("dialog").getByTestId("confirm").click();
    await expect(page.getByTestId("restore-outcome")).toHaveText("Restored. Restart EMS so it uses the restored files.");
    await expect(names.filter({ hasText: /^ems-config-rollback-/ })).not.toHaveCount(0);

    const count = await names.count();
    await row.getByRole("button", { name: `Delete ${created}` }).click();
    await page.getByRole("dialog").getByTestId("confirm").click();
    await expect(names).toHaveCount(count - 1);
    await expect(page.getByTestId("backup-list").locator("li").filter({ hasText: created })).toHaveCount(0);
  });

  test("a restore whose preview was used up elsewhere is refused, and a fresh preview takes its place", async ({ page, seedAdminScenario }) => {
    await seededAt(page, seedAdminScenario, "#/backups");
    await page.locator('[data-scope="config"]').click();
    await page.getByTestId("create-backup").click();
    await expect(page.getByTestId("create-outcome")).toHaveText("The backup was created and verified.");

    const previewed = page.waitForResponse((response) => response.url().endsWith("/api/admin/maintenance/backups/restore/preview"));
    await page.getByTestId("backup-list").locator('li[data-kind="archive"]').first().getByTestId("restore-open").click();
    const plan = await (await previewed).json();
    await expect(page.getByTestId("restore-summary")).toBeVisible();

    const used = await post(page, "/api/admin/maintenance/backups/restore/execute", { plan_id: plan.plan_id, confirm: true });
    expect(used.status, JSON.stringify(used.body)).toBeLessThan(300);
    await expect
      .poll(async () => (await (await page.request.get(`/api/admin/maintenance/backups/jobs/${used.body.job_id}`)).json()).status)
      .not.toBe("running");

    await page.getByTestId("restore-execute").click();
    await page.getByRole("dialog").getByTestId("confirm").click();
    await expect(page.getByTestId("restore-refused")).toContainText("The restore did not start. This preview has expired.");
    await expect(page.getByTestId("restore-summary")).toBeVisible();
    await expect(page.getByTestId("restore-execute")).toBeEnabled();
    await expect(page.getByTestId("restore-outcome")).toHaveCount(0);
  });
});

// A guided workflow that can no longer finish, put in place by the gated seed
// hook the Classic recovery tests use, then resolved from Modern's System page.
async function stuckAt(page: Page, scenarios: string[]) {
  await openModern(page);
  for (const scenario of scenarios) {
    const seeded = await post(page, "/api/admin/test/seed", { scenario });
    expect(seeded.status, JSON.stringify(seeded.body)).toBe(200);
  }
  await page.goto("/modern/#/system");
  await page.reload();
  await expect(page.locator(".app")).toBeVisible();
}

const lifecycle = async (page: Page) => (await page.request.get("/api/admin/workflow-lifecycle")).json();

test.describe("Modern Admin: an unfinished workflow", { tag: ["@smoke", "@workflow"] }, () => {
  test("is named on every page and reset from the System page after one confirmation", async ({ page }) => {
    await stuckAt(page, ["setup_cleanup_pending"]);
    const banner = page.getByTestId("workflow-banner");
    await expect(banner).toContainText("Guided setup is in progress.");
    await expect(banner.getByTestId("workflow-continue")).toHaveAttribute("href", "/#setup");
    await expect(page.getByTestId("recovery-summary")).toContainText("Guided setup is stuck: Cleanup unfinished.");

    const executed = page.waitForResponse((response) => response.url().endsWith("/api/admin/workflow-lifecycle/recovery") && response.request().method() === "POST");
    await page.getByTestId("recovery-safe").click();
    await page.getByRole("dialog").getByTestId("confirm").click();
    expect((await executed).ok()).toBeTruthy();
    await expect(page.getByText("The unfinished workflow was cancelled.")).toBeVisible();
    expect((await lifecycle(page)).switchable).toBe(true);
    await expect(banner).toBeHidden();
  });

  test("leftover records are cleared only after a second, last confirmation", async ({ page }) => {
    await stuckAt(page, ["installed_system_artifacts", "workflow_state_corrupt"]);
    await expect(page.getByTestId("recovery-summary")).toBeVisible();
    await page.getByRole("button", { name: "Force-clear leftover records" }).click();
    await page.getByRole("dialog").getByTestId("confirm").click();
    const last = page.getByRole("dialog", { name: "This is the last confirmation" });
    await expect(last).toBeVisible();
    await last.getByTestId("confirm").click();
    await expect(page.getByText("The leftover records were backed up and removed.")).toBeVisible();
    expect((await lifecycle(page)).state).toBe("idle");
  });

  test("a state that changed after the confirmation is refused, not acted on", async ({ page }) => {
    await stuckAt(page, ["installed_system_artifacts", "setup_cleanup_pending"]);
    await page.getByTestId("recovery-safe").click();
    await expect(page.getByRole("dialog")).toBeVisible();
    const changed = await post(page, "/api/admin/test/seed", { scenario: "workflow_state_corrupt" });
    expect(changed.status, JSON.stringify(changed.body)).toBe(200);
    const executed = page.waitForResponse((response) => response.url().endsWith("/api/admin/workflow-lifecycle/recovery") && response.request().method() === "POST");
    await page.getByRole("dialog").getByTestId("confirm").click();
    expect((await executed).ok()).toBe(false);
    await expect(page.locator(".recovery .outcome.fault")).toHaveText("The workflow changed since this page read it. Look again, then decide.");
    expect((await lifecycle(page)).state).not.toBe("idle");
  });
});

test.describe("Modern Admin: on a phone", { tag: ["@smoke"] }, () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test("a version tapped in the list opens the guided upgrade here and verifies it", async ({ page, seedAdminScenario }) => {
    await openModern(page);
    await seedAdminScenario("mqtt_migration");
    await page.goto("/modern/#/updates");
    await page.reload();
    await expect(page.getByTestId("releases")).toBeVisible();
    const validated = page.waitForResponse((response) => response.url().endsWith("/api/admin/maintenance/upgrade/validate"));
    await page.locator('[data-tag="v9.9.10"]').getByTestId("choose-release").click();
    expect((await validated).ok()).toBeTruthy();
    await expect(page.getByTestId("upgrade")).toHaveAttribute("data-stage", "ready");
    await expect(page).toHaveURL(/\/modern\/#\/updates$/);
    await expect(page.getByRole("heading", { name: "Update to v9.9.10" })).toBeInViewport();
    await expect(page.getByTestId("upgrade-migration")).toContainText("format. 1 device loses");
  });

  test("the bar holds four pages and More holds the rest", async ({ page }) => {
    await openModern(page);
    const bar = page.locator(".bottom-nav");
    await expect(bar).toBeVisible();
    await expect(bar.locator("a")).toHaveCount(4);
    await expect(bar.locator("a .slot-label")).toHaveText(["Overview", "Updates", "Settings", "Services"]);
    await page.getByTestId("nav-more").click();
    const sheet = page.getByRole("dialog", { name: "More" });
    await expect(sheet.getByRole("link", { name: "Diagnostics" })).toBeVisible();
    await sheet.getByRole("link", { name: "System" }).click();
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("System");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });
});
