// SPDX-License-Identifier: AGPL-3.0-or-later
import { tick } from "svelte";
import { getJson } from "../../../shared/lib/api.js";
import { applianceGet, applianceSend, loadSession, rememberNotice, session } from "./session.svelte.js";
import { host, refresh, stopPolling, watchOperations } from "./host.svelte.js";
import { forget } from "./forms.svelte.js";
import { MANAGER_OPERATIONS, POWER_OPERATIONS, findOperation, managerReconnectState, planRequestId } from "../lib/operations.js";
import { errorWords } from "../lib/errors.js";

/** The first look waits long enough for the host to have gone down; then every few seconds. */
export const RECONNECT_FIRST_MS = 8000;
export const RECONNECT_EVERY_MS = 5000;
/** Unpacking on a Pi 3B+ plus the installer's five minutes for a busy package manager, with room. */
export const MANAGER_RECONNECT_LIMIT_MS = 15 * 60 * 1000;

/**
 * Plan -> preview -> confirmation -> execution -> verification -> result.
 * Every change to this appliance goes through it: the agent makes a plan, the
 * owner reads it in a dialog and confirms it with its token, and the banner
 * follows the operation until the owner acknowledges it. Nothing here decides
 * whether a change is allowed; the agent refuses what it will not do.
 */
class Actions {
  planning = $state("");
  pending = $state.raw(null);
  confirming = $state(false);
  dialogError = $state.raw(null);
  settling = $state("");
  notice = $state.raw(null);
  reconnect = $state("");
}

export const actions = new Actions();

let lostPlan = null;
let opener = null;
let resuming = false;
let reconnectTimer = null;
let waiting = { before: "", outage: false, since: 0, operationId: "" };

function showPlan(payload, options) {
  actions.notice = null;
  actions.dialogError = null;
  actions.pending = { payload, options, shownAt: Date.now() };
}

/**
 * The control that asked for a plan is disabled while the plan is made, so the
 * native dialog would hand focus back to <body>. It goes back to that control,
 * or to the banner once the operation it planned has started.
 */
function restoreFocus(toBanner = false) {
  const target = opener;
  opener = null;
  if (typeof document === "undefined") return;
  tick().then(() => {
    const banner = toBanner ? document.querySelector("[data-testid=operation-banner]") : null;
    const element = banner || (target && target.isConnected ? target : null);
    if (element && typeof element.focus === "function") element.focus();
  });
}

/**
 * The dialog follows its operation. A plan cancelled in another tab, or ended
 * when the agent restarted, is no plan to confirm, so a listing read after the
 * dialog opened that holds it in any other state, or holds another operation
 * in the one slot, closes the dialog and leaves the banner to say the rest.
 */
function followPending(active, payload, startedAt) {
  const pending = actions.pending;
  if (!pending || actions.confirming || startedAt < pending.shownAt) return;
  const id = pending.payload?.operation?.operation_id;
  if (!id) return;
  const known = [active, ...((payload && payload.unacknowledged) || []), ...((payload && payload.recent) || [])];
  const record = known.find((item) => item && item.operation_id === id);
  if (record && record.state === "awaiting_confirmation") return;
  actions.pending = null;
  actions.dialogError = null;
  actions.notice = { key: "plan.gone", tone: "c-warn" };
  restoreFocus();
}

watchOperations(followPending);

/** A session that ended takes its plan and its notices with it; the next sign-in may be hours later. */
export function forgetPlan() {
  lostPlan = null;
  actions.pending = null;
  actions.dialogError = null;
  actions.notice = null;
}

/**
 * A plan whose answer never reached the page is handed over by the agent only
 * against the request id this page sent with it.
 */
function resumeLostPlan(active) {
  if (!lostPlan || resuming) return;
  if (!active) {
    lostPlan = null;
    if (actions.notice?.key === "plan.lost") actions.notice = { key: "plan.lostNone", tone: "c-warn" };
    return;
  }
  if (active.state !== "awaiting_confirmation") return;
  const lost = lostPlan;
  resuming = true;
  applianceSend("/api/operations/resume-plan", { body: { operation_id: active.operation_id, plan_request: lost.request } }).then(
    (payload) => {
      resuming = false;
      lostPlan = null;
      showPlan(payload, lost.options);
    },
    (error) => {
      resuming = false;
      if (error && error.status !== undefined) {
        lostPlan = null;
        if (actions.notice?.key === "plan.lost") actions.notice = { key: "plan.lostElsewhere", tone: "c-warn" };
      }
    },
  );
}

watchOperations(resumeLostPlan);

/**
 * Asks the agent for a plan. options: key (the control that asked), endpoint,
 * body, title (words), confirm (a label key), danger, forget (form fields to
 * clear once the plan is confirmed).
 */
