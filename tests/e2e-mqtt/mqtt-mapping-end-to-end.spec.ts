// SPDX-License-Identifier: AGPL-3.0-or-later
// One run from a device in an unknown format to the EMS reading it, on a real
// Mosquitto (tests/e2e-mqtt/harness.py plays a Shelly Gen2 plug on it), taken
// the way a user takes it: every step is a click on the Admin console's page,
// and the result is read off the EMS dashboard's page.
//   1. the live topics recognise the plug and label it Shelly (Gen2+);
//   2. it is set up by answering what it measures (a balcony plant), the
//      negative reading is turned round on its own, the server's preview says
//      612.3, and one save writes the mapping, which starts the real mapping
//      service (python -m mqtt_mapping), and adds its device to the EMS;
//   3. its row says it is in the EMS and then that its first value arrived;
//   4. the real EMS, dry and with every write gate off, reads the broker, and
//      its dashboard shows the device online at 612 W;
//   5. a new name in the mapping renames the EMS device, and removing the
//      mapping removes the EMS device with it.
// Every wait is on a response or a state the page shows, never a timer.
import { test, expect, type Page } from "@playwright/test";
import { spawn, type ChildProcess } from "node:child_process";
import { openSync, readFileSync } from "node:fs";
import { join } from "node:path";

const PASSWORD = "e2e-admin-password";

type Harness = { app: string; python: string; dashboard_port: number; broker_port: number; broker_ref: string; shelly_ids: Record<string, string> };

function harness(): Harness {
  return JSON.parse(readFileSync(process.env.EMS_MQTT_E2E_STATE as string, "utf8"));
}

/** The first browser sets the password on the sign-in screen, the second signs in with it. */
async function signIn(page: Page) {
  await page.goto("/");
  const screen = page.getByTestId("sign-in-screen");
  await expect(screen).toHaveAttribute("data-mode", /^(create|login)$/);
  const creating = (await screen.getAttribute("data-mode")) === "create";
  await page.fill("#admin-password", PASSWORD);
  if (creating) await page.fill("#admin-password-confirm", PASSWORD);
  await page.getByTestId("sign-in-submit").click();
  await expect(page.locator(".app")).toBeVisible();
}

