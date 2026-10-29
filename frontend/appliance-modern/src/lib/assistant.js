// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The Appliance Manager's assistant: what is still to do on this Raspberry
 * Pi before and after the EMS runs on it, and the ways to what an owner comes
 * for. It reads the host status and the backup listing the pages read and
 * asks the agent nothing of its own. Pure, so the node tests can walk it;
 * tests/assistant.test.js holds each step to the facts it names.
 */
import { adminGate, daemonState, dockerStopped, emsContainer, updatesInDoubt } from "./host.js";

export { ASSISTANT_STATES, STATE_LOOKS, WELCOME_CHOICES, WELCOME_KEY, assistantMark, bareAddress, stepProgress } from "../../../shared/assistant/assistant.js";

/** The steps in the order a fresh appliance takes them; backup access is optional and never comes first. */
export const STEP_IDS = Object.freeze(["network", "admin", "ems", "updates", "backup"]);
export const OPTIONAL_STEPS = Object.freeze(["backup"]);

/** The steps whose being left marks the menu amber: EMS Admin or the EMS is not there or not running. */
export const URGENT_STEPS = Object.freeze(["admin", "ems"]);

export const STEP_LOOKS = Object.freeze({
  network: { icon: "wifi", tone: "c-out" },
  admin: { icon: "server", tone: "c-inv" },
  ems: { icon: "zap", tone: "c-out" },
  updates: { icon: "shield-check", tone: "c-soc" },
  backup: { icon: "key-round", tone: "c-soc" },
});

/**
 * The ways to what an owner comes for, each a page of this console, and the
 * EMS Admin console itself once it runs.
 */
export const TASKS = Object.freeze([
  { id: "update", icon: "circle-arrow-up", tone: "c-out", page: "updates" },
  { id: "admin", icon: "server", tone: "c-inv", page: "admin" },
  { id: "network", icon: "wifi", tone: "c-out", page: "network" },
  { id: "access", icon: "key-round", tone: "c-soc", page: "access" },
  { id: "diagnostics", icon: "stethoscope", tone: "c-bat", page: "diagnostics" },
  { id: "settings", icon: "sliders-horizontal", tone: "c-pv", page: "settings" },
  { id: "openAdmin", icon: "external-link", tone: "c-inv", external: true },
]);

/** The container states and health the agent sends (appliance/docker_backend.py CONTAINER_*, HEALTH_*). */
export const CONTAINER_MISSING = "missing";
export const CONTAINER_RUNNING = "running";
export const HEALTH_STARTING = "starting";

/** What the welcome can say the appliance is, each with its lead; "unknown" has none of its own. */
export const APPLIANCE_KINDS = Object.freeze(["docker", "bootstrap", "admin", "stopped", "setup", "running"]);

const mapping = (value) => (value && typeof value === "object" && !Array.isArray(value) ? value : {});

/** A status that answered; a read in flight (null), not asked (undefined) or failed ({ error }) is none. */
export function readStatus(status) {
  const value = mapping(status);
  return status && !value.error ? value : null;
}

/** A Docker section the agent could not read, or one without the EMS container's name, cannot say there is no EMS. */
function emsUnread(status) {
  const docker = mapping(status.docker);
  return !daemonState(status) || !docker.ems_container;
}

/**
 * What the appliance is, for the welcome: "docker" (Docker does not run),
 * "bootstrap" (nothing installed yet), "running" (the EMS runs), "admin"
 * (EMS Admin is there but not healthy), "stopped" (the EMS container is there
 * but does not run), "setup" (EMS Admin is healthy, the EMS not set up), or
 * "unknown" while EMS Admin or the EMS cannot be told.
 */
export function applianceKind(status) {
  const read = readStatus(status);
  if (!read) return "unknown";
  if (dockerStopped(read)) return "docker";
  const admin = adminStep(read);
  if (admin.kind === "bootstrap") return "bootstrap";
  const ems = emsContainer(read);
  if (ems?.state === CONTAINER_RUNNING) return "running";
  if (admin.kind === "unread") return "unknown";
  if (admin.state !== "done") return "admin";
  if (emsUnread(read)) return "unknown";
  return ems && ems.state !== CONTAINER_MISSING ? "stopped" : "setup";
}

