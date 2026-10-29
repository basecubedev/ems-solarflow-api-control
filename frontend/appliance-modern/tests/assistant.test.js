// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  ASSISTANT_STATES,
  APPLIANCE_KINDS,
  STATE_LOOKS,
  STEP_IDS,
  STEP_LOOKS,
  TASKS,
  URGENT_STEPS,
  WELCOME_KEY,
  applianceKind,
  assistantMark,
  assistantSteps,
  markedSteps,
  assistantTasks,
  installationRead,
  opensOnAssistant,
  readStatus,
} from "../src/lib/assistant.js";
import { PAGE_IDS } from "../src/app/pages.js";
import { CONNECTIVITY } from "../src/lib/words.js";

const server = (name) => readFileSync(new URL(`../../../appliance/${name}`, import.meta.url), "utf8");

const status = (patch = {}) => ({
  admin: { installed: true, healthy: true, bootstrap_required: false, version: "v1.0.0", health: "healthy", container: { state: "running", health: "healthy" } },
  docker: { daemon: { state: "running" }, ems_container: "ems", containers: [{ name: "ems", state: "running" }] },
  network: { connectivity: "full", active_connection: "HomeNet" },
  updates: { security_count: 0, normal_count: 1 },
  ...patch,
});
const backup = (keys) => ({ account: { exists: true, key_count: keys } });
const states = (steps) => Object.fromEntries(steps.map((step) => [step.id, step.state]));
const next = (steps) => steps.filter((step) => step.next).map((step) => step.id);

test("a status that has not answered or failed is none, and the appliance is then unknown", () => {
  assert.equal(readStatus(undefined), null);
  assert.equal(readStatus(null), null);
  assert.equal(readStatus({ error: "unreachable" }), null);
  assert.equal(applianceKind(undefined), "unknown");
  assert.equal(applianceKind({ error: "unreachable" }), "unknown");
  assert.equal(applianceKind({}), "unknown");
});

const noEms = { daemon: { state: "running" }, ems_container: "ems", containers: [] };
const stoppedEms = { daemon: { state: "running" }, ems_container: "ems", containers: [{ name: "ems", state: "exited" }] };
const welcomeKind = (value) => {
  const kind = applianceKind(value);
  assert.ok(kind === "unknown" || APPLIANCE_KINDS.includes(kind), kind);
  return kind;
};
const downAdmin = { installed: true, healthy: false, bootstrap_required: false, health: "none", container: { state: "exited", health: "none" } };

test("the appliance is told by EMS Admin and the EMS container", () => {
  assert.equal(welcomeKind(status()), "running");
  assert.equal(welcomeKind(status({ docker: noEms })), "setup");
  assert.equal(welcomeKind(status({ admin: { installed: false, bootstrap_required: true } })), "bootstrap");
  assert.equal(welcomeKind(status({ docker: { daemon: { state: "inactive" } } })), "docker");
});

test("the welcome never asks to set up the EMS in an EMS Admin that is not healthy, nor one that is set up already", () => {
  assert.equal(welcomeKind(status({ admin: downAdmin, docker: stoppedEms })), "admin");
  assert.equal(welcomeKind(status({ admin: { installed: false, bootstrap_required: false }, docker: noEms })), "admin");
  assert.equal(welcomeKind(status({ admin: { ...status().admin, transition: { state: "live" } }, docker: noEms })), "admin");
  assert.equal(welcomeKind(status({ docker: stoppedEms })), "stopped");
  assert.equal(welcomeKind(status({ admin: downAdmin })), "running");
});

test("an Admin section the agent could not read leaves the EMS told by its container", () => {
  const unread = { status: "unavailable", error: "docker timed out" };
  assert.equal(welcomeKind(status({ admin: unread })), "running");
  assert.equal(welcomeKind(status({ admin: unread, docker: noEms })), "unknown");
  assert.deepEqual(states(assistantSteps({ status: status({ admin: unread }), backup: backup(1) })), { network: "done", admin: "unknown", ems: "done", updates: "done", backup: "done" });
  assert.equal(states(assistantSteps({ status: status({ admin: unread, docker: noEms }), backup: backup(1) })).ems, "unknown");
});

test("a Docker section the agent could not read never asks to set up an EMS that may run", () => {
  for (const docker of [{ section: "docker", status: "unavailable", error: "CommandError" }, { daemon: { state: "running" }, containers: [] }]) {
    assert.equal(welcomeKind(status({ docker })), "unknown");
    assert.equal(states(assistantSteps({ status: status({ docker }), backup: backup(1) })).ems, "unknown");
  }
});

