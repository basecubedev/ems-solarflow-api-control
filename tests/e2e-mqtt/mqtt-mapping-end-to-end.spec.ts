// SPDX-License-Identifier: AGPL-3.0-or-later
// One run from a device in an unknown format to the EMS reading it, on a real
// Mosquitto (tests/e2e-mqtt/harness.py plays a Shelly Gen2 plug on it):
//   1. the live topics recognise the plug and label it Shelly (Gen2+);
//   2. it is mapped in the browser -- inverted, negatives as 0 -- and the
//      server's preview says 612.3 before the mapping is applied, which starts
//      the real mapping service (python -m mqtt_mapping);
//   3. Find devices discovers the published external device and it is added
//      and saved into config.json through the ordinary adoption;
//   4. the real EMS, dry and with every write gate off, reads the broker and
//      its dashboard API shows the inverter at 612.3 W.
// Every wait is on a response or a state the servers report, never a timer.
import { test, expect, type Page } from "@playwright/test";
import { spawn, type ChildProcess } from "node:child_process";
import { openSync, readFileSync } from "node:fs";
import { join } from "node:path";

const PASSWORD = "e2e-admin-password";

type Harness = { app: string; python: string; dashboard_port: number; broker_port: number; broker_ref: string; shelly_id: string };

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
  let ems: ChildProcess | null = null;
  try {
    await signIn(page);
    await page.goto("/#/settings/devices");

    // 1. Recognise the topics.
    const mapping = page.getByTestId("mqtt-mapping");
    await mapping.getByTestId("mqtt-mapping-broker").selectOption(world.broker_ref);
    await mapping.getByTestId("mqtt-topics-start").click();
    const plug = mapping.locator(`li[data-label="shelly_gen2"][data-key="${world.shelly_id}"]`);
    await expect(plug).toContainText("Shelly (Gen2+)");
    const apower = plug.locator(".value-row").filter({ hasText: "apower" });
    await expect(apower).toContainText("-612.3");

    // 2. Map it, preview it, apply it.
    await plug.getByTestId("mqtt-map-group").click();
    const editor = page.getByTestId("mqtt-mapping-editor");
    await expect(editor.getByTestId("mqtt-device-id")).toHaveValue(world.shelly_id);
    await apower.getByTestId("mqtt-use-value").click();
    await expect(editor).toContainText(`${world.shelly_id}/status/switch:0 → apower`);
    await editor.getByTestId("mqtt-source-invert").check();
    await editor.getByTestId("mqtt-source-negative").check();
    const previewed = page.waitForResponse((response) => response.url().endsWith("/api/discovery/mqtt-mapping/value-preview"));
    await editor.getByTestId("mqtt-device-preview").click();
    expect((await previewed).ok()).toBeTruthy();
    await expect(page.getByTestId("mqtt-preview-state")).toContainText('{"inverterPower":612.3}');
    await editor.getByTestId("mqtt-device-save").click();
    const applied = page.waitForResponse((response) => response.url().endsWith("/api/discovery/mqtt-mapping/apply"));
    await page.locator("dialog[open]").getByTestId("confirm").click();
    expect((await applied).ok()).toBeTruthy();
    await expect
      .poll(async () => {
        const view = await (await page.request.get("/api/discovery/mqtt-mapping")).json();
        return view.status?.devices?.find((item: { id: string }) => item.id === world.shelly_id)?.publishing ?? false;
      }, { timeout: 60_000 })
      .toBe(true);

    // 3. Find the published device and add it through the ordinary adoption.
    const saved = await page.request.post("/api/discovery/connections/mqtt-brokers", {
      data: { host: "127.0.0.1", port: world.broker_port, label: "E2E broker" },
      headers: { "X-CSRF-Token": await csrf(page) },
    });
    expect(saved.ok()).toBeTruthy();
    await page.reload();
    await page.getByTestId("discovery-start").click();
    await expect(page.getByTestId("discovery-outcome")).toBeVisible({ timeout: 60_000 });
    const found = page.getByTestId("discovery-items").locator("li").filter({ hasText: world.shelly_id });
    await found.getByRole("button", { name: "Add inverter" }).click();
    await page.getByTestId("review").click();
    await expect(page.getByTestId("settings-review")).toContainText("Settings are valid");
    await page.getByTestId("apply").click();
    await page.getByRole("dialog", { name: "Save the checked changes?" }).getByTestId("confirm").click();
    await expect(page.getByTestId("settings-applied")).toBeVisible();
    const config = JSON.parse(readFileSync(join(world.app, "config", "config.json"), "utf8"));
    const added = config.devices.find((device: { mqtt?: { device_id?: string } }) => device.mqtt?.device_id === world.shelly_id);
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
