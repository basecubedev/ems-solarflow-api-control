// SPDX-License-Identifier: AGPL-3.0-or-later
import { adminGet, adminSend } from "./auth.svelte.js";
import { setup, goTo } from "./setup.svelte.js";
import { refreshCloud } from "./sources.svelte.js";
import { mergeObservation, observationKey, scanTarget } from "../lib/discovery.js";
import { aggregate } from "../lib/setupDraft.js";

const POLL_MS = 1200;
const SCAN_LIMIT_MS = 120000;
const MDNS_POLL_MS = 20000;

/** The discovery sources, in their default priority. */
export const SOURCES = Object.freeze(["local_api", "local_mqtt", "zendure_mqtt"]);
const GATE_ERRORS = new Set(["system_build_alignment_required", "setup_operation_required", "operation_mismatch", "system_alignment_incomplete", "system_build_mismatch"]);

/**
 * Device discovery during Guided Setup. Mutations go to the setup aliases of
 * the discovery routes and carry the confirmed operation, which the server
 * checks; reads use the shared routes. Held here so a step change keeps it.
 */
class SetupDiscovery {
  preparation = $state.raw({ discovery_priority: [...SOURCES], sources: Object.fromEntries(SOURCES.map((source) => [source, { enabled: true }])) });
  networks = $state.raw([]);
  gateways = $state.raw([]);
  networkWarnings = $state.raw([]);
  gatewayState = $state("");
  detecting = $state(false);
  found = $state.raw({});
  mdnsDevices = $state.raw({});
  scans = $state.raw([]);
  progress = $state.raw({ total: 0, done: 0, failed: 0, active: 0 });
  unified = $state.raw(null);
  running = $state(false);
  scanning = $state(false);
  hasRun = $state(false);
  mdns = $state.raw(null);
  ignored = $state.raw([]);
  brokers = $state.raw([]);
  brokerMessage = $state.raw(null);
  proposals = $state.raw([]);
  proposalsError = $state.raw(null);
  gateError = $state.raw(null);
  generation = 0;
  session = 0;
  scanned = new Set();
  started = false;

  /** What every source found: mDNS observations merged with the scans this browser ran. */
  devices = $derived(aggregate(this.mdnsDevices, this.found));
}

export const setupDiscovery = new SetupDiscovery();

function headers() {
  return setup.operation?.operationId ? { "X-Setup-Operation-ID": setup.operation.operationId } : {};
}

function setupPath(path) {
  return path.replace(/^\/api\/discovery/, "/api/setup/discovery");
}

/** Where Guided Setup sends a discovery write, and what it names: the confirmed operation. */
export const setupRoute = Object.freeze({ path: setupPath, headers });

function send(path, body = {}) {
  return adminSend(setupPath(path), { body, headers: headers() });
}

function normalize(data) {
  const priority = [];
  for (const source of data?.discovery_priority || []) if (SOURCES.includes(source) && !priority.includes(source)) priority.push(source);
  for (const source of SOURCES) if (!priority.includes(source)) priority.push(source);
  return { discovery_priority: priority, sources: Object.fromEntries(priority.map((source) => [source, { enabled: data?.sources?.[source]?.enabled !== false }])) };
}

export function sourceEnabled(source) {
  return setupDiscovery.preparation.sources?.[source]?.enabled !== false;
}

export async function loadPreparation() {
  try {
    setupDiscovery.preparation = normalize(await adminGet("/api/discovery/preparation"));
  } catch {
    return;
  }
}

async function persistPreparation(next) {
  setupDiscovery.preparation = next;
  try {
    setupDiscovery.preparation = normalize(await send("/api/discovery/preparation", next));
  } catch (error) {
    gate(error);
    await loadPreparation();
  }
  await refresh();
}

/** Moves a source up or down; the order picks the connection of a device more than one source found. */
export function moveSource(source, delta) {
  const priority = [...setupDiscovery.preparation.discovery_priority];
  const index = priority.indexOf(source);
  const next = index + delta;
  if (index < 0 || next < 0 || next >= priority.length) return null;
  priority.splice(index, 1);
  priority.splice(next, 0, source);
  return persistPreparation({ ...setupDiscovery.preparation, discovery_priority: priority });
}

export function toggleSource(source, enabled) {
  return persistPreparation({ ...setupDiscovery.preparation, sources: { ...setupDiscovery.preparation.sources, [source]: { enabled } } });
}

/** A refusal because the build is not confirmed: Guided Setup returns to its first step. */
function gate(error) {
  if (!GATE_ERRORS.has(error?.payload?.error)) return false;
  setupDiscovery.gateError = error;
  if (setup.step !== "release") {
    setup.build = { ...setup.build, error };
    goTo("release");
  }
  return true;
}

