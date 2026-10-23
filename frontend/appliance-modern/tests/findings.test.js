// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { attentionBySection, findingTarget, findingWords, overviewVerdict, rankedFindings, sessionFindings } from "../src/lib/findings.js";

const finding = (code, severity, section = "updates") => ({ code, severity, section, title: `${code} title`, message: "m", next_step: "n" });
const status = (level, warnings = []) => ({ health: { level, warnings } });

test("a status read that failed is the one finding, and the verdict says it was not read", () => {
  const failed = { error: "agent_unavailable" };
  assert.deepEqual(rankedFindings(failed, [finding("x", "warning")]).map((item) => item.code), ["status_unavailable"]);
  assert.deepEqual(overviewVerdict(failed), { key: "verdict.unread", tone: "fault" });
  assert.deepEqual(overviewVerdict(undefined), { key: "verdict.loading", tone: "idle" });
});

test("findings rank errors first and keep the server's order within a severity", () => {
  const ranked = rankedFindings(status("degraded", [finding("a", "warning"), finding("b", "error"), finding("c", "warning")]), [finding("d", "error", "settings")]);
  assert.deepEqual(ranked.map((item) => item.code), ["b", "d", "a", "c"]);
});

test("the browser's own findings raise the verdict and never lower it; info moves nothing", () => {
  assert.equal(overviewVerdict(status("healthy"), [finding("x", "warning")]).key, "verdict.attention");
  assert.equal(overviewVerdict(status("healthy"), [finding("x", "error")]).key, "verdict.degraded");
  assert.equal(overviewVerdict(status("healthy"), [finding("x", "info")]).key, "verdict.healthy");
  assert.equal(overviewVerdict(status("degraded"), [finding("x", "info")]).key, "verdict.degraded");
  assert.equal(overviewVerdict(status("mystery")).key, "verdict.unknown");
});

test("only an explicit no is a password finding, and a degraded audit is one", () => {
  assert.deepEqual(sessionFindings(null, null), []);
  assert.deepEqual(sessionFindings(null, true), []);
  assert.deepEqual(sessionFindings(null, false).map((item) => item.code), ["password_unconfirmed"]);
  assert.deepEqual(sessionFindings({ degraded: true, unrecorded_events: 3 }, null).map((item) => [item.code, item.severity, item.facts.unrecorded]), [["security_audit_degraded", "error", 3]]);
});

test("the menu marks carry the worst severity per page", () => {
  const marks = attentionBySection(status("degraded", [finding("a", "warning", "updates"), finding("b", "error", "updates"), finding("c", "warning", "access")]));
  assert.deepEqual(marks, { updates: "error", access: "warning" });
});

test("a finding opens its page only when that changes the page and the page exists", () => {
  const pages = ["overview", "updates", "admin"];
  assert.equal(findingTarget(finding("a", "warning", "updates"), "overview", pages), "updates");
  assert.equal(findingTarget(finding("a", "warning", "updates"), "updates", pages), null);
  assert.equal(findingTarget(finding("a", "warning", "nowhere"), "overview", pages), null);
});

test("a known code is said with facts from the status; an unknown one keeps the server's English", () => {
  const known = findingWords(finding("security_updates_pending", "warning"), { updates: { security_count: 4 } });
  assert.equal(known.known, true);
  assert.deepEqual(known.message, { key: "finding.security_updates_pending.message", vars: {}, count: 4 });
  const stale = findingWords(finding("package_index_stale", "warning"), { updates: { index_age_seconds: 9 * 86400 + 5 } });
  assert.equal(stale.message.count, 9);
  const ems = findingWords(finding("ems_not_running", "error"), { docker: { ems_container: "ems", containers: [{ name: "ems", state: "exited" }] } });
  assert.deepEqual(ems.message.vars, { name: "ems", state: { key: "container.exited" } });
  const section = findingWords(finding("network_unavailable", "warning"), {});
  assert.equal(section.title.key, "finding.network_unavailable.title");
  const docker = findingWords(finding("docker_not_running", "error"), { docker: { daemon: { state: "stopped" } } });
  assert.deepEqual(docker.message.vars.state, { key: "docker.stopped" });
  const odd = findingWords(finding("ems_not_running", "error"), { docker: { ems_container: "ems", containers: [{ name: "ems", state: "weird" }] } });
  assert.deepEqual(odd.message.vars.state, { text: "weird", lang: "en" });
  const unknown = findingWords(finding("brand_new", "warning"), {});
  assert.deepEqual(unknown.title, { text: "brand_new title", lang: "en" });
});