test("while EMS Admin starts or is replaced, the EMS comes later rather than reading as unreadable", () => {
  const starting = { installed: true, healthy: false, bootstrap_required: false, health: "starting", container: { state: "running", health: "starting" } };
  assert.equal(states(assistantSteps({ status: status({ admin: starting, docker: noEms }), backup: backup(1) })).ems, "later");
  assert.equal(states(assistantSteps({ status: status({ admin: { ...status().admin, transition: { state: "live" } }, docker: noEms }), backup: backup(1) })).ems, "later");
});

test("on a fresh appliance EMS Admin is the next step, the EMS waits for it, backup access stays optional", () => {
  const steps = assistantSteps({ status: status({ admin: { installed: false, bootstrap_required: true }, docker: { daemon: { state: "running" }, ems_container: "ems", containers: [] } }), backup: backup(0) });
  assert.deepEqual(steps.map((step) => step.id), STEP_IDS);
  assert.deepEqual(states(steps), { network: "done", admin: "todo", ems: "later", updates: "done", backup: "todo" });
  assert.equal(steps.find((step) => step.id === "admin").kind, "bootstrap");
  assert.deepEqual(next(steps), ["admin"]);
});

test("with EMS Admin healthy and no EMS, setting up the EMS is next; a stopped EMS container is named", () => {
  const none = assistantSteps({ status: status({ docker: { daemon: { state: "running" }, ems_container: "ems", containers: [] } }), backup: backup(0) });
  assert.deepEqual(states(none), { network: "done", admin: "done", ems: "todo", updates: "done", backup: "todo" });
  assert.equal(none.find((step) => step.id === "ems").kind, "setup");
  assert.deepEqual(next(none), ["ems"]);
  const stopped = assistantSteps({ status: status({ docker: { daemon: { state: "running" }, ems_container: "ems", containers: [{ name: "ems", state: "exited" }] } }), backup: backup(1) });
  assert.equal(stopped.find((step) => step.id === "ems").kind, "stopped");
  const absent = assistantSteps({ status: status({ docker: { daemon: { state: "running" }, ems_container: "ems", containers: [{ name: "ems", state: "missing" }] } }), backup: backup(1) });
  assert.equal(absent.find((step) => step.id === "ems").kind, "setup");
  assert.match(server("docker_backend.py"), /^CONTAINER_MISSING = "missing"$/m);
  assert.match(server("docker_backend.py"), /^CONTAINER_RUNNING = "running"$/m);
  assert.match(server("docker_backend.py"), /^HEALTH_STARTING = "starting"$/m);
});

test("EMS Admin that is missing, unhealthy, behind a stopped Docker or being replaced is never done", () => {
  const kindOf = (patch) => assistantSteps({ status: status(patch), backup: backup(1) }).find((step) => step.id === "admin");
  assert.deepEqual([kindOf({ admin: { installed: false } }).state, kindOf({ admin: { installed: false } }).kind], ["todo", "missing"]);
  const failing = kindOf({ admin: { installed: true, healthy: false, health: "unhealthy", container: { state: "running", health: "unhealthy" } } });
  assert.deepEqual([failing.state, failing.kind], ["todo", "unhealthy"]);
  const stopped = kindOf({ admin: downAdmin });
  assert.deepEqual([stopped.state, stopped.kind], ["todo", "stopped"]);
  const starting = kindOf({ admin: { installed: true, healthy: false, health: "starting", container: { state: "running", health: "starting" } } });
  assert.deepEqual([starting.state, starting.kind], ["unknown", "starting"]);
  assert.deepEqual([kindOf({ docker: { daemon: { state: "failed" } } }).state, kindOf({ docker: { daemon: { state: "failed" } } }).kind], ["todo", "docker"]);
  const noDocker = kindOf({ admin: {}, docker: { daemon: { state: "unavailable" } } });
  assert.deepEqual([noDocker.state, noDocker.kind], ["todo", "docker"]);
  assert.equal(applianceKind(status({ admin: {}, docker: { daemon: { state: "unavailable" } } })), "docker");
  const live = kindOf({ admin: { installed: true, healthy: true, transition: { state: "live" } } });
  assert.deepEqual([live.state, live.kind], ["unknown", "live"]);
});

test("security updates waiting or a failed check keep the update step to do; an optional step is next only when nothing else is", () => {
  const waiting = assistantSteps({ status: status({ updates: { security_count: 2 } }), backup: backup(0) });
  const updates = waiting.find((step) => step.id === "updates");
  assert.deepEqual([updates.state, updates.kind, updates.count], ["todo", "security", 2]);
  assert.deepEqual(next(waiting), ["updates"]);
  const doubt = assistantSteps({ status: status({ updates: { security_count: 0, error: "update_check_failed" } }), backup: backup(1) });
  assert.equal(doubt.find((step) => step.id === "updates").kind, "doubt");
  const onlyOptional = assistantSteps({ status: status(), backup: backup(0) });
  assert.deepEqual(next(onlyOptional), ["backup"]);
  assert.deepEqual(next(assistantSteps({ status: status(), backup: backup(2) })), []);
});

