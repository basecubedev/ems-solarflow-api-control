// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The Admin's EMS CLI checks (admin/ems_cli.py): read-only emsctl commands
 * run against the installed EMS. The check ids and statuses are the server's;
 * tests/diagnostics.test.js walks its CHECKS and _SUMMARY_BUCKET.
 */

export const CHECK_IDS = Object.freeze(["quick_diagnose", "config_upgrade_dry_run", "influx_status", "runtime_status"]);
export const CHECK_STATUSES = Object.freeze({
  ok: "c-ok",
  disabled: "c-idle",
  warning: "c-warn",
  failed: "c-fault",
  timeout: "c-fault",
  unavailable: "c-idle",
  not_run: "c-idle",
});
export const RUN_MODES = Object.freeze(["container", "local", "unavailable"]);

/**
 * Why no check ran, as the server says it. The sentence for "no EMS here yet"
 * (admin/ems_cli.py UNAVAILABLE_MESSAGE) has words in every language; any
 * other -- the check runner itself failed -- is shown as the server wrote it.
 * tests/test_admin_modern.py holds the sentence to that source.
 */
export const UNAVAILABLE_MESSAGES = Object.freeze({
  "EMS CLI diagnostics are unavailable because no running EMS container or local emsctl.py was found.": "diagnostics.unavailable",
});

export function unavailableView(message) {
  const text = typeof message === "string" ? message.trim() : "";
  if (!text || Object.hasOwn(UNAVAILABLE_MESSAGES, text)) return { key: UNAVAILABLE_MESSAGES[text] || "diagnostics.unavailable", text: "" };
  return { key: null, text };
}

function firstLine(text) {
  return (
    String(text || "")
      .split("\n")
      .map((line) => line.trim())
      .find(Boolean) || ""
  );
}

/** One check: its name and state as keys, and the one line that says what happened. */
export function checkView(check) {
  const id = String(check?.id || "");
  const status = Object.hasOwn(CHECK_STATUSES, check?.status) ? check.status : "not_run";
  let message = typeof check?.message === "string" ? check.message : "";
  let messageKey = "";
  if (!message && status === "timeout") messageKey = "diagnostics.timedOut";
  else if (!message && status !== "ok") {
    message = firstLine(check?.stderr) || (status === "unavailable" ? "" : firstLine(check?.stdout));
    if (!message && status === "unavailable") messageKey = "diagnostics.couldNotRun";
    else if (!message && typeof check?.exit_code === "number") messageKey = "diagnostics.exitCode";
  }
  const raw = [check?.stdout ? String(check.stdout) : "", check?.stderr ? `stderr:\n${check.stderr}` : ""].filter(Boolean).join("\n\n");
  return {
    id,
    nameKey: CHECK_IDS.includes(id) ? `diagnostics.check.${id}` : null,
    name: String(check?.label || id),
    status,
    tone: CHECK_STATUSES[status],
    durationMs: typeof check?.duration_ms === "number" ? check.duration_ms : null,
    message,
    messageKey,
    exitCode: check?.exit_code,
    raw,
    truncated: Boolean(check?.truncated),
    diagnosis: id === "quick_diagnose" && check?.json && typeof check.json === "object" ? check.json.diagnosis || null : null,
  };
}

export function summaryTone(summary) {
  if (!summary) return "c-idle";
  if (summary.status === "ok") return "c-ok";
  if (summary.status === "failed") return "c-fault";
  if (summary.status === "warning") return "c-warn";
  return "c-idle";
}
