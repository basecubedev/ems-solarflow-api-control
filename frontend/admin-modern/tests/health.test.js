// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { FINDINGS, findingView, healthVerdict, emsFact } from "../src/lib/health.js";

const SERVER = readFileSync(new URL("../../../admin/maintenance_health.py", import.meta.url), "utf8");

const overview = (patch = {}) => ({
  install_state: { state: "standard_install" },
  paths: { config: { exists: true, placeholder_fields: [] }, compose: { exists: true }, data: { exists: true } },
  docker: { available: true },
  containers: { ems: { found: true, running: true, status: "running", tag: "v0.8.17", started_at: "2026-10-20T10:00:00Z" } },
  health: { status: "ok", findings: [] },
  ...patch,
});

test("every finding code the server can rank has an answer here", () => {
  const codes = new Set([...SERVER.matchAll(/_finding\(\s*"([a-z_]+)"/g)].map((match) => match[1]));
  assert.ok(codes.size >= 9, [...codes].join());
  assert.deepEqual([...codes].filter((code) => !Object.hasOwn(FINDINGS, code)).sort(), []);
  assert.deepEqual(Object.keys(FINDINGS).filter((code) => !codes.has(code)).sort(), []);
});

test("the server's ranking is kept, and a known finding takes its facts from the overview", () => {
  const data = overview({
    containers: { ems: { found: true, running: false, status: "exited" } },
    paths: { config: { exists: true, placeholder_fields: ["devices[0].ip"] }, compose: { exists: true }, data: { exists: true } },
    health: {
      status: "error",
      findings: [
        { code: "ems_stopped", severity: "error", title: "EMS is not running" },
        { code: "ems_safe_mode", severity: "warning", title: "EMS writes nothing yet" },
      ],
    },
  });
  const verdict = healthVerdict(data, false);
  assert.equal(verdict.level, "error");
  assert.deepEqual(verdict.findings.map((finding) => finding.code), ["ems_stopped", "ems_safe_mode"]);
  assert.deepEqual(verdict.findings[0].vars, { status: { key: "container.status.exited" } });
  assert.deepEqual(verdict.findings[1].vars, { fields: ["devices[0].ip"] });
  assert.equal(verdict.findings[0].actions[0].target, "services");
});

test("an unknown code keeps the server's words and offers no action", () => {
  const view = findingView({ code: "brand_new", severity: "info", title: "T", message: "M", next_step: "N" }, overview());
  assert.equal(view.known, false);
  assert.deepEqual([view.title, view.message, view.next], ["T", "M", "N"]);
  assert.deepEqual(view.actions, []);
  assert.equal(findingView({ code: "x", severity: "bogus" }).severity, "warning");
});

test("an incomplete installation names what is missing, in the server's state", () => {
  const data = overview({
    install_state: { state: "standard_config_only" },
    paths: { config: { exists: true }, compose: { exists: false }, data: { exists: false } },
    health: { status: "error", findings: [{ code: "installation_incomplete", severity: "error" }] },
  });
  assert.deepEqual(healthVerdict(data).findings[0].vars, { state: "standard_config_only", missing: ["compose", "data"] });
  const odd = overview({ install_state: { state: "something_new" }, health: { status: "error", findings: [{ code: "installation_incomplete", severity: "error" }] } });
  assert.equal(healthVerdict(odd).findings[0].vars.state, "partial_install");
});

test("the verdict never reads healthy without a readable answer", () => {
  assert.equal(healthVerdict(null, false).level, "loading");
  assert.equal(healthVerdict(null, true).level, "unreadable");
  assert.equal(healthVerdict(overview({ health: null })).level, "unreadable");
  assert.equal(healthVerdict(overview({ health: { status: "ok", findings: "nope" } })).level, "unreadable");
  assert.equal(healthVerdict(overview()).level, "ok");
});

test("a healthy answer that could not be read again is not passed off as current", () => {
  const verdict = healthVerdict(overview(), true);
  assert.equal(verdict.level, "unreadable");
  assert.equal(verdict.alert, null);
});

test("with nothing installed, the other findings stay and the worst of them colours the pill", () => {
  const bare = healthVerdict(overview({ install_state: { state: "none" }, health: { findings: [{ code: "installation_incomplete", severity: "error" }] } }));
  assert.deepEqual([bare.level, bare.findings.length, bare.alert], ["none", 0, null]);
  const blind = healthVerdict(overview({
    install_state: { state: "none" },
    docker: { available: false, error: "socket refused" },
    health: { findings: [{ code: "docker_unavailable", severity: "error" }, { code: "installation_incomplete", severity: "error" }] },
  }));
  assert.deepEqual([blind.level, blind.findings.map((finding) => finding.code), blind.alert], ["none", ["docker_unavailable"], "error"]);
});

test("EMS as one fact: the start time only while it runs", () => {
  assert.deepEqual(emsFact(overview()), { running: true, found: true, status: { key: "container.status.running" }, version: "v0.8.17", startedAt: "2026-10-20T10:00:00Z" });
  const stopped = emsFact(overview({ containers: { ems: { found: true, running: false, status: "paused", started_at: "x" } }, components: { ems: { tag: "v1" } } }));
  assert.equal(stopped.startedAt, null);
  assert.equal(stopped.version, "v1");
  assert.deepEqual(emsFact(overview({ containers: { ems: { status: "weird" } } })).status, { text: "weird" });
});
