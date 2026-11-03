// SPDX-License-Identifier: AGPL-3.0-or-later
import { SETTLE_MS, freshGroup, mappedProposal } from "../lib/mqtt.js";
import { adminGet, adminSend } from "./auth.svelte.js";

const BROKER = "/api/discovery/mqtt-broker";
const MAPPING = "/api/discovery/mqtt-mapping";
const TOPICS = "/api/discovery/mqtt-topics/sessions";
const BROKERS = `${MAPPING}/brokers`;
const MAPPING_POLL_MS = 5000;

/**
 * The built-in broker, the mapping file and one live-topic session. The
 * server switches, validates and calculates; this only shows what it answers
 * and sends what the operator asked for.
 */
class Mqtt {
  broker = $state.raw(null);
  mapping = $state.raw(null);
  brokers = $state.raw(null);
  topics = $state.raw(null);
  session = $state("");
  busy = $state("");
  result = $state.raw(null);
  error = $state.raw(null);
  waiting = $state.raw(null);
  topicsAt = $state(0);
  setup = $state.raw(null);
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

let mappingReads = 0;

/** The mapping and its service's status. Only the newest read lands, so a slow poll never puts back a document an apply replaced. */
export async function loadMapping() {
  const read = ++mappingReads;
  try {
    const answer = await adminGet(MAPPING);
    if (read === mappingReads) mqtt.mapping = answer;
  } catch (error) {
    if (read === mappingReads) mqtt.error = { name: "load", error };
  }
}

/** The brokers a mapping may read: built in, the EMS's profiles, remembered ones and those a search found. */
export async function loadBrokers() {
  try {
    mqtt.brokers = await adminGet(BROKERS);
  } catch (error) {
    mqtt.error = { name: "brokers", error };
  }
}

export const searchBrokers = () => run("search", () => send(`${BROKERS}/search`, {})).then(after(loadBrokers));
export const checkBroker = (ref) => run("check", () => send(`${BROKERS}/check`, { ref })).then(after(loadBrokers));
export const useBrokerAccount = (ref, username, password) => run("account", () => send(`${BROKERS}/account`, { ref, username, password })).then(after(loadBrokers));
export const addBroker = (form) => run("manual", () => send(`${BROKERS}/manual`, form)).then(after(loadBrokers));

export const enableBroker = () => run("enable", () => send(`${BROKER}/enable`, { confirm: true })).then(after(loadBroker)).then(after(loadBrokers));
export const stopBroker = () => run("stop", () => send(`${BROKER}/stop`, { confirm: true })).then(after(loadBroker)).then(after(loadBrokers));
export const saveBroker = (changes) => run("settings", () => send(`${BROKER}/settings`, changes)).then(after(loadBroker));
export const setPassword = (name, password) => run("password", () => send(`${BROKER}/password`, password ? { name, password } : { name })).then(after(loadBroker));
export const revealPassword = (name) => run("reveal", () => send(`${BROKER}/reveal`, { name }));
export const previewMapping = (document) => run("preview", () => send(`${MAPPING}/preview`, { document }));
export const applyMapping = (document) => run("apply", () => send(`${MAPPING}/apply`, { document, confirm: true })).then(after(loadMapping)).then(after(loadBrokers));
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
    mqtt.topicsAt = Date.now();
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

let watchers = 0;
let timer = null;

/** Polls the live topics every two seconds while anyone shows them; one timer for every component. */
export function watchTopics() {
  watchers += 1;
  if (!timer) timer = setInterval(pollTopics, 2000);
  return () => {
    watchers -= 1;
    if (watchers <= 0 && timer) {
      clearInterval(timer);
      timer = null;
      watchers = 0;
    }
  };
}

/** Starts the live topics on ``brokerRef`` and remembers what was there, so only a new device stands out. */
export async function waitForDevice(brokerRef) {
  const opening = !mqtt.session;
  const before = (mqtt.topics?.groups || []).map((group) => `${group.label}/${group.key}`);
  mqtt.waiting = { brokerRef, before, since: Date.now() + (opening ? SETTLE_MS : 0) };
  if (opening) await startTopics(brokerRef);
}

/** The first group that appeared since waiting began and can be mapped, or null. */
export function newGroup() {
  if (!mqtt.waiting) return null;
  return freshGroup(mqtt.topics?.groups, { before: mqtt.waiting.before, since: mqtt.waiting.since, polledAt: mqtt.topicsAt });
}

/** Hands a live-topic group to the mapping panel, which opens its questions for it. */
export function setUpGroup(group) {
  mqtt.waiting = null;
  mqtt.setup = group;
}

/** The proposal the server makes from a saved mapping for its device; no search runs. */
export async function findProposal(deviceId, brokerRef) {
  const data = await adminGet("/api/discovery/mqtt-proposals").catch(() => null);
  return mappedProposal(data?.proposals, deviceId, brokerRef);
}

let mappingWatchers = 0;
let mappingTimer = null;

/** Reads the mapping and the service's status every five seconds while anyone shows them, so a row says when its device publishes. */
export function watchMapping() {
  mappingWatchers += 1;
  if (!mappingTimer) mappingTimer = setInterval(() => mqtt.busy || loadMapping(), MAPPING_POLL_MS);
  return () => {
    mappingWatchers -= 1;
    if (mappingWatchers <= 0 && mappingTimer) {
      clearInterval(mappingTimer);
      mappingTimer = null;
      mappingWatchers = 0;
    }
  };
}
