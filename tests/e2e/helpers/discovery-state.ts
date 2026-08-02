import { expect, type Page } from "@playwright/test";
import type { DiscoveryStateSpec, LocalApiDeviceSpec } from "../fixtures/admin";

// Setup's device plan is computed from the deterministic Admin's *own*
// discovery state, so a browser journey has to put the devices there rather
// than answer /api/discovery/devices for the page. That is also what makes the
// ids line up: the server issues them, the page reads them back, and the plan
// names the same cards the operator sees.
//
// Which means a spec cannot know an issued id in advance. It describes devices
// by what an operator would recognize — a serial, a display name — and looks the
// issued ids up here.

export const SHELLY_METER: LocalApiDeviceSpec = {
  ip: "192.168.100.93",
  serial: "SHELLYE2E1",
  role: "grid_meter",
  api_family: "shelly_gen2",
  device_type: "shelly_pro_3em",
  model: "Shelly Pro 3EM",
};

export function apiInverter(serial: string, ip: string): LocalApiDeviceSpec {
  return {
    ip,
    serial,
    role: "inverter",
    api_family: "zendure_local_http",
    device_type: "zendure_solarflow_800_pro2",
    model: "SolarFlow 800 Pro 2",
  };
}

// A Zendure cloud account entry for one inverter, with the complete write route
// so the connection is control-capable.
export function cloudInverter(serial: string, productKey = "E2EPK") {
  return {
    broker_id: "zendure_cloud_mqtt:mqtt.zen-iot.com:8883",
    broker_host: "mqtt.zen-iot.com",
    broker_port: 8883,
    tls_mode: "encrypted_no_verify",
    source_type: "zendure_cloud_mqtt",
    topic_family: "zensdk_ha_scalar",
    device_id: serial,
    serial_number: serial,
    product_key: productKey,
    model_hint: "SolarFlow 800 Pro 2",
    display_name: "SolarFlow 800 Pro 2",
    metrics_seen: ["electricLevel", "outputHomePower", "outputLimit"],
  };
}

// A local broker publishing scalar telemetry for one inverter. Every other axis
// is complete, so this isolates the unproven-write-path case.
export function localScalarBroker(serials: string[], host = "192.168.100.60") {
  return {
    host,
    port: 1883,
    devices: serials.map((serial) => ({
      broker_id: `local_mqtt:${host}:1883`,
      broker_host: host,
      broker_port: 1883,
      source_type: "local_mqtt",
      topic_family: "zensdk_ha_scalar",
      device_id: serial,
      serial_number: serial,
      product_key: "E2EPK",
      model_hint: "SolarFlow 800 Pro 2",
      display_name: "SolarFlow 800 Pro 2",
      metrics_seen: ["electricLevel", "outputHomePower"],
      topics_seen: [`Zendure/sensor/${serial}/electricLevel`],
    })),
  };
}

/** The issued observation ids the server currently serves, keyed by serial. */
export async function servedObservations(page: Page): Promise<Map<string, string>> {
  const response = await page.request.get("/api/discovery/devices");
  expect(response.ok()).toBeTruthy();
  const body = await response.json();
  const byserial = new Map<string, string>();
  for (const device of body.devices || []) {
    const serial = String(device.serial_number || "");
    const issued = String(device.observation_id || "");
    if (serial && issued) byserial.set(serial, issued);
  }
  return byserial;
}

/** The current MQTT proposal ids, keyed by the serial they were observed for. */
export async function servedProposals(page: Page): Promise<Map<string, string>> {
  const response = await page.request.get("/api/discovery/mqtt-proposals");
  expect(response.ok()).toBeTruthy();
  const body = await response.json();
  const byserial = new Map<string, string>();
  for (const proposal of body.proposals || []) {
    const serial = String(proposal.serial_number || "");
    const id = String(proposal.id || "");
    if (serial && id) byserial.set(serial, id);
  }
  return byserial;
}

/** Save the operator's source order through the real preparation store. */
export async function setDiscoveryPriority(page: Page, priority: string[]) {
  const status = await page.request.get("/api/admin/auth/status");
  const auth = await status.json();
  const response = await page.request.post("/api/discovery/preparation", {
    headers: { "X-CSRF-Token": auth.csrf_token as string },
    data: { discovery_priority: priority },
  });
  expect(response.ok()).toBeTruthy();
}

export type { DiscoveryStateSpec, LocalApiDeviceSpec };
