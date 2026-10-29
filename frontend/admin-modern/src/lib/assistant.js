// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The assistant: what is still to do on this installation, and the ways to
 * what an owner comes to the console for. It reads the answers the overview
 * reads and asks the server nothing of its own. Pure, so the node tests can
 * walk it; tests/assistant.test.js holds each step to the facts it names.
 */
import { HEALTHY_INSTALL_STATES, emsFact } from "./health.js";

export { ASSISTANT_STATES, STATE_LOOKS, WELCOME_CHOICES, WELCOME_KEY, assistantMark, bareAddress, stepProgress } from "../../../shared/assistant/assistant.js";

/** The steps in the order an installation takes them; the dashboard is where they lead. */
export const STEP_IDS = Object.freeze(["install", "devices", "backup"]);

/** The step whose being left marks the menu amber: EMS is not installed. */
export const URGENT_STEPS = Object.freeze(["install"]);

export const STEP_LOOKS = Object.freeze({
  install: { icon: "package-plus", tone: "c-out" },
  devices: { icon: "zap", tone: "c-inv" },
  backup: { icon: "archive", tone: "c-soc" },
  dashboard: { icon: "layout-dashboard", tone: "c-pv" },
});

/**
 * The ways to what an owner comes for, each a page of this console. They need
 * an installation; "reinstall" sets one up again over the running system and
 * says so.
 */
export const TASKS = Object.freeze([
  { id: "update", icon: "circle-arrow-up", tone: "c-out", page: "updates" },
  { id: "settings", icon: "sliders-horizontal", tone: "c-pv", page: "settings" },
  { id: "device", icon: "zap", tone: "c-inv", page: "settings", tab: "devices" },
  { id: "backup", icon: "archive", tone: "c-soc", page: "backups" },
  { id: "check", icon: "stethoscope", tone: "c-bat", page: "diagnostics" },
  { id: "reinstall", icon: "rotate-cw", tone: "c-warn", page: "setup" },
]);


const mapping = (value) => (value && typeof value === "object" && !Array.isArray(value) ? value : {});

/**
 * Whether EMS is installed: true, false, or null while the overview has not
 * answered. "none" and the incomplete states are both not installed; the
 * difference is what ``installKind`` says.
 */
export function installed(overview) {
  const state = mapping(mapping(overview).install_state).state;
  if (!overview || typeof state !== "string") return null;
  return HEALTHY_INSTALL_STATES.includes(state);
}

/**
 * What the installation is, for the welcome and the first step: "none",
 * "legacy" (settings in the old root config.json, which the System page
 * moves), "partial", "missing" (the settings are in place, the EMS container
 * is not), "installed", or "unknown" while the overview has not answered.
 */
export function installKind(overview) {
  const done = installed(overview);
  if (done === null) return "unknown";
  if (done) return containerFound(overview) === false ? "missing" : "installed";
  const install = overview.install_state;
  if (install.state === "none") return "none";
  return install.legacy_migration_available === true ? "legacy" : "partial";
}

/** For an incomplete installation: whether the server recommends managing what exists over a new setup. */
export function managesExisting(overview) {
  return mapping(mapping(overview).install_state).recommended_path === "manage_existing";
}

/** Whether the EMS container exists, or null while Docker cannot be asked and the files' verdict stands. */
function containerFound(overview) {
  if (!mapping(mapping(overview).docker).available) return null;
  return emsFact(overview).found;
}

/**
 * The devices as the settings have them: how many, and the fields EMS still
 * finds the template's example values in (devices, the grid meter, the
 * broker -- all on the devices tab), as summary.control names them.
 */
function deviceFacts(config) {
  if (mapping(config).status !== "ok") return { count: null, placeholders: [] };
  const summary = mapping(config.summary);
  const count = Number(summary.device_count);
  const fields = mapping(summary.control).placeholder_fields;
  return {
    count: Number.isFinite(count) ? count : null,
    placeholders: Array.isArray(fields) ? fields.filter((field) => typeof field === "string" && field) : [],
  };
}

/**
 * The newest backup that can be read: an archive the server marks invalid
 * (cut short, damaged) is not a backup to rely on before an update.
 */
export function latestUsableBackup(listing) {
  const records = Array.isArray(listing?.backups) ? listing.backups : [];
  const stamps = records.filter((record) => record && record.valid !== false && typeof record.created_at === "string" && record.created_at).map((record) => record.created_at);
  return stamps.length ? stamps.reduce((newest, stamp) => (stamp > newest ? stamp : newest)) : null;
}

/**
 * The checklist. Each step: its state, and the fact that decides it. Devices
 * and the backup wait for the installation's files; the installation itself
 * is done only once Docker also has the EMS container. Devices still carrying
 * the template's example values are not set up (``placeholders``). A step
 * whose answer has not come is "unknown", never done. ``next`` marks the
 * first step still to do.
 */
export function assistantSteps({ overview, config, backups }) {
  const done = installed(overview);
  const kind = installKind(overview);
  const devices = deviceFacts(config);
  const lastBackup = latestUsableBackup(backups);
  const after = (state) => (done === null ? "unknown" : done ? state : "later");
  const deviceState = devices.count === null ? "unknown" : devices.placeholders.length || devices.count === 0 ? "todo" : "done";
  const steps = [
    { id: "install", state: done === null ? "unknown" : kind === "installed" ? "done" : "todo", kind },
    { id: "devices", state: after(deviceState), count: devices.count, placeholders: devices.placeholders },
    { id: "backup", state: after(!backups ? "unknown" : lastBackup ? "done" : "todo"), at: lastBackup },
  ];
  const next = steps.find((step) => step.state === "todo");
  return steps.map((step) => ({ ...step, ...STEP_LOOKS[step.id], next: step === next }));
}

/**
 * Where the steps lead: the dashboard, once EMS runs, the Admin knows its
 * address and the settings leave it on. ``reason`` says why not: "off" in the
 * settings; "stopped" until EMS runs; "noAddress" when the settings cannot
 * give its address; "docker" when Docker cannot be asked whether EMS runs,
 * so the address is offered as it is.
 */
export function dashboardStep(overview, dashboard, config) {
  const step = (state, reason) => ({ id: "dashboard", state, reason, ...STEP_LOOKS.dashboard });
  if (installed(overview) === null) return step("unknown", null);
  if (mapping(config).status === "ok" && mapping(config.summary).dashboard_enabled === false) return step("off", "off");
  const running = mapping(mapping(overview).docker).available ? emsFact(overview).running : null;
  if (running === false || (!installed(overview) && running !== true)) return step("later", "stopped");
  if (!dashboard) return step("unknown", "noAddress");
  return running ? step("ready", null) : step("unknown", "docker");
}


/**
 * How many findings the overview's headline names when it asks for
 * attention: all of them once the worst is a warning or an error, as
 * Overview.svelte counts them; nothing when there are only notes.
 */
export function attentionCount(verdict) {
  const level = verdict?.level === "none" ? verdict.alert : verdict?.level;
  return (level === "warning" || level === "error") && Array.isArray(verdict.findings) ? verdict.findings.length : 0;
}

/**
 * The tasks the page offers once the settings are in place, none before;
 * setting up again over the running system only while an EMS container
 * exists to set up over.
 */
export function assistantTasks(overview) {
  if (!installed(overview)) return [];
  return installKind(overview) === "missing" ? TASKS.filter((task) => task.id !== "reinstall") : TASKS;
}


/**
 * Whether the console opens on the assistant: always while nothing is
 * installed, otherwise while the reader leaves the choice on.
 */
export function opensOnAssistant(overview, choice) {
  return installKind(overview) === "none" || choice !== "off";
}