/**
 * The devices every enabled source found, unified by the server. With
 * ``refreshSources`` the server first refreshes each enabled source once.
 */
export async function refresh(refreshSources = false) {
  if (!setup.operation?.operationId) return;
  const session = setupDiscovery.session;
  try {
    const data = await send("/api/discovery/run", refreshSources ? { refresh: true } : {});
    if (session !== setupDiscovery.session) return;
    setupDiscovery.unified = data || null;
    setupDiscovery.gateError = null;
  } catch (error) {
    if (session === setupDiscovery.session) gate(error);
  }
  if (refreshSources && session === setupDiscovery.session) loadSourceStatus();
}

/** mDNS state and its observations, the ignored services, the MQTT brokers and proposals: read only. */
export async function loadSourceStatus() {
  await Promise.allSettled([pollMdns(), loadBrokers()]);
}

/** mDNS state and what it found, as Classic polls it from sign-in on. */
export async function pollMdns() {
  const [status, devices] = await Promise.allSettled([adminGet("/api/discovery/mdns/status"), adminGet("/api/discovery/devices")]);
  if (status.status === "fulfilled") setupDiscovery.mdns = status.value;
  else setupDiscovery.mdns = { ...(setupDiscovery.mdns || {}), state: "request_failed", controls_unavailable: String(setupDiscovery.mdns?.state || "").startsWith("unavailable_"), last_error: status.reason?.message || String(status.reason) };
  if (devices.status !== "fulfilled") return;
  const found = {};
  for (const device of Array.isArray(devices.value?.devices) ? devices.value.devices : []) found[observationKey(device)] = device;
  setupDiscovery.mdnsDevices = found;
  setupDiscovery.ignored = Array.isArray(devices.value?.ignored_devices) ? devices.value.ignored_devices : [];
}

/** Polls mDNS while the caller lives; answers the function that stops it. */
export function watchMdns() {
  pollMdns();
  const timer = setInterval(pollMdns, MDNS_POLL_MS);
  return () => clearInterval(timer);
}

/** The brokers found and the proposals made from them. */
export async function loadBrokers() {
  try {
    const data = await adminGet("/api/discovery/mqtt-brokers");
    setupDiscovery.brokers = Array.isArray(data?.candidates) ? data.candidates : [];
  } catch (error) {
    setupDiscovery.brokerMessage = { kind: "unavailable", error };
  }
  await loadProposals();
}

export async function loadProposals() {
  const generation = setupDiscovery.generation;
  try {
    const data = await adminGet("/api/discovery/mqtt-proposals");
    if (generation !== setupDiscovery.generation) return;
    setupDiscovery.proposals = Array.isArray(data?.proposals) ? data.proposals : [];
    setupDiscovery.proposalsError = null;
  } catch (error) {
    if (generation !== setupDiscovery.generation) return;
    setupDiscovery.proposals = [];
    setupDiscovery.proposalsError = error;
  }
}

/** Listens briefly, read only, to the reachable brokers for Zendure hardware topics. */
export async function refreshBrokers() {
  const session = setupDiscovery.session;
  try {
    const data = await send("/api/discovery/mqtt-brokers/refresh");
    if (session === setupDiscovery.session) setupDiscovery.brokerMessage = { kind: "refreshed", values: { reachable: Number(data?.reachable) || 0, devices: Number(data?.devices_found) || 0 } };
  } catch (error) {
    if (session === setupDiscovery.session && !gate(error)) setupDiscovery.brokerMessage = { kind: "failed", error };
  }
  await loadBrokers();
}

/** Checks the MQTT ports of networks a scan covers, one request per network. */
async function probeBrokers(cidrs) {
  const session = setupDiscovery.session;
  const results = await Promise.all(
    cidrs.map(async (cidr) => {
      try {
        return { found: Number((await send("/api/discovery/mqtt-brokers/probe", { cidr }))?.found) || 0, failed: false };
      } catch (error) {
        gate(error);
        return { found: 0, failed: true };
      }
    }),
  );
  await loadBrokers();
  if (session !== setupDiscovery.session) return;
  const failed = results.filter((result) => result.failed).length;
  setupDiscovery.brokerMessage = failed ? { kind: "probeFailed", values: { count: failed } } : { kind: "probed", values: { count: results.reduce((sum, result) => sum + result.found, 0) } };
}

/** An mDNS request the Admin console refused or did not answer: said in the mDNS block until the next poll. */
function mdnsFailed(error) {
  if (gate(error)) return;
  const previous = setupDiscovery.mdns || {};
  const state = String(previous.state || (previous.available ? "disabled" : "unavailable_dependency"));
  setupDiscovery.mdns = { ...previous, state: "request_failed", controls_unavailable: state.startsWith("unavailable_"), message: "", last_error: String(error?.payload?.message || error?.message || error) };
}

