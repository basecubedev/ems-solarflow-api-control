// SPDX-License-Identifier: AGPL-3.0-or-later
import { createLiveTransport } from "../lib/transport.js";
import { getJson } from "../lib/api.js";
import { prefs, applyDocumentPrefs } from "./prefs.svelte.js";

/**
 * The live snapshot. It is replaced wholesale on every frame and therefore
 * held raw: a deep proxy over a few kilobytes of JSON every two seconds buys
 * nothing, because nothing mutates it in place.
 */
class Live {
  snapshot = $state.raw(null);
  changedAt = $state(0);
  sequence = $state(0);
  transport = $state("connecting");
  now = $state(Date.now());
  runtime = $state.raw(null);
}

export const live = new Live();

export const RUNTIME_REFRESH_MS = 30000;

let transport = null;
let clock = null;
let runtimeTimer = null;

function accept(snapshot) {
  if (!snapshot || typeof snapshot !== "object") return;
  const previous = live.snapshot;
  live.snapshot = snapshot;
  if (!previous || previous.timestamp !== snapshot.timestamp) {
    live.changedAt = Date.now();
    live.sequence += 1;
  }
}

let runtimeRequest = 0;

/** The newest request wins, so a slow answer never replaces a newer one. */
export async function loadRuntime() {
  runtimeRequest += 1;
  const request = runtimeRequest;
  try {
    const runtime = await getJson("/api/runtime");
    if (request === runtimeRequest) live.runtime = runtime;
  } catch {
    return live.runtime;
  }
  return live.runtime;
}

async function loadUiConfig() {
  try {
    const config = await getJson("/api/ui-config");
    if (config && typeof config.animation_mode === "string") {
      prefs.serverAnimation = config.animation_mode;
      applyDocumentPrefs();
    }
  } catch {
    prefs.serverAnimation = "normal";
  }
}

export function startLive() {
  if (transport) return;
  transport = createLiveTransport({
    onSnapshot: accept,
    onState: (state) => {
      live.transport = state;
    },
    fetchLive: () => getJson("/api/live"),
    createEventSource: (url) => new EventSource(url),
  });
  transport.start();
  document.addEventListener("visibilitychange", () => transport.setVisible(!document.hidden));
  clock = setInterval(() => {
    if (!document.hidden) live.now = Date.now();
  }, 1000);
  loadRuntime();
  runtimeTimer = setInterval(() => {
    if (!document.hidden) loadRuntime();
  }, RUNTIME_REFRESH_MS);
  loadUiConfig();
}

export function stopLive() {
  transport?.stop();
  transport = null;
  if (clock) clearInterval(clock);
  if (runtimeTimer) clearInterval(runtimeTimer);
}
