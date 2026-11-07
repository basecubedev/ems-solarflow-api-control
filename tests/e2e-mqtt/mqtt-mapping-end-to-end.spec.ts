// SPDX-License-Identifier: AGPL-3.0-or-later
// One run from a device in an unknown format to the EMS reading it, on a real
// Mosquitto (tests/e2e-mqtt/harness.py plays a Shelly Gen2 plug on it), taken
// the way a user takes it: every step is a click on the Admin console's page,
// and the result is read off the EMS dashboard's page.
//   1. the live topics recognise the plug, fold it to one row that says Shelly
//      (Gen2+), its power and Not mapped, and the filter finds it by its id;
//   2. its button opens a dialog named after it, in view, where it is set up by
//      answering what it measures (a balcony plant); the negative reading is
//      turned round on its own, the server's preview says 612.3, and one save
//      writes the mapping, which starts the real mapping service
//      (python -m mqtt_mapping), and adds its device to the EMS;
//   3. the dialog goes on to say the device is saved and in the EMS, which
//      reads it once it restarts, and offers that restart; its button stays off
//      here and says why: the server's container plan cannot run it, as this
//      run starts EMS as a process, not as the container a restart recreates.
//      Closed, it leaves the device's row in view and focused, which says it is
//      in the EMS (no EMS runs yet, so nothing says what it reads) and then
//      that its first value arrived; beside it the restart stays offered, and
//      the plug's live row says it is mapped;
//   4. the real EMS, dry and with every write gate off, reads the broker, and
//      its dashboard shows the device online at 612 W; its control snapshot
//      names the device it read, so the console's row says Active in the EMS;
//   5. Edit on the plug's live row opens its mapping in the dialog with the
//      plug's own values first and the one in use marked; a new name renames
//      the EMS device, and removing the mapping removes the EMS device with it,
//      each ending in the same step; the row and the restart offer say what
//      the running EMS has yet to read, under the name it runs, and the
//      restart left for later stays offered in view, with the focus on it.
// A second run takes a plug under a washing machine the same way as a consumer,
// goes on from the step to the next device, renames it and carries what it
// drew along to its new name.
// Every wait is on a response or a state the page shows, never a timer.
import { test, expect, type Page } from "@playwright/test";
import { spawn, type ChildProcess } from "node:child_process";
import { openSync, readFileSync, rmSync } from "node:fs";
import { join } from "node:path";

const PASSWORD = "e2e-admin-password";

type Harness = {
  app: string;
  python: string;
  dashboard_port: number;
  broker_port: number;
  broker_ref: string;
  shelly_ids: Record<string, string>;
  washer_ids: Record<string, string>;
};

function harness(): Harness {
  return JSON.parse(readFileSync(process.env.EMS_MQTT_E2E_STATE as string, "utf8"));
}