test("a Shelly plug in an unknown format is mapped, added and read by the EMS", async ({ page }) => {
  const world = harness();
  const project = test.info().project.name;
  const shellyId = world.shelly_ids[project];
  const deviceName = `Balkon ${project}`;
  const deviceId = `balkon-${project}`;
  let ems: ChildProcess | null = null;
  try {
    await signIn(page);
    await page.goto("/#/settings/devices");

    // 1. Recognise the topics.
    const mapping = page.getByTestId("mqtt-mapping");
    await mapping.locator(`[data-testid="mqtt-broker-choice"][data-ref="${world.broker_ref}"] input[type=radio]`).check();
    await mapping.getByTestId("mqtt-topics-start").click();
    const plug = mapping.locator(`li[data-label="shelly_gen2"][data-key="${shellyId}"]`);
    await expect(plug).toContainText("Shelly (Gen2+)");
    const apower = plug.locator(".value-row").filter({ hasText: "apower" });
    await expect(apower).toContainText("-612.3");

    // 2. Answer what it measures, preview it, and save the mapping with its EMS device.
    await plug.getByTestId("mqtt-map-group").click();
    const wizard = page.getByTestId("mqtt-wizard");
    await expect(wizard).toContainText("What does this device measure?");
    await wizard.getByTestId("mqtt-kind-balcony").check();
    await wizard.getByTestId("mqtt-wizard-name").fill(deviceName);
    await expect(wizard.getByTestId("mqtt-wizard-power")).toHaveValue(`${shellyId}/status/switch:0|apower`);
    await expect(wizard.getByTestId("mqtt-wizard-turned")).toBeVisible();
    const previewed = page.waitForResponse((response) => response.url().endsWith("/api/discovery/mqtt-mapping/value-preview"));
    await wizard.getByTestId("mqtt-wizard-preview").click();
    expect((await previewed).ok()).toBeTruthy();
    await expect(wizard.getByTestId("mqtt-preview-state")).toContainText('{"inverterPower":612.3,"solarPower":612.3}');
    await expect(wizard.getByTestId("mqtt-wizard-save")).toHaveText("Save and add to the EMS");
    await wizard.getByTestId("mqtt-wizard-save").click();
    const dialog = page.locator("dialog[open]");
    await expect(dialog).toContainText("Its device is added to the settings at once");
    await dialog.getByTestId("confirm").click();

    // 3. Its row says it is in the EMS, then that its first value arrived.
    const row = page.getByTestId("mqtt-mapped-devices").locator(`li[data-id="${deviceId}"]`);
    await expect(row.getByTestId("mqtt-adopted")).toHaveText("In the EMS", { timeout: 60_000 });
    await expect(row).toContainText("Published", { timeout: 60_000 });
    await expect(mapping.getByTestId("mqtt-steps").locator('li[data-step="ems"]')).toHaveClass(/done/);
    const config = JSON.parse(readFileSync(join(world.app, "config", "config.json"), "utf8"));
    const added = config.devices.find((device: { mqtt?: { device_id?: string } }) => device.mqtt?.device_id === deviceId);
    expect(added).toMatchObject({ type: "external_mqtt", enabled: true, name: deviceName });
    expect(config.system).toMatchObject({ dry_run: true, allow_hardware_writes: false, allow_mqtt_local_control_writes: false });

    // 4. The EMS reads it.
    const log = openSync(join(world.app, "data", "ems-e2e.log"), "w");
    ems = spawn(world.python, ["-B", "ems-solarflow-api-control.py", "--config", join(world.app, "config", "config.json")], {
      cwd: world.app,
      stdio: ["ignore", log, log],
    });
    const dashboard = await page.context().newPage();
    await expect
      .poll(async () => {
        try {
          await dashboard.goto("about:blank");
          await dashboard.goto(`http://127.0.0.1:${world.dashboard_port}/#/devices`);
          await dashboard.getByTestId("ems-state").waitFor({ timeout: 5_000 });
          return true;
        } catch {
          return false;
        }
      }, { timeout: 90_000 })
      .toBe(true);
    const card = dashboard.locator(`[data-testid="device-card"][data-device="${deviceName}"]`);
    await expect(card).toHaveAttribute("data-state", "online", { timeout: 90_000 });
    await expect(card).toContainText("Telemetry only");
    await expect(card).toContainText("612\u00a0W", { timeout: 30_000 });

    // 5. A new name in the mapping renames its EMS device, and removing the
    //    mapping can take the EMS device with it.
    const configured = () => JSON.parse(readFileSync(join(world.app, "config", "config.json"), "utf8")).devices;
    const renamed = `${deviceName} South`;
    await row.getByRole("button", { name: "Edit" }).click();
    const editor = page.getByTestId("mqtt-mapping-editor");
    await editor.getByLabel("Name").fill(renamed);
    await editor.getByTestId("mqtt-device-save").click();
    await expect(dialog).toContainText(`The EMS device ${deviceName} is renamed to ${renamed};`);
    await dialog.getByTestId("confirm").click();
    await expect(row).toContainText(renamed);
    await expect.poll(() => configured().find((device: { mqtt?: { device_id?: string } }) => device.mqtt?.device_id === deviceId)?.name).toBe(renamed);

    await row.getByRole("button", { name: "Remove" }).click();
    await expect(dialog.getByTestId("mqtt-remove-ems")).toBeChecked();
    await expect(dialog).toContainText(`Also remove the EMS device ${renamed}`);
    await dialog.getByTestId("confirm").click();
    await expect(row).toHaveCount(0);
    await expect.poll(() => configured().some((device: { mqtt?: { device_id?: string } }) => device.mqtt?.device_id === deviceId)).toBe(false);
  } finally {
    if (ems && ems.exitCode === null) {
      const ended = new Promise((resolve) => ems?.once("exit", resolve));
      ems.kill("SIGTERM");
      await ended;
    }
  }
});
