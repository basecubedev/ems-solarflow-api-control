// SPDX-License-Identifier: AGPL-3.0-or-later
import { untrack } from "svelte";
import { createLiveTransport } from "../lib/transport.js";
import { getJson } from "../../../shared/lib/api.js";
import { prefs, applyDocumentPrefs } from "./prefs.svelte.js";
import { auth } from "./auth.svelte.js";

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
  runtimeUnavailable = $state(false);
}

export const live = new Live();

export const RUNTIME_REFRESH_MS = 30000;

let transport = null;
let clock = null;
let runtimeTimer = null;
let stopAuthWatch = null;

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
let runtimePending = false;
let runtimeRetry = null;
export const RUNTIME_RETRY_MS = 5000;

/**
 * The newest request wins, so a slow answer never replaces a newer one. A
 * failed read is said beside the write mode and tried again soon while signed
 * in, as Classic does: right after a sign-in into an EMS that is still
 * restarting, the 30 s refresh would leave the settings locked for half a minute.
 */
export async function loadRuntime() {
  runtimeRequest += 1;
  const request = runtimeRequest;
  runtimePending = true;
  try {
    const runtime = await getJson("/api/runtime");
    if (request !== runtimeRequest) return live.runtime;
    live.runtime = runtime;
    live.runtimeUnavailable = false;
    clearTimeout(runtimeRetry);
    runtimeRetry = null;
  } catch {
    if (request !== runtimeRequest) return live.runtime;
    live.runtimeUnavailable = true;
    if (auth.authenticated && !runtimeRetry) {
      runtimeRetry = setTimeout(() => {
        runtimeRetry = null;
        loadRuntime();
      }, RUNTIME_RETRY_MS);
    }
  } finally {
    if (request === runtimeRequest) runtimePending = false;
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
  stopAuthWatch = retryRuntimeOnSignIn();
  loadUiConfig();
}

/**
 * A runtime read that failed while signed out is asked again the moment a
 * session starts, as Classic reloads it after every sign-in, so the pill's
 * "retrying" is true from the first second. It waits for the page's own
 * effects of that sign-in, so a page that reads the runtime itself is not
 * asked twice.
 */
function retryRuntimeOnSignIn() {
  return $effect.root(() => {
    $effect(() => {
      if (!auth.authenticated) return;
      untrack(() => {
        queueMicrotask(() => {
          if (auth.authenticated && live.runtimeUnavailable && !runtimeRetry && !runtimePending) loadRuntime();
        });
      });
    });
  });
}

export function stopLive() {
  transport?.stop();
  transport = null;
  if (clock) clearInterval(clock);
  if (runtimeTimer) clearInterval(runtimeTimer);
  stopAuthWatch?.();
  stopAuthWatch = null;
}
