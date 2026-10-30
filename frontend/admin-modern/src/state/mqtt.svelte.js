// SPDX-License-Identifier: AGPL-3.0-or-later
import { adminGet, adminSend } from "./auth.svelte.js";

const BROKER = "/api/discovery/mqtt-broker";
const MAPPING = "/api/discovery/mqtt-mapping";
const TOPICS = "/api/discovery/mqtt-topics/sessions";

/**
 * The built-in broker, the mapping file and one live-topic session. The
 * server switches, validates and calculates; this only shows what it answers
 * and sends what the operator asked for.
 */
class Mqtt {
  broker = $state.raw(null);
  mapping = $state.raw(null);
  topics = $state.raw(null);
  session = $state("");
  busy = $state("");
  result = $state.raw(null);
  error = $state.raw(null);
}

export const mqtt = new Mqtt();

let route = null;

/** Guided Setup's discovery aliases while the setup page shows this; ``null`` goes back. */
export function routeMqtt(next) {
  route = next || null;
}

function send(path, body) {
  if (!route) return adminSend(path, { body });
  return adminSend(route.path(path), { body, headers: route.headers() });
}

async function run(name, task) {
  if (mqtt.busy) return null;
  mqtt.busy = name;
  mqtt.error = null;
  try {
    const answer = await task();
    mqtt.result = { name, answer, at: Date.now() };
    return answer;
  } catch (error) {
    mqtt.error = { name, error, payload: error?.payload || null };
    return null;
  } finally {
    mqtt.busy = "";
  }
}

export async function loadBroker() {
  try {
    mqtt.broker = await adminGet(BROKER);
  } catch (error) {
    mqtt.error = { name: "load", error };
  }
}

export async function loadMapping() {
  try {
    mqtt.mapping = await adminGet(MAPPING);
  } catch (error) {
    mqtt.error = { name: "load", error };
  }
}

export const enableBroker = () => run("enable", () => send(`${BROKER}/enable`, { confirm: true })).then(after(loadBroker));
export const stopBroker = () => run("stop", () => send(`${BROKER}/stop`, { confirm: true })).then(after(loadBroker));
export const saveBroker = (changes) => run("settings", () => send(`${BROKER}/settings`, changes)).then(after(loadBroker));
export const setPassword = (name, password) => run("password", () => send(`${BROKER}/password`, password ? { name, password } : { name })).then(after(loadBroker));
export const revealPassword = (name) => run("reveal", () => send(`${BROKER}/reveal`, { name }));
export const previewMapping = (document) => run("preview", () => send(`${MAPPING}/preview`, { document }));
export const applyMapping = (document) => run("apply", () => send(`${MAPPING}/apply`, { document, confirm: true })).then(after(loadMapping));
export const previewValue = (device) => run("value", () => send(`${MAPPING}/value-preview`, mqtt.session ? { device, session: mqtt.session } : { device }));

function after(reload) {
  return async (answer) => {
    await reload();
    return answer;
  };
}

export async function startTopics(brokerRef) {
  const answer = await run("topics", () => send(TOPICS, { broker_ref: brokerRef }));
  if (answer?.session) {
    mqtt.session = answer.session;
    await pollTopics();
  }
  return answer;
}

export async function pollTopics() {
  if (!mqtt.session) return;
  try {
    mqtt.topics = await adminGet(`${TOPICS}/${encodeURIComponent(mqtt.session)}`);
  } catch (error) {
    mqtt.session = "";
    mqtt.topics = null;
    mqtt.error = { name: "topics", error };
  }
}

export async function stopTopics() {
  const session = mqtt.session;
  mqtt.session = "";
  mqtt.topics = null;
  if (session) await send(`${TOPICS}/${encodeURIComponent(session)}/stop`, {}).catch(() => null);
}
