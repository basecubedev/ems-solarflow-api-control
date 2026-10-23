// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The Maintenance overview's ranked findings, in the reader's language.
 *
 * The server ranks them (admin/maintenance_health.py) and this renders that
 * order; a second sort here would be a second ranking rule. A finding whose
 * code is known gets its sentences from the string table, with the facts the
 * overview carries filled in; an unknown code keeps the server's words, so a
 * newer server is never silenced. tests/health.test.js walks the server's
 * codes and requires every one to be in FINDINGS.
 */

export const HEALTHY_INSTALL_STATES = Object.freeze(["standard_install", "admin_prepared_install"]);

/** install_state.py's verdicts; each has a label and a sentence in the string table. */
export const INSTALL_STATES = Object.freeze([
  "standard_install",
  "admin_prepared_install",
  "standard_config_only",
  "legacy_root_config",
  "compose_only",
  "partial_install",
  "none",
]);

/** Docker's container states, which the overview passes through as they come. */
export const CONTAINER_STATUSES = Object.freeze(["running", "created", "restarting", "removing", "paused", "exited", "dead", "missing", "unknown"]);

export const SEVERITY_TONES = Object.freeze({ error: "c-fault", warning: "c-warn", info: "c-acc" });
export const SEVERITY_ICONS = Object.freeze({ error: "circle-x", warning: "triangle-alert", info: "info" });

/**
 * What the reader can do about each finding. "page" opens a page of this
 * console, "refresh" reads the overview again.
 */
export const FINDINGS = Object.freeze({
  docker_unavailable: [{ kind: "refresh", label: "health.action.recheck" }],
  installation_incomplete: [
    { kind: "page", target: "setup", label: "health.action.setup" },
    { kind: "page", target: "backups", label: "health.action.restore" },
    { kind: "page", target: "system", label: "health.action.files" },
  ],
  ems_missing: [{ kind: "page", target: "updates", label: "health.action.updates" }],
  ems_stopped: [{ kind: "page", target: "services", label: "health.action.services" }],
  installation_warning: [{ kind: "page", target: "diagnostics", label: "health.action.diagnostics" }],
  settings_await_restart: [{ kind: "page", target: "services", label: "health.action.restart" }],
  config_unreadable: [{ kind: "page", target: "settings", label: "health.action.settings" }],
  ems_safe_mode: [{ kind: "page", target: "settings", label: "health.action.settings" }],
  ems_image_unverifiable: [{ kind: "page", target: "updates", label: "health.action.updates" }],
  overview_unreadable: [{ kind: "refresh", label: "health.action.recheck" }],
});

const mapping = (value) => (value && typeof value === "object" && !Array.isArray(value) ? value : {});

function missingPaths(overview) {
  const paths = mapping(overview?.paths);
  return ["config", "compose", "data"].filter((name) => !mapping(paths[name]).exists);
}

function containerStatus(overview) {
  const status = String(mapping(mapping(overview?.containers).ems).status || "stopped");
  return CONTAINER_STATUSES.includes(status) ? { key: `container.status.${status}` } : { text: status };
}

/** The facts a known finding's sentence names, read from the same overview the server read. */
function findingVars(code, overview) {
  if (code === "installation_incomplete") {
    const state = String(mapping(overview?.install_state).state || "");
    return { state: INSTALL_STATES.includes(state) ? state : "partial_install", missing: missingPaths(overview) };
  }
  if (code === "ems_stopped") return { status: containerStatus(overview) };
  if (code === "docker_unavailable") return { detail: mapping(overview?.docker).error || "" };
  if (code === "ems_safe_mode") {
    const fields = mapping(mapping(overview?.paths).config).placeholder_fields;
    return { fields: Array.isArray(fields) ? fields.map(String) : [] };
  }
  return {};
}

/**
 * One finding as the page shows it: a known code as keys and facts, an
 * unknown one with the server's title, message and next step as they are.
 */
export function findingView(finding, overview) {
  const severity = ["error", "warning", "info"].includes(finding?.severity) ? finding.severity : "warning";
  const code = String(finding?.code || "");
  const known = Object.hasOwn(FINDINGS, code);
  return {
    code,
    severity,
    tone: SEVERITY_TONES[severity],
    icon: SEVERITY_ICONS[severity],
    known,
    vars: known ? findingVars(code, overview) : {},
    title: String(finding?.title || ""),
    message: String(finding?.message || ""),
    next: String(finding?.next_step || ""),
    actions: known ? FINDINGS[code] : [],
  };
}

/**
 * The page's one-line answer. "loading" until the overview has answered at
 * all. An overview that cannot be read -- or could not be read again, while
 * an older answer is still on screen -- is "unreadable", never healthy:
 * ``failed`` says the last read failed. With no installation, the other
 * findings stay, and ``alert`` is the worst of them for the status pill.
 */
export function healthVerdict(overview, failed) {
  if (!overview) return { level: failed ? "unreadable" : "loading", findings: [], alert: null };
  const install = mapping(overview.install_state);
  const health = mapping(overview.health);
  const readable = Array.isArray(health.findings);
  const findings = readable ? health.findings.filter(Boolean).map((finding) => findingView(finding, overview)) : [];
  if (!readable || failed) return { level: "unreadable", findings, alert: null };
  if (install.state === "none") {
    const others = findings.filter((finding) => finding.code !== "installation_incomplete");
    return { level: "none", findings: others, alert: others.length ? others[0].severity : null };
  }
  if (!findings.length) return { level: "ok", findings, alert: null };
  return { level: findings[0].severity, findings, alert: null };
}

/** EMS as one fact: its state, its version and since when it runs. */
export function emsFact(overview) {
  const ems = mapping(mapping(overview?.containers).ems);
  const components = mapping(mapping(overview?.components).ems);
  return {
    running: Boolean(ems.running),
    found: Boolean(ems.found),
    status: containerStatus(overview),
    version: ems.tag || components.tag || null,
    startedAt: ems.running ? ems.started_at || null : null,
  };
}
