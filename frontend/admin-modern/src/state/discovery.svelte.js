// SPDX-License-Identifier: AGPL-3.0-or-later
import { adminGet, adminSend } from "./auth.svelte.js";
import { configuredNetworks, mergeObservation, normalizeSource, scanTarget } from "../lib/discovery.js";

const POLL_MS = 1200;
const SCAN_LIMIT_MS = 120000;
const CLOUD = "/api/discovery/zendure-cloud-mqtt";

/**
 * The network search, held here rather than by the page: a search runs for
 * a while, and coming back finds it still running or its results. The
 * results are kept until cleared; nothing here changes the draft.
 */
class Discovery {
  running = $state(false);
  scanning = $state(false);
  found = $state.raw({});
  proposals = $state.raw([]);
  progress = $state.raw({ total: 0, done: 0, failed: 0, active: 0 });
  scans = $state.raw([]);
  causes = $state.raw([]);
  ignored = $state.raw([]);
  outcome = $state("");
  cloudSkipped = $state(false);
  searched = $state(false);
  generation = 0;
  scanned = new Set();
}

export const discovery = new Discovery();

function step(change) {
  discovery.progress = { ...discovery.progress, ...change(discovery.progress) };
}

function finishWork(generation, failed, cause) {
  if (generation !== discovery.generation) return;
  step((progress) => ({ active: Math.max(0, progress.active - 1), [failed ? "failed" : "done"]: progress[failed ? "failed" : "done"] + 1 }));
  if (failed && cause) discovery.causes = [...discovery.causes, cause];
}

function updateScan(scan, change) {
  discovery.scans = discovery.scans.map((item) => (item === scan.ref ? (scan.ref = { ...item, ...change }) : item));
}

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function scanNetwork(cidr, generation, onProgress) {
  const started = await adminSend("/api/discovery/scan", { body: { cidr } });
  if (!started?.scan_id) throw new Error("scan request failed");
  const deadline = Date.now() + SCAN_LIMIT_MS;
  while (Date.now() < deadline) {
    if (generation !== discovery.generation) return [];
    const result = await adminGet(`/api/discovery/result/${encodeURIComponent(started.scan_id)}`);
    if (result?.progress) onProgress(result.progress);
    if (result?.status !== "running") return Array.isArray(result?.devices) ? result.devices : [];
    await pause(POLL_MS);
  }
  throw new Error("scan timed out");
}

/** Scans these networks once each per search; a network already scanned in this search is skipped. */
async function scanAll(cidrs, source) {
  const generation = discovery.generation;
  const queued = [];
  for (const raw of cidrs) {
    const target = scanTarget(raw);
    if (target.error || discovery.scanned.has(target.cidr)) continue;
    discovery.scanned.add(target.cidr);
    const scan = { ref: { cidr: target.cidr, status: "queued", error: null, total_hosts: 0, checked_hosts: 0 } };
    discovery.scans = [...discovery.scans, scan.ref];
    queued.push(scan);
  }
  if (!queued.length) return;
  step((progress) => ({ total: progress.total + queued.length }));
  await Promise.all(
    queued.map(async (scan) => {
      updateScan(scan, { status: "running" });
      step((progress) => ({ active: progress.active + 1 }));
      try {
        const devices = await scanNetwork(scan.ref.cidr, generation, (progress) => {
          if (generation === discovery.generation) updateScan(scan, { total_hosts: progress.total_hosts || 0, checked_hosts: progress.checked_hosts || 0 });
        });
        if (generation !== discovery.generation) return;
        let found = discovery.found;
        for (const device of devices) found = mergeObservation(found, device, normalizeSource(source));
        discovery.found = found;
        updateScan(scan, { status: "done" });
        finishWork(generation, false, null);
      } catch (error) {
        if (generation !== discovery.generation) return;
        updateScan(scan, { status: "failed", error: error?.message || String(error) });
        finishWork(generation, true, `${scan.ref.cidr}: ${error?.message || "scan failed"}`);
      }
    }),
  );
}

async function work(generation, label, task) {
  step((progress) => ({ active: progress.active + 1 }));
  try {
    await task();
    finishWork(generation, false, null);
  } catch (error) {
    finishWork(generation, true, `${label}: ${error?.message || String(error)}`);
  }
}

