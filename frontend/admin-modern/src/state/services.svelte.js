// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import { adminSend, sessionLost } from "./auth.svelte.js";
import { system, ensure, keepWithOverview, reloadAfter } from "./system.svelte.js";
import { t } from "../i18n/i18n.svelte.js";
import { canSync, restartAfter } from "../lib/services.js";

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
  const startedAt = Date.now();
  const body = kind === "sync" ? { confirm: true, reason: "manual" } : { confirm: true };
  try {
    serviceAction.outcome = { kind, result: await adminSend(PATHS[kind], { body }), error: null, at: Date.now(), startedAt };
  } catch (error) {
    serviceAction.outcome = { kind, result: error?.payload || null, error, at: Date.now(), startedAt };
  } finally {
    serviceAction.busy = "";
    reloadAfter(serviceAction.outcome?.at ?? 0, "overview", "plan", "lifecycle");
  }
}

/**
 * The EMS restart a saved change waits for, as the component that offers it
 * sees it; call it while that component starts. when() is ``{ since, shown }``
 * on this browser's clock, and the outcome is the restart that counts as its
 * own (restartAfter). It can start only when the container plan can run it, as
 * on Services; widgets/PlanRefusal says why not.
 */
export function restartOffer(when) {
  ensure("plan");
  keepWithOverview("plan");
  const outcome = $derived(restartAfter(serviceAction.outcome, when(), system.plan.data, system.plan.askedAt, t));
  return {
    get busy() {
      return serviceAction.busy === "sync";
    },
    get outcome() {
      return outcome;
    },
    get done() {
      return outcome?.tone === "ok";
    },
    get ready() {
      return canSync(system.plan.data) && !serviceAction.busy;
    },
    start() {
      runServiceAction("sync");
    },
  };
}
