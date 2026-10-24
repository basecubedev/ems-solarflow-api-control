// SPDX-License-Identifier: AGPL-3.0-or-later
import { live } from "./live.svelte.js";
import { deriveSystemStatus, staleAfterMs } from "../lib/status.js";
import { normalizedDevices } from "../lib/devices.js";

export function loopInterval() {
  return Number(live.runtime?.system?.loop_interval) || 5;
}

export function systemStatus() {
  return deriveSystemStatus({
    snapshot: live.snapshot,
    changedAt: live.changedAt,
    now: live.now,
    staleAfter: staleAfterMs(loopInterval()),
  });
}

export function devices() {
  return normalizedDevices(live.snapshot);
}

export function isStale() {
  return systemStatus().level === "stale";
}