export async function setMdns(enabled) {
  try {
    await send(enabled ? "/api/discovery/mdns/enable" : "/api/discovery/mdns/disable");
  } catch (error) {
    mdnsFailed(error);
    return;
  }
  await pollMdns();
}

export async function refreshMdns() {
  try {
    await send("/api/discovery/mdns/refresh");
  } catch (error) {
    mdnsFailed(error);
    return;
  }
  await pollMdns();
}

/** The networks this host is on, and those reachable through a common router gateway. */
export async function detectNetworks() {
  setupDiscovery.detecting = true;
  try {
    try {
      const data = await adminGet("/api/discovery/networks");
      setupDiscovery.networks = Array.isArray(data?.networks) ? data.networks : [];
      setupDiscovery.networkWarnings = Array.isArray(data?.warnings) ? data.warnings : [];
    } catch (error) {
      setupDiscovery.networks = [];
      setupDiscovery.networkWarnings = [error?.message || String(error)];
    }
    setupDiscovery.gatewayState = "probing";
    try {
      const data = await send("/api/discovery/gateway-probe");
      setupDiscovery.gateways = (Array.isArray(data?.candidates) ? data.candidates : [])
        .filter((candidate) => candidate.status === "reachable")
        .map((candidate) => ({ cidr: candidate.network, source: "gateway", signals: candidate.signals || [], scan_recommended: Boolean(candidate.scan_supported), is_docker_like: false }));
      setupDiscovery.gatewayState = setupDiscovery.gateways.length ? "added" : "none";
    } catch {
      setupDiscovery.gateways = [];
      setupDiscovery.gatewayState = "unavailable";
    }
  } finally {
    setupDiscovery.detecting = false;
  }
}

/** Every network found, directly seen ones first; a gateway duplicate of a direct one is dropped. */
export function allNetworks() {
  const seen = new Set();
  return [...setupDiscovery.networks, ...setupDiscovery.gateways].filter((network) => {
    if (!network?.cidr || seen.has(network.cidr)) return false;
    seen.add(network.cidr);
    return true;
  });
}

function step(change) {
  setupDiscovery.progress = { ...setupDiscovery.progress, ...change(setupDiscovery.progress) };
}

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function scanOne(cidr, generation, onProgress) {
  const started = await send("/api/discovery/scan", { cidr });
  if (!started?.scan_id) throw new Error("scan request failed");
  const deadline = Date.now() + SCAN_LIMIT_MS;
  while (Date.now() < deadline) {
    if (generation !== setupDiscovery.generation) return [];
    const result = await adminGet(`/api/discovery/result/${encodeURIComponent(started.scan_id)}`);
    if (result?.progress) onProgress(result.progress);
    if (result?.status !== "running") return Array.isArray(result?.devices) ? result.devices : [];
    await pause(POLL_MS);
  }
  throw new Error("scan timed out");
}

/** Scans these networks, each once per run unless asked again; MQTT brokers on them are probed alongside. */
export async function scan(cidrs, source = "active_scan") {
  const generation = setupDiscovery.generation;
  const targets = [];
  for (const raw of cidrs) {
    const target = scanTarget(raw);
    if (target.error || setupDiscovery.scanned.has(target.cidr)) continue;
    setupDiscovery.scanned.add(target.cidr);
    targets.push(target.cidr);
  }
  if (!targets.length) return;
  probeBrokers(targets);
  step((progress) => ({ total: progress.total + targets.length }));
  setupDiscovery.scans = [...setupDiscovery.scans, ...targets.map((cidr) => ({ cidr, status: "queued", total_hosts: 0, checked_hosts: 0, error: null }))];
  const update = (cidr, change) => {
    setupDiscovery.scans = setupDiscovery.scans.map((item) => (item.cidr === cidr ? { ...item, ...change } : item));
  };
  await Promise.all(
    targets.map(async (cidr) => {
      update(cidr, { status: "running" });
      step((progress) => ({ active: progress.active + 1 }));
      try {
        const devices = await scanOne(cidr, generation, (progress) => update(cidr, { total_hosts: progress.total_hosts || 0, checked_hosts: progress.checked_hosts || 0 }));
        if (generation !== setupDiscovery.generation) return;
        let found = setupDiscovery.found;
        for (const device of devices) found = mergeObservation(found, device, source);
        setupDiscovery.found = found;
        update(cidr, { status: "done" });
        step((progress) => ({ active: Math.max(0, progress.active - 1), done: progress.done + 1 }));
      } catch (error) {
        if (generation !== setupDiscovery.generation) return;
        if (gate(error)) setupDiscovery.scanned.delete(cidr);
        update(cidr, { status: "failed", error: error?.message || String(error) });
        step((progress) => ({ active: Math.max(0, progress.active - 1), failed: progress.failed + 1 }));
      }
    }),
  );
}