/**
 * Searches as Classic's Maintenance search does: the MQTT brokers, the
 * Zendure cloud when a key is saved, the MQTT proposals, mDNS, the networks
 * of the configured devices and the recommended LAN networks. A failed part
 * is named and never stops the others.
 */
export async function search(draft) {
  if (discovery.running) return;
  discovery.running = true;
  discovery.searched = true;
  discovery.outcome = "";
  discovery.cloudSkipped = false;
  discovery.ignored = [];
  discovery.proposals = [];
  const generation = discovery.generation;
  step((progress) => ({ total: progress.total + 5 }));
  try {
    const mqtt = (async () => {
      await work(generation, "MQTT brokers", () => adminSend("/api/discovery/mqtt-brokers/refresh", { body: {} }));
      await work(generation, "Zendure cloud", async () => {
        const settings = await adminGet(`${CLOUD}/settings`);
        if (settings?.token_saved) await adminSend(`${CLOUD}/refresh`, { body: {} });
        else discovery.cloudSkipped = true;
      });
      await work(generation, "MQTT proposals", async () => {
        const data = await adminGet("/api/discovery/mqtt-proposals");
        if (generation === discovery.generation) discovery.proposals = Array.isArray(data?.proposals) ? data.proposals : [];
      });
    })();
    const known = scanAll(configuredNetworks(draft), "active_scan");
    const mdns = work(generation, "mDNS", async () => {
      await adminSend("/api/discovery/mdns/refresh", { body: {} });
      const data = await adminGet("/api/discovery/devices");
      if (generation !== discovery.generation) return;
      let found = discovery.found;
      for (const device of Array.isArray(data?.devices) ? data.devices : []) found = mergeObservation(found, device, "mdns");
      discovery.found = found;
    });
    const networks = (async () => {
      let cidrs = [];
      await work(generation, "network discovery", async () => {
        const data = await adminGet("/api/discovery/networks");
        cidrs = (Array.isArray(data?.networks) ? data.networks : []).filter((network) => network.scan_recommended && !network.is_docker_like).map((network) => network.cidr).filter(Boolean);
      });
      if (generation === discovery.generation) await scanAll(cidrs, "active_scan");
    })();
    await Promise.all([mqtt, known, mdns, networks]);
    if (generation !== discovery.generation) return;
    discovery.outcome = discovery.progress.failed ? "warnings" : "done";
  } catch {
    if (generation === discovery.generation) discovery.outcome = "failed";
  } finally {
    if (generation === discovery.generation) discovery.running = false;
  }
}

/** Scans one typed address or range; earlier results are kept. */
export async function scanManually(raw) {
  const target = scanTarget(raw);
  if (target.error) return target.error;
  discovery.scanning = true;
  discovery.searched = true;
  const generation = discovery.generation;
  discovery.scanned.delete(target.cidr);
  try {
    await scanAll([target.cidr], "manual_scan");
    if (generation === discovery.generation) discovery.outcome = discovery.progress.failed ? "manualWarnings" : "manual";
  } finally {
    if (generation === discovery.generation) discovery.scanning = false;
  }
  return "";
}

/** Forgets the results and what was ignored; the draft stays as it is. */
export function clearResults() {
  discovery.generation += 1;
  discovery.running = false;
  discovery.scanning = false;
  discovery.found = {};
  discovery.proposals = [];
  discovery.progress = { total: 0, done: 0, failed: 0, active: 0 };
  discovery.scans = [];
  discovery.causes = [];
  discovery.ignored = [];
  discovery.cloudSkipped = false;
  discovery.searched = false;
  discovery.outcome = "reset";
  discovery.scanned = new Set();
}

export function ignore(key) {
  if (!discovery.ignored.includes(key)) discovery.ignored = [...discovery.ignored, key];
}

/** How far the search is, counting the hosts a running scan has checked. */
export function progressPercent(progress, scans) {
  if (!progress.total) return 0;
  let completed = progress.done + progress.failed;
  for (const scan of scans) if (scan.status === "running" && scan.total_hosts > 0) completed += Math.min(1, scan.checked_hosts / scan.total_hosts);
  return Math.min(100, Math.round((completed / progress.total) * 100));
}
