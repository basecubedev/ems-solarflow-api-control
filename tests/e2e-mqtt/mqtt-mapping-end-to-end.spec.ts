// SPDX-License-Identifier: AGPL-3.0-or-later
// One run from a device in an unknown format to the EMS reading it, on a real
// Mosquitto (tests/e2e-mqtt/harness.py plays a Shelly Gen2 plug on it):
//   1. the live topics recognise the plug and label it Shelly (Gen2+);
//   2. it is set up by answering what it measures (a balcony plant), the
//      negative reading is turned round on its own, and the server's preview
//      says 612.3 before the mapping is applied, which starts the real mapping
//      service (python -m mqtt_mapping);
//   3. "Add as device" in its row adds it through the same external-device
//      adoption Find devices uses, and it is saved into config.json;
//   4. the real EMS, dry and with every write gate off, reads the broker and
//      its dashboard API shows the inverter at 612.3 W.
// Every wait is on a response or a state the servers report, never a timer.
import { test, expect, type Page } from "@playwright/test";
import { spawn, type ChildProcess } from "node:child_process";
import { openSync, readFileSync } from "node:fs";
import { join } from "node:path";

const PASSWORD = "e2e-admin-password";

type Harness = { app: string; python: string; dashboard_port: number; broker_port: number; broker_ref: string; shelly_ids: Record<string, string> };

function harness(): Harness {
  return JSON.parse(readFileSync(process.env.EMS_MQTT_E2E_STATE as string, "utf8"));
}

async function csrf(page: Page): Promise<string> {
  return (await (await page.request.get("/api/admin/auth/status")).json()).csrf_token;
}

async function signIn(page: Page) {
  const setup = await page.request.post("/api/admin/auth/setup", { data: { password: PASSWORD, confirm_password: PASSWORD } });
  expect([200, 409]).toContain(setup.status());
  if (setup.status() === 409) {
    expect((await page.request.post("/api/admin/auth/login", { data: { password: PASSWORD } })).ok()).toBeTruthy();
  }
  await page.goto("/");
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
    await mapping.getByTestId("mqtt-mapping-broker").selectOption(world.broker_ref);
    await mapping.getByTestId("mqtt-topics-start").click();
    const plug = mapping.locator(`li[data-label="shelly_gen2"][data-key="${shellyId}"]`);
    await expect(plug).toContainText("Shelly (Gen2+)");
    const apower = plug.locator(".value-row").filter({ hasText: "apower" });
    await expect(apower).toContainText("-612.3");

    // 2. Answer what it measures, preview it, apply it.
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
    await wizard.getByTestId("mqtt-wizard-save").click();
    const applied = page.waitForResponse((response) => response.url().endsWith("/api/discovery/mqtt-mapping/apply"));
    await page.locator("dialog[open]").getByTestId("confirm").click();
    expect((await applied).ok()).toBeTruthy();
    await expect
      .poll(async () => {
        const view = await (await page.request.get("/api/discovery/mqtt-mapping")).json();
        return view.status?.devices?.find((item: { id: string }) => item.id === deviceId)?.publishing ?? false;
      }, { timeout: 60_000 })
      .toBe(true);

    // 3. Add it as a device from its row, through the same adoption Find devices uses.
    const saved = await page.request.post("/api/discovery/connections/mqtt-brokers", {
      data: { host: "127.0.0.1", port: world.broker_port, label: "E2E broker" },
      headers: { "X-CSRF-Token": await csrf(page) },
    });
    expect(saved.ok()).toBeTruthy();
    await page.reload();
    const row = page.getByTestId("mqtt-mapped-devices").locator("li").filter({ hasText: deviceId });
    await row.getByTestId("mqtt-adopt").click();
    await expect(row.getByTestId("mqtt-adopted")).toBeVisible({ timeout: 60_000 });
    await page.getByTestId("review").click();
    await expect(page.getByTestId("settings-review")).toContainText("Settings are valid");
    await page.getByTestId("apply").click();
    await page.getByRole("dialog", { name: "Save the checked changes?" }).getByTestId("confirm").click();
    await expect(page.getByTestId("settings-applied")).toBeVisible();
    const config = JSON.parse(readFileSync(join(world.app, "config", "config.json"), "utf8"));
    const added = config.devices.find((device: { mqtt?: { device_id?: string } }) => device.mqtt?.device_id === deviceId);
    expect(added).toMatchObject({ type: "external_mqtt", enabled: true });
    expect(config.system).toMatchObject({ dry_run: true, allow_hardware_writes: false, allow_mqtt_local_control_writes: false });

    // 4. The EMS reads it.
    const log = openSync(join(world.app, "data", "ems-e2e.log"), "w");
    ems = spawn(world.python, ["-B", "ems-solarflow-api-control.py", "--config", join(world.app, "config", "config.json")], {
      cwd: world.app,
      stdio: ["ignore", log, log],
    });
    await expect
      .poll(async () => {
        try {
          const live = await (await fetch(`http://127.0.0.1:${world.dashboard_port}/api/live`)).json();
          const device = live?.devices?.[added.name];
          return device ? { read_only: device.read_only, online: device.online, output_w: device.output_w } : null;
        } catch {
          return null;
        }
      }, { timeout: 90_000 })
      .toEqual({ read_only: true, online: true, output_w: 612.3 });
  } finally {
    if (ems && ems.exitCode === null) {
      const ended = new Promise((resolve) => ems?.once("exit", resolve));
      ems.kill("SIGTERM");
      await ended;
    }
  }
});
