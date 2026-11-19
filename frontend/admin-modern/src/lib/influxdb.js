// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The bundled InfluxDB version and its upgrade (admin/influx_upgrade.py) in
 * the reader's language. Every state and step is a code the server names;
 * its English sentences are only shown where it explains something this
 * build has no words for.
 */
import { STEP_ICONS, STEP_STATES } from "./backups.js";

export const UPGRADE_STEPS = Object.freeze(["preflight", "inventory", "pull", "backup", "update_compose", "recreate", "verify"]);
export const ROLLBACK_STEPS = Object.freeze(["preflight", "pull", "stop", "set_aside", "update_compose", "start", "restore", "verify"]);
export const PLAN_STATES = Object.freeze(["available", "low_memory", "current", "newer", "not_running", "no_way_back", "busy", "blocked", "mismatch", "unmanaged", "unavailable", "not_installed", "disabled", "external"]);
export const RECORD_STATES = Object.freeze(["running", "upgraded", "failed", "rolling_back", "rolled_back", "rollback_failed"]);
/** What a running step may note while it goes on (admin/influx_upgrade.py STILL_STARTING), in the reader's words. */
export const STEP_NOTES = Object.freeze({ still_starting: "influx.stillStarting", putting_back: "influx.puttingBack" });
/** Nothing installed, analytics off or an InfluxDB the operator runs: no bundled version to speak of. */
const NOT_OURS = Object.freeze(["not_installed", "disabled", "external"]);

const text = (value) => (typeof value === "string" ? value : "");

/** ``2.9.1`` from ``influxdb:2.9.1``; null for anything else. */
export function imageTag(image) {
  const value = text(image);
  return value.startsWith("influxdb:") && value.length > 9 ? value.slice(9) : null;
}

export function influxView(plan) {
  if (!plan || plan.ok === false) return null;
  const state = PLAN_STATES.includes(plan.state) ? plan.state : "unavailable";
  return {
    state,
    shown: !NOT_OURS.includes(state),
    current: text(plan.current_version) || imageTag(plan.current_image),
    target: text(plan.target_version) || imageTag(plan.target_image),
    targetImage: text(plan.target_image) || null,
    canUpgrade: state === "available" && Boolean(imageTag(plan.target_image)),
    offered: ["available", "low_memory"].includes(state) && Boolean(imageTag(plan.target_image)),
    memory: memoryView(plan.memory),
    message: text(plan.message),
    last: lastView(plan.last),
    rollback: rollbackView(plan.rollback),
    recheck: plan.recheck && typeof plan.recheck === "object" ? { available: plan.recheck.available === true, reason: text(plan.recheck.reason) } : null,
    slow: plan.slow === true,
    live: plan.live === true,
  };
}

/** What InfluxDB holds, what its upgrade needs free beside that and what is free, in MB; null where unread. */
function memoryView(memory) {
  if (!memory || typeof memory !== "object") return null;
  const mb = (value) => (Number.isFinite(value) && value >= 0 ? Math.round(value) : null);
  const view = { held: mb(memory.influx_mb), needed: mb(memory.needed_mb), available: mb(memory.available_mb) };
  return Object.values(view).every((value) => value !== null) ? view : null;
}

/** The words a plan state's sentence fills in: the version on offer and the memory figures. */
export function stateWords(view) {
  return {
    target: view?.target || "—",
    needed: view?.memory?.needed ?? "—",
    available: view?.memory?.available ?? "—",
    held: view?.memory?.held ?? "—",
  };
}

/** Records of a run that ended as it should; any other record is a run that did not finish or failed. */
const SETTLED_RECORDS = Object.freeze(["upgraded", "rolled_back"]);

/** Whether the bundled InfluxDB is up to date with nothing left of an earlier run to look at. */
export function influxSettled(view) {
  return Boolean(view) && ["current", "newer"].includes(view.state) && (!view.last || SETTLED_RECORDS.includes(view.last.state));
}

/** Whether the server offers to check an upgrade that did not finish again; it decides, from what it can verify. */
export function influxCanRecheck(view) {
  return Boolean(view?.recheck?.available);
}

/**
 * Whether an earlier run did not finish or failed and left something to look
 * at while no run goes on; a failed attempt that changed nothing is offered
 * again as the update it was.
 */
export function influxNeedsAttention(view) {
  return Boolean(view?.shown) && Boolean(view.last) && !SETTLED_RECORDS.includes(view.last.state) && !["busy", "available", "low_memory"].includes(view.state);
}

/** States the Updates page explains in the InfluxDB card although no run is under way. */
export const EXPLAINED_STATES = Object.freeze(["low_memory", "not_running", "no_way_back", "busy", "blocked", "mismatch", "unmanaged", "unavailable"]);

/**
 * Whether the InfluxDB card is shown beside the Updates list: while a run of
 * this browser goes on or has just ended, after any recorded run (its result
 * and its way back), when the plan has something to explain, or when it
 * cannot be read. An update that is merely available is offered in the list.
 */
export function influxCardShown(view, { running = false, finished = false, unreadable = false } = {}) {
  if (running || finished) return true;
  if (!view) return unreadable;
  if (!view.shown) return false;
  return Boolean(view.last) || Boolean(view.rollback?.available) || EXPLAINED_STATES.includes(view.state);
}

export function lastView(record) {
  if (!record || !RECORD_STATES.includes(record.state)) return null;
  return {
    state: record.state,
    from: imageTag(record.from_image),
    to: imageTag(record.to_image),
    backup: text(record.backup),
    at: (record.state === "rolling_back" && text(record.rollback_started_at)) || text(record.finished_at) || text(record.started_at),
    message: text(record.message),
    aside: text(record.set_aside),
    kept: Array.isArray(record.kept) ? record.kept.filter((name) => typeof name === "string" && name) : [],
  };
}

export function rollbackView(view) {
  if (!view || typeof view !== "object") return null;
  const to = imageTag(view.to_image);
  return {
    available: Boolean(view.available) && Boolean(to),
    to,
    toImage: text(view.to_image) || null,
    backup: text(view.backup),
    since: text(view.since),
    reason: text(view.reason),
  };
}

/** A step of an upgrade or of its way back, for StepList. */
export function stepView(kind, step) {
  const steps = kind === "rollback" ? ROLLBACK_STEPS : UPGRADE_STEPS;
  const key = String(step?.key || "");
  const state = Object.hasOwn(STEP_STATES, step?.state) ? step.state : "pending";
  const known = steps.includes(key);
  return {
    key,
    label: known ? `influx.step.${kind === "rollback" ? "rollback" : "upgrade"}.${key}` : null,
    text: known ? "" : String(step?.label || key),
    type: null,
    state,
    tone: STEP_STATES[state],
    icon: STEP_ICONS[state],
    detail: text(step?.message),
    detailKey: state === "running" && Object.hasOwn(STEP_NOTES, step?.note) ? STEP_NOTES[step.note] : null,
  };
}