test("a network that is not fully online is to do; an unknown connectivity is unknown", () => {
  for (const connectivity of CONNECTIVITY.filter((value) => !["full", "unknown"].includes(value))) {
    assert.equal(states(assistantSteps({ status: status({ network: { connectivity } }), backup: backup(1) })).network, "todo", connectivity);
  }
  assert.equal(states(assistantSteps({ status: status({ network: { connectivity: "unknown" } }), backup: backup(1) })).network, "unknown");
  assert.equal(assistantSteps({ status: status(), backup: backup(1) })[0].connection, "HomeNet");
});

test("a step whose answer has not come, or failed, is unknown and never done", () => {
  assert.ok(assistantSteps({ status: undefined, backup: undefined }).every((step) => step.state === "unknown"));
  assert.ok(assistantSteps({ status: { error: "unreachable" }, backup: { error: "unreachable" } }).every((step) => step.state === "unknown"));
  assert.equal(states(assistantSteps({ status: status(), backup: null })).backup, "unknown");
});

test("every state and step has its look, every task opens a page of this console or EMS Admin", () => {
  assert.deepEqual(Object.keys(STATE_LOOKS).sort(), [...ASSISTANT_STATES].sort());
  assert.deepEqual(Object.keys(STEP_LOOKS).sort(), [...STEP_IDS].sort());
  for (const look of [...Object.values(STEP_LOOKS), ...TASKS]) assert.match(look.tone, /^c-[a-z]+$/);
  for (const task of TASKS) assert.ok(task.external || PAGE_IDS.includes(task.page), task.id);
  assert.deepEqual(assistantTasks("").map((task) => task.id), TASKS.filter((task) => !task.external).map((task) => task.id));
  assert.equal(assistantTasks("http://pi:8090/").length, TASKS.length);
});

test("the console opens on the assistant always while nothing is installed, otherwise until the reader turns it off", () => {
  const fresh = status({ admin: { installed: false, bootstrap_required: true } });
  assert.equal(opensOnAssistant(fresh, "off"), true);
  assert.equal(opensOnAssistant(status({ admin: { installed: false, bootstrap_required: true }, docker: { daemon: { state: "inactive" }, ems_container: "ems", containers: [] } }), "off"), true);
  assert.equal(opensOnAssistant(status(), "on"), true);
  assert.equal(opensOnAssistant(status(), "off"), false);
  assert.equal(opensOnAssistant(undefined, "off"), false);
  assert.equal(installationRead(status()), true);
  assert.equal(installationRead(status({ admin: { installed: false, bootstrap_required: true } })), true);
  for (const unread of [undefined, null, { error: "unreachable" }, status({ admin: { status: "unavailable", error: "CommandError" } })]) assert.equal(installationRead(unread), false);
  assert.ok(WELCOME_KEY.startsWith("ems-modern."));
});

test("the status fields the steps read are the ones the agent sends", () => {
  const sent = {
    "admin_lifecycle.py": ["bootstrap_required", "container", "health"],
    "packages.py": ["security_count"],
    "network.py": ["connectivity", "active_connection"],
    "backup_access.py": ["key_count"],
    "status.py": ["ems_container"],
  };
  for (const [file, fields] of Object.entries(sent)) {
    const text = server(file);
    for (const field of fields) assert.ok(text.includes(`"${field}"`), `${file}: ${field}`);
  }
});

test("the menu marks the assistant amber while EMS Admin or the EMS is not there or not running, quietly while another required step is left", () => {
  const mark = (value) => assistantMark(markedSteps(assistantSteps({ status: value, backup: null })), URGENT_STEPS);
  assert.equal(mark(status({ admin: { installed: false, bootstrap_required: true } })), "warning");
  assert.equal(mark(status({ docker: noEms })), "warning");
  assert.equal(mark(status({ docker: stoppedEms })), "warning");
  assert.equal(mark(status({ admin: downAdmin })), "warning");
  assert.equal(mark(status({ docker: { daemon: { state: "inactive" } } })), "warning");
  assert.equal(mark(status({ updates: { security_count: 2, normal_count: 0 } })), "info");
  assert.equal(mark(status({ network: { connectivity: "portal" } })), "info");
  assert.equal(mark(status({ network: { connectivity: "none" } })), "info");
  assert.equal(mark(status({ network: { connectivity: "limited" } })), "", "a LAN without internet stays a step, not a mark");
  assert.equal(assistantSteps({ status: status({ network: { connectivity: "limited" } }), backup: null })[0].state, "todo");
  assert.equal(mark(status()), "");
  assert.equal(mark(undefined), "");
  assert.equal(mark({ error: "unreachable" }), "");
  assert.equal(assistantMark(assistantSteps({ status: status(), backup: backup(0) }), URGENT_STEPS), "", "backup access is optional and never marks the menu");
});