export async function planOperation(options) {
  if (actions.planning || actions.pending) return;
  opener = typeof document === "undefined" ? null : document.activeElement;
  actions.planning = options.key || options.endpoint;
  actions.notice = null;
  const request = planRequestId();
  lostPlan = null;
  try {
    const payload = await applianceSend(options.endpoint, { body: { ...(options.body || {}), plan_request: request } });
    showPlan(payload, options);
  } catch (error) {
    if (!error || error.status === undefined) {
      lostPlan = { request, options };
      actions.notice = { key: "plan.lost", tone: "c-warn" };
    } else {
      if (error.code === "password_unconfirmed") session.confirmed = false;
      if (session.authenticated) actions.notice = { ...errorWords(error, "error.planFailed"), tone: "c-fault" };
    }
    if (session.authenticated) refresh();
  } finally {
    actions.planning = "";
  }
}

export function dismissNotice() {
  actions.notice = null;
}

export async function confirmPlan() {
  const pending = actions.pending;
  if (!pending || actions.confirming) return;
  actions.confirming = true;
  actions.dialogError = null;
  try {
    const payload = await applianceSend("/api/operations/confirm", {
      body: { operation_id: pending.payload.operation.operation_id, confirmation_token: pending.payload.confirmation_token },
    });
    host.operation = payload.operation || host.operation;
    if (pending.options.forget) forget(...pending.options.forget);
    actions.pending = null;
    restoreFocus(true);
    const type = (pending.payload.plan || {}).type || "";
    if (POWER_OPERATIONS.includes(type) || MANAGER_OPERATIONS.includes(type)) {
      startReconnect(type, pending.payload.operation.operation_id);
    }
  } catch (error) {
    if (!error || error.status === undefined) {
      const id = pending.payload.operation.operation_id;
      const record = await applianceGet(`/api/operations/${id}`).then(
        (answer) => (answer && answer.operation) || null,
        () => null,
      );
      if (record && record.state !== "awaiting_confirmation") {
        host.operation = record;
        if (pending.options.forget) forget(...pending.options.forget);
        actions.pending = null;
        restoreFocus(true);
        return;
      }
    }
    actions.dialogError = errorWords(error, "error.confirmFailed");
  } finally {
    actions.confirming = false;
  }
}

/** Closing the plan cancels it and clears it from the banner; the banner still offers its own Cancel if that fails. */
export function dismissPlan() {
  if (actions.confirming) return;
  const pending = actions.pending;
  actions.pending = null;
  actions.dialogError = null;
  restoreFocus();
  const operationId = pending && pending.payload && pending.payload.operation && pending.payload.operation.operation_id;
  if (!operationId) return;
  applianceSend("/api/operations/cancel", { body: { operation_id: operationId } })
    .then(() => applianceSend("/api/operations/acknowledge", { body: { operation_id: operationId } }))
    .catch(() => null)
    .then(() => {
      if (session.authenticated) refresh();
    });
}

async function settle(kind, path) {
  const operation = host.operation;
  if (!operation || actions.settling) return;
  actions.settling = kind;
  actions.notice = null;
  try {
    await applianceSend(path, { body: { operation_id: operation.operation_id } });
    if (kind === "acknowledge") host.operation = null;
    await refresh();
  } catch (error) {
    if (session.authenticated) actions.notice = { ...errorWords(error), tone: "c-fault" };
  } finally {
    actions.settling = "";
  }
}

export function acknowledgeOperation() {
  return settle("acknowledge", "/api/operations/acknowledge");
}

export function cancelOperation() {
  return settle("cancel", "/api/operations/cancel");
}

function scheduleReconnect(ms) {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(checkReconnect, ms);
}

/**
 * The Raspberry Pi restarts or shuts down, or the Manager package restarts its
 * own services: the page waits for it instead of reporting errors.
 */
export function startReconnect(type, operationId = "") {
  actions.reconnect = type;
  waiting = { before: session.version, outage: false, since: Date.now(), operationId };
  stopPolling();
  scheduleReconnect(RECONNECT_FIRST_MS);
}

export async function checkReconnect() {
  if (!actions.reconnect) return;
  clearTimeout(reconnectTimer);
  let answer;
  try {
    answer = await getJson("/api/session");
  } catch {
    waiting.outage = true;
    scheduleReconnect(RECONNECT_EVERY_MS);
    return;
  }
  if (MANAGER_OPERATIONS.includes(actions.reconnect)) {
    const signedIn = Boolean(answer && answer.authenticated);
    const manager = signedIn ? await getJson("/api/manager").catch(() => null) : null;
    const operations = signedIn ? await getJson("/api/operations").catch(() => null) : null;
    const operation = findOperation(operations, waiting.operationId);
    const state = managerReconnectState({ ...waiting, session: answer, manager, operation });
    if (state === "back") {
      // A reload, not a fresh read: the new package may ship a new console.
      rememberNotice("signIn.managerRestarted");
      globalThis.location.reload();
      return;
    }
    if (state === "waiting" && Date.now() - waiting.since < MANAGER_RECONNECT_LIMIT_MS) {
      scheduleReconnect(RECONNECT_EVERY_MS);
      return;
    }
  }
  await loadSession();
  actions.reconnect = "";
}
