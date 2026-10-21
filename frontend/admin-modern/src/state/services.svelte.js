// SPDX-License-Identifier: AGPL-3.0-or-later
import { adminSend, sessionLost } from "./auth.svelte.js";
import { reload } from "./system.svelte.js";

const PATHS = {
  sync: "/api/admin/maintenance/containers/sync",
  repair: "/api/admin/maintenance/analytics/repair",
};

/**
 * A container restart or an analytics repair. Either can take minutes on the
 * server and runs there whatever page is open, so it is held here rather than
 * by the Services page: coming back finds it still running, or its result.
 */
class ServiceAction {
  busy = $state("");
  outcome = $state.raw(null);
}

export const serviceAction = new ServiceAction();

/** A run the ended session refused never started; once signed in again it is no result to show. */
export function forgetSessionRefusal() {
  if (!serviceAction.busy && sessionLost(serviceAction.outcome?.error)) serviceAction.outcome = null;
}

export async function runServiceAction(kind) {
  if (serviceAction.busy || !Object.hasOwn(PATHS, kind)) return;
  serviceAction.busy = kind;
  serviceAction.outcome = null;
  const body = kind === "sync" ? { confirm: true, reason: "manual" } : { confirm: true };
  try {
    serviceAction.outcome = { kind, result: await adminSend(PATHS[kind], { body }), error: null, at: Date.now() };
  } catch (error) {
    serviceAction.outcome = { kind, result: error?.payload || null, error, at: Date.now() };
  } finally {
    serviceAction.busy = "";
    reload("overview", "plan", "lifecycle");
  }
}
