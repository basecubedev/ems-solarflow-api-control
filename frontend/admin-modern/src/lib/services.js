// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The container plan (admin/container_actions.py) in the reader's language.
 * Every state and action is a code the plan names; the server's English
 * labels are only the fallback for a code this build does not know.
 */
import { errorView, noAnswer } from "./errors.js";

export const SERVICES = Object.freeze(["ems", "influxdb"]);
export const DISPLAY_STATES = Object.freeze(["running", "stopped", "missing", "not_configured", "disabled", "external"]);
export const DISPLAY_TONES = Object.freeze({ running: "c-ok", stopped: "c-warn", missing: "c-warn", not_configured: "c-idle", disabled: "c-idle", external: "c-acc" });
export const SYNC_ACTIONS = Object.freeze(["ems:recreate", "influxdb:start", "influxdb:stop", "influxdb:sync", "influxdb:init", "influxdb:repair"]);
export const CONFIG_STATES = Object.freeze(["pending", "current", "unknown"]);

const mapping = (value) => (value && typeof value === "object" && !Array.isArray(value) ? value : {});

export function serviceView(plan, service) {
  const display = mapping(mapping(plan?.services)[service]);
  const current = mapping(mapping(plan?.current)[service]);
  const state = String(display.display_state || "");
  const known = DISPLAY_STATES.includes(state);
  return {
    service,
    state: known ? state : "unknown",
    label: known ? null : display.display_label || null,
    tone: known ? DISPLAY_TONES[state] : "c-idle",
    name: current.name || null,
    startedAt: current.running ? current.started_at || null : null,
    configured: display.configured !== false,
  };
}

/** The changes a sync would make, in the order the server runs them. */
export function plannedChanges(plan) {
  const configState = CONFIG_STATES.includes(plan?.config_state?.state) ? plan.config_state.state : "unknown";
  return (Array.isArray(plan?.actions) ? plan.actions : [])
    .filter((action) => action && action.action && action.action !== "none")
    .map((action) => {
      const id = `${action.service}:${action.action}`;
      if (!SYNC_ACTIONS.includes(id)) return { text: String(action.label || id) };
      return id === "ems:recreate" ? { key: `services.change.ems:recreate.${configState}` } : { key: `services.change.${id}` };
    });
}

/**
 * Why the plan cannot run, as the server says it (admin/container_actions.py).
 * The one sentence about Docker has words in every language; any other --
 * an unreadable config.json names the file and its error -- is shown as the
 * server wrote it. tests/test_admin_modern.py holds the sentence to that source.
 */
export const PLAN_MESSAGES = Object.freeze({
  "Docker/Compose is not available. Re-check the Admin deployment.": "services.unavailable",
});

export function planMessage(message) {
  const text = typeof message === "string" ? message.trim() : "";
  if (!text) return null;
  return { key: Object.hasOwn(PLAN_MESSAGES, text) ? PLAN_MESSAGES[text] : null, text };
}

export function canSync(plan) {
  return Boolean(plan?.available) && plannedChanges(plan).length > 0;
}

/** Whether a run that the server finished left something undone: a step failed or it warned. */
export function partlyDone(result) {
  const steps = Array.isArray(result?.steps) ? result.steps : [];
  const warnings = Array.isArray(result?.warnings) ? result.warnings : [];
  return result?.status === "completed_with_warnings" || warnings.length > 0 || steps.some((step) => step?.status !== "ok");
}

/** One finished step of a sync or a repair. */
export function stepView(step) {
  const id = `${step?.service}:${step?.action}`;
  const ok = step?.status === "ok";
  return {
    key: SYNC_ACTIONS.includes(id) ? `services.step.${id}` : null,
    text: SYNC_ACTIONS.includes(id) ? "" : id,
    ok,
    detail: ok ? "" : String(step?.detail || ""),
  };
}

export const SERVICE_REFUSALS = Object.freeze(["busy", "unavailable", "analytics_disabled", "influx_not_running"]);
export const RESULT_ICONS = Object.freeze({ ok: "circle-check", partial: "triangle-alert", unknown: "circle-help", fault: "circle-x" });

/** A finished restart or repair as the reader sees it, said with ``t``: its tone, message, steps and warnings. */
export function serviceOutcome(raw, t) {
  if (!raw) return null;
  const { kind, result, error } = raw;
  const steps = (Array.isArray(result?.steps) ? result.steps : []).map(stepView);
  const warnings = Array.isArray(result?.warnings) ? result.warnings.map(String) : [];
  const base = { steps, warnings, at: raw.at };
  if (!error && partlyDone(result)) return { ...base, tone: "partial", message: t("services.partlyDone") };
  if (!error) return { ...base, tone: "ok", message: kind === "sync" ? t("services.synced") : t("services.repaired") };
  if (noAnswer(error)) return { ...base, tone: "unknown", message: t("error.noAnswer") };
  if (result?.status === "unavailable") {
    const why = planMessage(result.message);
    if (why && !why.key) return { ...base, tone: "fault", message: why.text, lang: "en" };
  }
  if (SERVICE_REFUSALS.includes(result?.status)) return { ...base, tone: "fault", message: t(`services.status.${result.status}`) };
  const view = errorView(error, t, kind === "sync" ? t("services.syncFailed") : t("services.repairFailed"));
  return { ...base, tone: "fault", message: view.text, lang: view.lang };
}
