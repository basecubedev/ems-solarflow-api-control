// SPDX-License-Identifier: AGPL-3.0-or-later
import { adminGet, adminSend } from "./auth.svelte.js";

const CREDENTIALS = "/api/discovery/connections/mqtt-credentials";
const CLOUD = "/api/discovery/zendure-cloud-mqtt";

/**
 * What the network search may use: saved local MQTT broker credentials and
 * the Zendure cloud key. Both are written to the server, which keeps them
 * encrypted, and are never read back; the lists show only that they are set.
 */
class Sources {
  credentials = $state.raw(null);
  cloud = $state.raw(null);
  cloudDevices = $state.raw(null);
  busy = $state("");
  message = $state.raw(null);
}

export const sources = new Sources();

let route = null;

/**
 * Sends this panel's writes through Guided Setup's discovery routes, which
 * name the confirmed setup operation, while the setup page shows it; ``null``
 * goes back to the maintenance routes.
 */
export function routeSources(next) {
  route = next || null;
}

function send(path, options = {}) {
  if (!route) return adminSend(path, options);
  return adminSend(route.path(path), { ...options, headers: { ...(options.headers || {}), ...route.headers() } });
}

function say(kind, values = {}) {
  sources.message = { kind, values, at: Date.now() };
}

function fail(kind, error) {
  sources.message = { kind, error, at: Date.now() };
}

async function run(name, task) {
  if (sources.busy) return false;
  sources.busy = name;
  try {
    await task();
    return true;
  } finally {
    sources.busy = "";
  }
}

export async function loadSources() {
  try {
    sources.credentials = (await adminGet(CREDENTIALS))?.credentials || [];
  } catch (error) {
    fail("credentialsUnreadable", error);
  }
  try {
    sources.cloud = await adminGet(`${CLOUD}/settings`);
  } catch (error) {
    fail("cloudUnreadable", error);
  }
}

/** Saves a credential and answers the saved list; the reference names it in a broker profile. */
export async function saveCredential({ label, username, password }) {
  let saved = null;
  await run("credential", async () => {
    try {
      const data = await send(CREDENTIALS, { body: { label, username, password } });
      sources.credentials = data?.local_mqtt?.credentials || sources.credentials;
      saved = sources.credentials;
      say("credentialSaved");
    } catch (error) {
      fail("credentialFailed", error);
    }
  });
  return saved;
}

export async function removeCredential(id) {
  await run(`remove:${id}`, async () => {
    try {
      await send(`/api/discovery/connections/mqtt-credentials/${encodeURIComponent(id)}`, { method: "DELETE" });
      say("credentialRemoved");
      sources.credentials = (await adminGet(CREDENTIALS))?.credentials || [];
    } catch (error) {
      fail("credentialRemoveFailed", error);
    }
  });
}

export async function refreshBrokers() {
  await run("brokers", async () => {
    try {
      const data = await send("/api/discovery/mqtt-brokers/refresh", { body: {} });
      say("brokersRefreshed", { reachable: Number(data?.reachable) || 0, devices: Number(data?.devices_found) || 0 });
    } catch (error) {
      fail("brokersFailed", error);
    }
  });
}

async function reloadCloud() {
  try {
    sources.cloud = await adminGet(`${CLOUD}/settings`);
  } catch (error) {
    fail("cloudUnreadable", error);
  }
}

/** Saves the key; answers whether it was saved. */
export async function saveCloudKey(key) {
  let saved = false;
  await run("cloudSave", async () => {
    try {
      const data = await send(`${CLOUD}/token`, { body: { api_key: key } });
      if (data?.ok === false) throw Object.assign(new Error(data.message || "save failed"), { payload: data });
      saved = true;
      say("cloudSaved");
    } catch (error) {
      fail("cloudSaveFailed", error);
    }
    await reloadCloud();
  });
  return saved;
}

/** Tests the typed key, or the saved one when nothing is typed. */
export async function testCloudKey(key) {
  await run("cloudTest", async () => {
    try {
      const data = await send(`${CLOUD}/test`, { body: key ? { api_key: key } : {} });
      if (data?.ok === false) throw Object.assign(new Error(data.message || "test failed"), { payload: data });
      say("cloudTested", { devices: Number(data?.devices_found) || 0, broker: data?.broker || "", tls: data?.tls_mode || "system_ca" });
    } catch (error) {
      fail("cloudTestFailed", error);
    }
    await reloadCloud();
  });
}

export async function refreshCloud() {
  await run("cloudRefresh", async () => {
    try {
      const data = await send(`${CLOUD}/refresh`, { body: {} });
      if (data?.ok === false) throw Object.assign(new Error(data.message || "refresh failed"), { payload: data });
      sources.cloudDevices = Array.isArray(data?.candidates) ? data.candidates : [];
      say("cloudRefreshed", { listed: Number(data?.device_list_count) || 0, observed: Number(data?.mqtt_observed_count) || 0 });
    } catch (error) {
      fail("cloudRefreshFailed", error);
    }
    await reloadCloud();
  });
}

export async function forgetCloudKey() {
  await run("cloudForget", async () => {
    try {
      const data = await send(`${CLOUD}/token`, { method: "DELETE" });
      if (data?.ok === false) throw Object.assign(new Error(data.message || "delete failed"), { payload: data });
      sources.cloudDevices = null;
      say("cloudForgotten");
    } catch (error) {
      fail("cloudForgetFailed", error);
    }
    await reloadCloud();
  });
}