function networkStep(status) {
  const connectivity = mapping(mapping(status).network).connectivity;
  if (!status || !connectivity || connectivity === "unknown") return { state: "unknown" };
  return { state: connectivity === "full" ? "done" : "todo", connectivity, connection: mapping(status.network).active_connection || "" };
}

/** EMS Admin that is installed and not healthy: stopped, still starting, or failing its health check. */
function adminTrouble(admin) {
  if (mapping(admin.container).state !== CONTAINER_RUNNING) return { state: "todo", kind: "stopped" };
  if (admin.health === HEALTH_STARTING) return { state: "unknown", kind: "starting" };
  return { state: "todo", kind: "unhealthy" };
}

/** Docker first: without it the host cannot say what EMS Admin is, and nothing could run it anyway. */
function adminStep(status) {
  if (status && dockerStopped(status)) return { state: "todo", kind: "docker" };
  const gate = status ? adminGate(status) : "unread";
  if (gate === "unread") return { state: "unknown", kind: "unread" };
  if (gate === "live") return { state: "unknown", kind: "live" };
  if (gate !== "ready") return { state: "todo", kind: gate };
  const admin = mapping(status.admin);
  return admin.healthy ? { state: "done", kind: "healthy" } : adminTrouble(admin);
}

function emsStep(status, admin) {
  if (!status) return { state: "unknown" };
  const ems = emsContainer(status);
  if (ems?.state === CONTAINER_RUNNING) return { state: "done" };
  if (admin.kind === "unread" || emsUnread(status)) return { state: "unknown" };
  if (admin.state !== "done") return { state: "later" };
  return { state: "todo", kind: ems && ems.state !== CONTAINER_MISSING ? "stopped" : "setup" };
}

function updatesStep(status) {
  const updates = mapping(mapping(status).updates);
  if (!status || updates.security_count === undefined) return { state: "unknown" };
  if (updatesInDoubt(updates)) return { state: "todo", kind: "doubt" };
  const count = Number(updates.security_count) || 0;
  return count > 0 ? { state: "todo", kind: "security", count } : { state: "done" };
}

function backupStep(backup) {
  const listing = mapping(backup);
  if (!backup || listing.error) return { state: "unknown" };
  const keys = Number(mapping(listing.account).key_count) || 0;
  return { state: keys > 0 ? "done" : "todo", keys };
}

/**
 * The checklist. Each step: its state, and the fact that decides it. The EMS
 * waits for a healthy EMS Admin; a step whose answer has not come is
 * "unknown", never done. ``next`` marks the first step still to do, an
 * optional one only once nothing else is.
 */
export function assistantSteps({ status, backup }) {
  const read = readStatus(status);
  const admin = adminStep(read);
  const facts = {
    network: networkStep(read),
    admin,
    ems: emsStep(read, admin),
    updates: updatesStep(read),
    backup: backupStep(backup),
  };
  const steps = STEP_IDS.map((id) => ({ id, ...facts[id], optional: OPTIONAL_STEPS.includes(id), ...STEP_LOOKS[id] }));
  const next = steps.find((step) => step.state === "todo" && !step.optional) || steps.find((step) => step.state === "todo");
  return steps.map((step) => ({ ...step, next: step === next }));
}

/** The tasks the page offers; the way to EMS Admin only while it has an address. */
export function assistantTasks(adminHref) {
  return TASKS.filter((task) => !task.external || Boolean(adminHref));
}

/**
 * The steps the menu's mark weighs. A network that reaches the LAN but not
 * the internet stays a step on the page rather than a mark on every page: an
 * appliance on a LAN without internet could never clear it.
 */
export function markedSteps(steps) {
  return steps.filter((step) => !(step.id === "network" && step.connectivity === "limited"));
}

/** Whether the status says if anything is installed yet; the welcome choice "off" waits for that answer. */
export function installationRead(status) {
  return mapping(mapping(readStatus(status)).admin).bootstrap_required !== undefined;
}

/**
 * Whether the console opens on the assistant: always while nothing is
 * installed, Docker running or not, otherwise while the reader leaves the
 * choice on.
 */
export function opensOnAssistant(status, choice) {
  return Boolean(mapping(mapping(readStatus(status)).admin).bootstrap_required) || choice !== "off";
}