function lanNetworks() {
  return allNetworks().filter((network) => !network.is_docker_like).map((network) => network.cidr);
}

/**
 * A full discovery run: the networks are found and scanned when a local source
 * is enabled, then the server refreshes every enabled source once and unifies
 * what they found. A run that was stopped only shows what it had.
 */
export async function runDiscovery() {
  if (setupDiscovery.running || setupDiscovery.scanning) return;
  setupDiscovery.running = true;
  const generation = setupDiscovery.generation;
  const session = setupDiscovery.session;
  try {
    const enabled = setupDiscovery.preparation.discovery_priority.filter(sourceEnabled);
    if (enabled.includes("local_api") || enabled.includes("local_mqtt")) {
      await detectNetworks();
      if (generation === setupDiscovery.generation) await scan(lanNetworks());
    }
    if (session !== setupDiscovery.session) return;
    if (generation !== setupDiscovery.generation) await refresh();
    else {
      await refresh(true);
      if (session === setupDiscovery.session) setupDiscovery.hasRun = true;
    }
  } finally {
    if (session === setupDiscovery.session) setupDiscovery.running = false;
  }
}

/** Finds the networks again and scans every LAN network anew. */
export function rescanNetworks() {
  return scanning(async (current) => {
    const generation = setupDiscovery.generation;
    setupDiscovery.scanned = new Set();
    await detectNetworks();
    if (!current()) return;
    if (generation === setupDiscovery.generation) await scan(lanNetworks());
    await refresh();
  });
}

/**
 * Stops the running scans; what they found so far stays. The run itself
 * stays busy until its last request settles, so a second one cannot overlap.
 */
export function cancel() {
  setupDiscovery.generation += 1;
  setupDiscovery.progress = { ...setupDiscovery.progress, active: 0 };
  setupDiscovery.scans = setupDiscovery.scans.map((scan) => (["queued", "running"].includes(scan.status) ? { ...scan, status: "cancelled" } : scan));
}

/** Runs one scan job while nothing else scans, and marks the step busy for it. */
async function scanning(task) {
  if (setupDiscovery.running || setupDiscovery.scanning) return;
  setupDiscovery.scanning = true;
  const session = setupDiscovery.session;
  try {
    await task(() => session === setupDiscovery.session);
  } finally {
    if (session === setupDiscovery.session) setupDiscovery.scanning = false;
  }
}

/** One typed address or range, or one container network on request. */
export async function scanManually(raw) {
  const target = scanTarget(raw);
  if (target.error) return target.error;
  await scanning(async () => {
    setupDiscovery.scanned.delete(target.cidr);
    await scan([target.cidr], "manual_scan");
    await refresh();
  });
  return "";
}

/** Forgets what the scans and mDNS found in this browser; the setup draft keeps what it holds. */
export function resetResults() {
  cancel();
  setupDiscovery.found = {};
  setupDiscovery.mdnsDevices = {};
  setupDiscovery.scans = [];
  setupDiscovery.progress = { total: 0, done: 0, failed: 0, active: 0 };
  setupDiscovery.scanned = new Set();
}

/** Everything back to a first visit, for a setup started over. */
export function clearDiscovery() {
  resetResults();
  setupDiscovery.session += 1;
  setupDiscovery.running = false;
  setupDiscovery.scanning = false;
  setupDiscovery.started = false;
  setupDiscovery.unified = null;
  setupDiscovery.hasRun = false;
  setupDiscovery.ignored = [];
  setupDiscovery.brokerMessage = null;
  setupDiscovery.gateError = null;
  loadBrokers();
}

/**
 * One source refreshed again: its networks and mDNS, its brokers, or the
 * Zendure cloud, whose answer the credentials panel shows.
 */
export function rescanSource(source) {
  return scanning(async (current) => {
    const generation = setupDiscovery.generation;
    const jobs = [];
    if (source === "local_api") {
      await detectNetworks();
      if (!current()) return;
      if (generation === setupDiscovery.generation) jobs.push(scan(lanNetworks()));
      jobs.push(refreshMdns());
    } else if (source === "local_mqtt") jobs.push(refreshBrokers());
    else if (source === "zendure_mqtt") jobs.push(refreshCloud().then(loadProposals));
    await Promise.allSettled(jobs);
    await refresh();
  });
}

export function progressPercent() {
  const progress = setupDiscovery.progress;
  if (!progress.total) return 0;
  let completed = progress.done + progress.failed;
  for (const scan of setupDiscovery.scans) if (scan.status === "running" && scan.total_hosts > 0) completed += Math.min(1, scan.checked_hosts / scan.total_hosts);
  return Math.min(100, Math.round((completed / progress.total) * 100));
}
