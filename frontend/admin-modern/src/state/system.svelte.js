// SPDX-License-Identifier: AGPL-3.0-or-later
import { untrack } from "svelte";
import { adminGet, auth, sessionLost } from "./auth.svelte.js";

/** Each overview read runs several Docker inspections on the host, so once a minute is enough for a status. */
export const POLL_INTERVAL_MS = 60000;
export const FRESH_MS = 10000;

/**
 * One server read the pages share. The newest request wins, so a slow answer
 * never replaces a newer one, and a failed read keeps the last good answer
 * beside the error rather than blanking the page. A read refused because the
 * session ended is not this read's error: the sign-in is the answer to it,
 * and the read runs again once signed in.
 */
class Resource {
  data = $state.raw(null);
  error = $state.raw(null);
  loading = $state(false);
  loadedAt = $state(0);
  #path;
  #request = 0;

  constructor(path) {
    this.#path = path;
  }

  async load() {
    this.#request += 1;
    const request = this.#request;
    this.loading = true;
    try {
      const data = await adminGet(this.#path);
      if (request !== this.#request) return this.data;
      this.data = data;
      this.error = null;
      this.loadedAt = Date.now();
    } catch (error) {
      if (request === this.#request && !(sessionLost(error) && !auth.authenticated)) this.error = error;
    } finally {
      if (request === this.#request) this.loading = false;
    }
    return this.data;
  }

  /** Loads unless an answer younger than FRESH_MS is already here. */
  ensure() {
    if (this.loading || Date.now() - this.loadedAt < FRESH_MS) return Promise.resolve(this.data);
    return this.load();
  }
}

export const system = {
  overview: new Resource("/api/admin/maintenance/overview"),
  config: new Resource("/api/admin/maintenance/config"),
  plan: new Resource("/api/admin/maintenance/containers/plan"),
  backups: new Resource("/api/admin/maintenance/backups"),
  alignment: new Resource("/api/admin/system-alignment/status"),
  lifecycle: new Resource("/api/admin/workflow-lifecycle"),
  telemetry: new Resource("/api/admin/maintenance/zendure-mqtt/runtime-status"),
  migration: new Resource("/api/admin/maintenance/zendure-mqtt/migration-review"),
  releases: new Resource("/api/setup/releases?flow=upgrade"),
};

/** What every page needs to say whether the system is well, and whether a workflow holds the console. */
const POLLED = ["overview", "lifecycle"];

let poller = null;

export function startPolling(isActive) {
  if (poller) return;
  const tick = () => {
    if (document.hidden || !isActive()) return;
    POLLED.forEach((name) => system[name].load());
  };
  poller = setInterval(tick, POLL_INTERVAL_MS);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && isActive()) POLLED.forEach((name) => system[name].ensure());
  });
}

export function ensure(...names) {
  return Promise.all(names.map((name) => system[name].ensure()));
}

export function reload(...names) {
  return Promise.all(names.map((name) => system[name].load()));
}

/** Once signed in again, a read that failed only because the session had ended is not an error any more. */
export function forgetSessionErrors() {
  Object.values(system).forEach((resource) => {
    if (sessionLost(resource.error)) resource.error = null;
  });
}

/**
 * Reads that describe the same containers as the polled overview, kept as
 * fresh as it while a page shows them side by side: each new overview answer
 * reads them again, so a tile never says "running" next to a verdict that
 * says the EMS stopped. Call while a component initialises.
 */
export function keepWithOverview(...names) {
  $effect(() => {
    if (!system.overview.loadedAt) return;
    untrack(() => names.forEach((name) => system[name].ensure()));
  });
}