/** No EMS runs: the snapshot an EMS stopped by an earlier run left behind would still count as fresh for a minute. */
function noEmsRunning(world: Harness) {
  rmSync(join(world.app, "data", "control-status.json"), { force: true });
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

/** Starts the real EMS on the harness installation and opens its dashboard once it answers. */
async function startEms(page: Page, world: Harness, hash: string) {
  const log = openSync(join(world.app, "data", "ems-e2e.log"), "w");
  const ems = spawn(world.python, ["-B", "ems-solarflow-api-control.py", "--config", join(world.app, "config", "config.json")], {
    cwd: world.app,
    stdio: ["ignore", log, log],
  });
  const dashboard = await page.context().newPage();
  await expect
    .poll(async () => {
      try {
        await dashboard.goto("about:blank");
        await dashboard.goto(`http://127.0.0.1:${world.dashboard_port}/${hash}`);
        await dashboard.getByTestId("ems-state").waitFor({ timeout: 5_000 });
        return true;
      } catch {
        return false;
      }
    }, { timeout: 90_000 })
    .toBe(true);
  return { ems, dashboard };
}

/** An energy the dashboard shows in English ("998 Wh", "1.2 kWh", "1,234 kWh") as watt-hours. */
function wattHours(text: string | null) {
  const match = (text || "").replace(/,/g, "").match(/([\d.]+)\s*(k?)Wh/);
  return match ? Number(match[1]) * (match[2] ? 1000 : 1) : 0;
}

async function stopEms(ems: ChildProcess | null) {
  if (ems && ems.exitCode === null) {
    const ended = new Promise((resolve) => ems.once("exit", resolve));
    ems.kill("SIGTERM");
    await ended;
  }
}

test("a Shelly plug in an unknown format is mapped, added and read by the EMS", async ({ page }) => {
  const world = harness();
  const project = test.info().project.name;
  const shellyId = world.shelly_ids[project];
  const deviceName = `Balkon ${project}`;
  const deviceId = `balkon-${project}`;
  let ems: ChildProcess | null = null;
  try {
    noEmsRunning(world);
    await signIn(page);
    await page.goto("/#/settings/devices");

    // 1. Recognise the topics.
    const mapping = page.getByTestId("mqtt-mapping");
    await mapping.locator(`[data-testid="mqtt-broker-choice"][data-ref="${world.broker_ref}"] input[type=radio]`).check();
    await mapping.getByTestId("mqtt-topics-start").click();
    const plug = mapping.locator(`li[data-label="shelly_gen2"][data-key="${shellyId}"]`);
    await expect(plug).toContainText("Shelly (Gen2+)");
    await expect(plug).toContainText("apower -612.3");
    await expect(plug).toContainText("Not mapped");
    await mapping.getByTestId("mqtt-topics-filter").fill(shellyId);
    await expect(mapping.locator("li.group")).toHaveCount(1);
    await plug.getByRole("button", { name: new RegExp(shellyId) }).click();
    const apower = plug.locator(".value-row").filter({ hasText: "apower" });
    await expect(apower).toContainText("-612.3");

    // 2. Answer what it measures, preview it, and save the mapping with its EMS device.
    await plug.getByTestId("mqtt-map-group").click();
    const mapDialog = page.getByRole("dialog", { name: `Map ${shellyId} as an external device` });
    await expect(mapDialog).toBeInViewport();
    const wizard = mapDialog.getByTestId("mqtt-wizard");
    await expect(wizard).toContainText("What does this device measure?");
    await wizard.getByTestId("mqtt-kind-balcony").check();
    await wizard.getByTestId("mqtt-wizard-name").fill(deviceName);
    await expect(wizard.getByTestId("mqtt-wizard-power")).toHaveValue(`${shellyId}/status/switch:0|apower`);
    await expect(wizard.getByTestId("mqtt-wizard-turned")).toBeVisible();
    const previewed = page.waitForResponse((response) => response.url().endsWith("/api/discovery/mqtt-mapping/value-preview"));
    await mapDialog.getByTestId("mqtt-wizard-preview").click();
    expect((await previewed).ok()).toBeTruthy();
    await expect(mapDialog.getByTestId("mqtt-preview-state")).toContainText('{"inverterPower":612.3,"solarPower":612.3}');
    await expect(mapDialog.getByTestId("mqtt-wizard-save")).toHaveText("Save and add to the EMS");
    await mapDialog.getByTestId("mqtt-wizard-save").click();
    const confirmation = page.getByRole("dialog", { name: "Apply the mapping?" });
    await expect(confirmation).toContainText("Its device is added to the settings at once");
    await confirmation.getByTestId("confirm").click();

    // 3. The dialog says it is in the EMS once EMS restarts and why that
    //    restart cannot run here; closed, its row, in view, says it is in the
    //    EMS, then that its first value arrived, with the restart offered
    //    beside it.
    const step = mapDialog.getByTestId("mqtt-saved");
    await expect(step).toContainText("Saved and added to the EMS", { timeout: 60_000 });
    await expect(step).toBeInViewport();
    await expect(step).toContainText(`${deviceName} becomes active once EMS restarts.`);
    await expect(step.getByTestId("plan-unavailable")).toContainText("Docker or the container setup is not available");
    await expect(mapDialog.getByTestId("restart-ems")).toBeDisabled();
    await expect(mapDialog.getByTestId("mqtt-saved-another")).toBeFocused();
    await mapDialog.getByRole("button", { name: "Close" }).click();
    await expect(mapDialog).toBeHidden();
    const dialog = page.locator("dialog[open]");
    const row = page.getByTestId("mqtt-mapped-devices").locator(`li[data-id="${deviceId}"]`);
    await expect(row).toBeInViewport();
    await expect(row).toBeFocused();
    await expect(row.getByTestId("mqtt-adopted")).toHaveText("In the EMS");
    const restart = mapping.getByTestId("restart-offer");
    await expect(restart).toBeInViewport();
    await expect(restart).toContainText("Some changes take effect once EMS restarts.");
    await expect(restart.getByTestId("plan-unavailable")).toContainText("Docker or the container setup is not available");
    await expect(restart.getByTestId("restart-ems")).toBeDisabled();
    await expect(row).toContainText("Published", { timeout: 60_000 });
    await expect(plug).toContainText(`Mapped: ${deviceId}`);
    await expect(plug).not.toContainText("Waits for an EMS restart");
    await expect(mapping.getByTestId("mqtt-steps").locator('li[data-step="ems"]')).toHaveClass(/done/);
    const config = JSON.parse(readFileSync(join(world.app, "config", "config.json"), "utf8"));
    const added = config.devices.find((device: { mqtt?: { device_id?: string } }) => device.mqtt?.device_id === deviceId);
    expect(added).toMatchObject({ type: "external_mqtt", enabled: true, name: deviceName });
    expect(config.system).toMatchObject({ dry_run: true, allow_hardware_writes: false, allow_mqtt_local_control_writes: false });

    // 4. The EMS reads it.
    const started = await startEms(page, world, "#/devices");
    ems = started.ems;
    const dashboard = started.dashboard;
    const card = dashboard.locator(`[data-testid="device-card"][data-device="${deviceName}"]`);
    await expect(card).toHaveAttribute("data-state", "online", { timeout: 90_000 });
    await expect(card).toContainText("Telemetry only");
    await expect(card).toContainText("612\u00a0W", { timeout: 30_000 });
    await expect(row.getByTestId("mqtt-adopted")).toHaveText("Active in the EMS", { timeout: 60_000 });

    // 5. A new name in the mapping renames its EMS device, and removing the
    //    mapping can take the EMS device with it.
    const configured = () => JSON.parse(readFileSync(join(world.app, "config", "config.json"), "utf8")).devices;
    const renamed = `${deviceName} South`;
    await plug.getByTestId("mqtt-edit-group").click();
    const editDialog = page.getByRole("dialog", { name: `Edit ${deviceId}` });
    await expect(editDialog).toBeInViewport();
    await expect(editDialog.locator("li.group").first()).toHaveAttribute("data-key", shellyId);
    await expect(editDialog.locator(`li[data-key="${shellyId}"] .value-row`).filter({ hasText: "apower" }).getByTestId("mqtt-use-value")).toHaveText("Used as Inverter power");
    await editDialog.getByTestId("mqtt-mapping-editor").getByLabel("Name").fill(renamed);
    await editDialog.getByTestId("mqtt-device-save").click();
    await expect(confirmation).toContainText(`The EMS device ${deviceName} is renamed to ${renamed};`);
    await confirmation.getByTestId("confirm").click();
    await expect(editDialog.getByTestId("mqtt-saved")).toContainText("Saved; its EMS device is renamed", { timeout: 60_000 });
    await expect(editDialog.getByTestId("mqtt-saved")).toContainText(`EMS shows the new name ${renamed} once it restarts.`);
    await editDialog.getByRole("button", { name: "Close" }).click();
    await expect(editDialog).toBeHidden();
    await expect(row).toBeInViewport();
    await expect(row).toContainText(renamed);
    await expect(row.getByTestId("mqtt-adopted")).toHaveText("Waits for an EMS restart");
    await expect(restart).toContainText(`Waiting for an EMS restart: ${renamed} (renamed)`);
    await expect.poll(() => configured().find((device: { mqtt?: { device_id?: string } }) => device.mqtt?.device_id === deviceId)?.name).toBe(renamed);

    const savedBefore = await page.getByTestId("settings-applied").textContent();
    await row.getByRole("button", { name: "Remove" }).click();
    await expect(dialog.getByTestId("mqtt-remove-ems")).toBeChecked();
    await expect(dialog).toContainText(`Also remove the EMS device ${renamed}`);
    await dialog.getByTestId("confirm").click();
    const removed = page.getByRole("dialog", { name: `Remove ${renamed}` });
    await expect(removed.getByTestId("mqtt-saved")).toContainText("Removed from the mapping and from the EMS", { timeout: 60_000 });
    await expect(removed.getByTestId("mqtt-saved")).toContainText(`EMS keeps reading ${deviceName} until it restarts.`);
    await removed.getByTestId("mqtt-saved-later").click();
    await expect(removed).toBeHidden();
    await expect(row).toHaveCount(0);
    await expect(page.getByTestId("settings-applied")).not.toHaveText(savedBefore ?? "");
    await expect(restart).toBeInViewport();
    await expect(restart).toContainText(`Waiting for an EMS restart: ${deviceName} (removed)`);
    await expect(restart.getByRole("link", { name: "Follow it in Services" })).toBeFocused();
    await expect(restart.getByTestId("restart-ems")).toBeDisabled();
    await expect(plug).toContainText("Not mapped");
    await expect.poll(() => configured().some((device: { mqtt?: { device_id?: string } }) => device.mqtt?.device_id === deviceId)).toBe(false);
  } finally {
    await stopEms(ems);
  }
});

// The same way for a consumer: a Shelly plug under a washing machine is mapped
// by answering that it is a consumer, saved with its EMS device, read by the
// real EMS, and shown on the dashboard's Consumers page and in the Overview's
// energy flow -- never as an inverter. Renamed in the mapping, the console asks
// about what it drew, and the EMS counts it on under the new name.
test("a Shelly plug under a washing machine is mapped as a consumer and shown where the house's power goes", async ({ page }) => {
  const world = harness();
  const project = test.info().project.name;
  const washerId = world.washer_ids[project];
  const consumerName = `Washer ${project}`;
  const deviceId = `washer-${project}`;
  let ems: ChildProcess | null = null;
  try {
    noEmsRunning(world);
    await signIn(page);
    await page.goto("/#/settings/devices");

    // 1. Recognise the plug.
    const mapping = page.getByTestId("mqtt-mapping");
    await mapping.locator(`[data-testid="mqtt-broker-choice"][data-ref="${world.broker_ref}"] input[type=radio]`).check();
    await mapping.getByTestId("mqtt-topics-start").click();
    const plug = mapping.locator(`li[data-label="shelly_gen2"][data-key="${washerId}"]`);
    await expect(plug).toContainText("apower 437.2");

    // 2. Answer that it is a consumer, preview it, and save it with its EMS device.
    await plug.getByTestId("mqtt-map-group").click();
    const mapDialog = page.getByRole("dialog", { name: `Map ${washerId} as an external device` });
    await expect(mapDialog).toBeInViewport();
    const wizard = mapDialog.getByTestId("mqtt-wizard");
    await wizard.getByTestId("mqtt-kind-consumer").check();
    await expect(wizard).toContainText("Which value is the power it draws?");
    await wizard.getByTestId("mqtt-wizard-name").fill(consumerName);
    await expect(wizard.getByTestId("mqtt-wizard-power")).toHaveValue(`${washerId}/status/switch:0|apower`);
    await expect(wizard.getByTestId("mqtt-wizard-turned")).toHaveCount(0);
    const previewed = page.waitForResponse((response) => response.url().endsWith("/api/discovery/mqtt-mapping/value-preview"));
    await mapDialog.getByTestId("mqtt-wizard-preview").click();
    expect((await previewed).ok()).toBeTruthy();
    await expect(mapDialog.getByTestId("mqtt-preview-state")).toContainText('{"power":437.2}');
    await mapDialog.getByTestId("mqtt-wizard-save").click();
    const confirmation = page.getByRole("dialog", { name: "Apply the mapping?" });
    await confirmation.getByTestId("confirm").click();
    await expect(mapDialog.getByTestId("mqtt-saved")).toContainText("Saved and added to the EMS", { timeout: 60_000 });
    await mapDialog.getByTestId("mqtt-saved-another").click();
    await expect(mapDialog).toBeHidden();
    const search = mapping.getByTestId("mqtt-topics-filter");
    await expect(search).toBeFocused();
    await expect(search).toBeInViewport();
    const dialog = page.locator("dialog[open]");

    // 3. It is in the EMS as a consumer, and its value arrived.
    const row = page.getByTestId("mqtt-mapped-devices").locator(`li[data-id="${deviceId}"]`);
    await expect(row.getByTestId("mqtt-adopted")).toHaveText("In the EMS");
    await expect(row).toContainText("Published", { timeout: 60_000 });
    const config = JSON.parse(readFileSync(join(world.app, "config", "config.json"), "utf8"));
    const added = config.devices.find((device: { mqtt?: { device_id?: string } }) => device.mqtt?.device_id === deviceId);
    expect(added).toMatchObject({ type: "external_mqtt", enabled: true, name: consumerName, mqtt: { topic_family: "ems_consumer" } });
    await page.reload();
    const card = page.locator("section.device").filter({ hasText: consumerName });
    await expect(card.locator(".summary .name")).toHaveText(/^Consumer \d+$/);

    // 4. The EMS reads it, and the dashboard shows it as a consumer, not as an inverter:
    //    ranked on its page, and in the Overview's flow as the consumer block,
    //    which leads back to that page.
    let started = await startEms(page, world, "#/consumers");
    ems = started.ems;
    let dashboard = started.dashboard;
    const ranked = dashboard.locator(`[data-testid="consumer-row"][data-consumer="${consumerName}"]`);
    await expect(ranked.getByTestId("consumer-power")).toHaveText("437\u00a0W", { timeout: 90_000 });
    await expect(row.getByTestId("mqtt-adopted")).toHaveText("Active in the EMS", { timeout: 60_000 });
    const flow = dashboard.getByTestId("energy-flow");
    await dashboard.goto(`http://127.0.0.1:${world.dashboard_port}/#/overview`);
    await expect(flow.getByTestId("flow-consumers")).toContainText("437\u00a0W");
    await flow.getByTestId("flow-consumers").click();
    await expect(dashboard).toHaveURL(/#\/consumers$/);
    await expect(ranked.locator(".c-today")).not.toHaveText("0\u00a0Wh", { timeout: 90_000 });
    const drawn = await ranked.locator(".c-today").textContent();
    await dashboard.goto(`http://127.0.0.1:${world.dashboard_port}/#/devices`);
    await expect(dashboard.getByTestId("grid-meter")).toBeVisible();
    await expect(dashboard.locator(`[data-testid="device-card"][data-device="${consumerName}"]`)).toHaveCount(0);

    // 5. A new name asks about what it drew, and carries it along when told to:
    //    the EMS, started again, counts it under the new name and no longer
    //    under the old one.
    const renamed = `Waschmaschine ${project}`;
    await row.getByRole("button", { name: "Edit" }).click();
    const editDialog = page.getByRole("dialog", { name: `Edit ${deviceId}` });
    await editDialog.getByTestId("mqtt-mapping-editor").getByLabel("Name").fill(renamed);
    await editDialog.getByTestId("mqtt-device-save").click();
    await expect(confirmation).toContainText(`The EMS device ${consumerName} is renamed to ${renamed};`);
    await confirmation.getByTestId("confirm").click();
    const question = page.locator("dialog[open]").filter({ has: page.getByTestId("consumer-history") });
    await expect(question).toContainText(`${consumerName} is now called ${renamed}.`);
    await question.getByRole("button", { name: "Carry them along" }).click();
    await question.getByTestId("history-confirm").click();
    await expect(editDialog.getByTestId("mqtt-saved")).toContainText("Saved; its EMS device is renamed", { timeout: 60_000 });
    await expect(editDialog.getByTestId("mqtt-saved")).toContainText(`EMS shows the new name ${renamed} once it restarts.`);
    await editDialog.getByRole("button", { name: "Close" }).click();
    await expect(row).toContainText(renamed);
    const named = () => JSON.parse(readFileSync(join(world.app, "config", "config.json"), "utf8")).devices.find((device: { mqtt?: { device_id?: string } }) => device.mqtt?.device_id === deviceId);
    await expect.poll(() => named()?.name).toBe(renamed);
    expect(named().history_id).toBeUndefined();
    await stopEms(ems);
    await dashboard.close();
    started = await startEms(page, world, "#/consumers");
    ems = started.ems;
    dashboard = started.dashboard;
    const carried = dashboard.getByTestId("consumer-energy").locator(`tr[data-consumer="${renamed}"]`);
    await expect(carried).toBeVisible({ timeout: 90_000 });
    await expect.poll(async () => wattHours(await carried.locator("td").nth(1).textContent())).toBeGreaterThanOrEqual(wattHours(drawn));
    await expect(dashboard.getByTestId("consumer-energy").locator(`tr[data-consumer="${consumerName}"]`)).toHaveCount(0);

    // 6. Removing the mapping takes the consumer out of the EMS.
    await row.getByRole("button", { name: "Remove" }).click();
    await expect(dialog.getByTestId("mqtt-remove-ems")).toBeChecked();
    await dialog.getByTestId("confirm").click();
    const removed = page.getByRole("dialog", { name: `Remove ${renamed}` });
    await expect(removed.getByTestId("mqtt-saved")).toContainText("Removed from the mapping and from the EMS", { timeout: 60_000 });
    await removed.getByTestId("mqtt-saved-later").click();
    await expect(row).toHaveCount(0);
    await expect
      .poll(() => JSON.parse(readFileSync(join(world.app, "config", "config.json"), "utf8")).devices.some((device: { mqtt?: { device_id?: string } }) => device.mqtt?.device_id === deviceId))
      .toBe(false);
  } finally {
    await stopEms(ems);
  }
});
