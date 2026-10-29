// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { adminContainer, adminGate, adminHealth, adminVersion, dockerCondition, exactDuration, gigabytes, groupByTrack, indexAge, latestVerdict, osTiles, securityUpdatesOffered, systemVerdict } from "../src/lib/host.js";
import { planBlocked, planFields, vocabularyWords } from "../src/lib/plan.js";
import { logSources, logSummary } from "../src/lib/logs.js";
import { errorWords } from "../src/lib/errors.js";
import { adminLink } from "../src/lib/links.js";
import { ApiError } from "../../shared/lib/api.js";

const withDocker = (state, admin) => ({ docker: { daemon: { state } }, admin });

test("an unread daemon is not a stopped one", () => {
  assert.equal(dockerCondition(""), "unknown");
  assert.equal(dockerCondition("running"), "running");
  assert.equal(dockerCondition("unavailable"), "unavailable");
  assert.equal(dockerCondition("exited"), "stopped");
});

test("the Admin gate walks unread, docker, bootstrap, missing, live and ready", () => {
  assert.equal(adminGate({}), "unread");
  assert.equal(adminGate(withDocker("exited", { installed: true })), "docker");
  assert.equal(adminGate(withDocker("running", { installed: false, bootstrap_required: true })), "bootstrap");
  assert.equal(adminGate(withDocker("running", { installed: false })), "missing");
  assert.equal(adminGate(withDocker("running", { installed: true, transition: { state: "live" } })), "live");
  assert.equal(adminGate(withDocker("running", { installed: true })), "ready");
  assert.equal(adminGate(withDocker("", { installed: true })), "ready");
});

test("the installed version never reads as missing while Docker is down", () => {
  assert.deepEqual(adminVersion(withDocker("exited", { installed: false })), { key: "admin.version.unknown" });
  assert.deepEqual(adminVersion(withDocker("running", { installed: false })), { key: "admin.version.notInstalled" });
  assert.deepEqual(adminVersion(withDocker("running", { installed: true, version: "v1.0.0" })), { text: "v1.0.0" });
});

test("releases group by track in the trusted order, an unknown track with the least trusted", () => {
  const groups = groupByTrack([{ tag: "a", track: "unstable" }, { tag: "b", track: "stable" }, { tag: "c", track: "odd" }]);
  assert.deepEqual(groups.map((group) => [group.track, group.items.map((item) => item.tag)]), [["stable", ["b"]], ["unstable", ["a"]], ["experimental", ["c"]]]);
});

test("only an upgrade is an update; an unknown direction may be the same image", () => {
  assert.equal(latestVerdict({ direction: "upgrade" }).key, "updates.verdict.available");
  assert.equal(latestVerdict({ direction: "unknown" }).key, "updates.verdict.uncomparable");
});

test("security updates are offered only when the counts can be trusted", () => {
  assert.equal(securityUpdatesOffered({ security_count: 2, package_manager: { healthy: true } }), true);
  assert.equal(securityUpdatesOffered({ security_count: 2, package_manager: { healthy: false } }), false);
  assert.equal(securityUpdatesOffered({ security_count: 2, error: "update_check_failed" }), false);
  assert.equal(securityUpdatesOffered({ security_count: 0 }), false);
});

test("the OS verdict puts a doubt before the counts, and a stale index after security updates", () => {
  const doubt = { code: "package_manager_unhealthy", severity: "error", section: "updates" };
  const stale = { code: "package_index_stale", severity: "warning", section: "updates" };
  assert.equal(systemVerdict({ security_count: 1, package_manager: { healthy: false } }, [stale, doubt]).finding, doubt);
  assert.equal(systemVerdict({ security_count: 1, package_manager: { healthy: true } }, [stale]).key, "updates.os.security");
  assert.equal(systemVerdict({ security_count: 0, normal_count: 0, package_manager: { healthy: true } }, [stale]).finding, stale);
  assert.equal(systemVerdict({}, []).key, "updates.os.unknown");
});

test("sizes and durations read the way Classic reads them", () => {
  assert.deepEqual(gigabytes(141999), { value: 139, digits: 0 });
  assert.deepEqual(gigabytes(2048), { value: 2, digits: 1 });
  assert.equal(gigabytes(0), null);
  assert.deepEqual(exactDuration(1800), { unit: "minute", count: 30 });
  assert.deepEqual(exactDuration(5400), { unit: "minute", count: 90 });
  assert.deepEqual(exactDuration(43200), { unit: "hour", count: 12 });
  assert.equal(exactDuration(1.5), null);
  assert.deepEqual(indexAge({ index_age_seconds: 3607 }), { unit: "hour", count: 1 });
  assert.deepEqual(indexAge({ index_age_seconds: 3 * 86400 }), { unit: "day", count: 3 });
  assert.equal(indexAge({}), null);
});

test("a plan reads in Classic's order, hides the seal in Basic and leaves objects to their blocks", () => {
  const plan = { authority: "abc", target_tag: "v1", type: "admin.install", blockers: [{ code: "x" }], novel_field: "y", target: { admin_version: "v1" } };
  assert.deepEqual(planFields(plan, false).map((field) => field.key), ["type", "target_tag", "novel_field"]);
  assert.deepEqual(planFields(plan, true).map((field) => field.key), ["type", "target_tag", "authority", "novel_field"]);
  assert.equal(planFields(plan, false).find((field) => field.key === "novel_field").known, false);
  assert.equal(planBlocked(plan), true);
  assert.equal(planBlocked({}), false);
});

test("free space is shown once measured, and said to be unmeasured rather than 0 MB when it is not", () => {
  const measured = planFields({ minimum_free_megabytes: 1024, free_megabytes: 141312, free_space_known: true }, false);
  assert.deepEqual(measured.map((field) => field.key), ["minimum_free_megabytes", "free_megabytes"]);
  const unmeasured = planFields({ minimum_free_megabytes: 1024, free_megabytes: 0, free_space_known: false }, false);
  assert.deepEqual(unmeasured.map((field) => [field.key, field.value, field.known]), [["minimum_free_megabytes", 1024, true], ["free_space_known", false, true]]);
});

test("a plan's vocabulary values are translated, an unknown one stays the appliance's English, data stays data", () => {
  assert.deepEqual(vocabularyWords("action", "restart"), { key: "admin.restart" });
  assert.deepEqual(vocabularyWords("action", "fix_broken"), { key: "repair.fix_broken" });
  assert.deepEqual(vocabularyWords("scope", "security"), { key: "plan.scope.security" });
  assert.deepEqual(vocabularyWords("direction", "downgrade"), { key: "manager.direction.downgrade" });
  assert.deepEqual(vocabularyWords("current_state", "running"), { key: "container.running" });
  assert.deepEqual(vocabularyWords("target_channel", "previous_known_good"), { key: "install.channel.previous_known_good" });
  assert.deepEqual(vocabularyWords("lock_state", "free"), { text: "free", lang: "en" });
  assert.deepEqual(vocabularyWords("action", "restart_policy"), { text: "restart policy", lang: "en" });
  assert.equal(vocabularyWords("hostname", "ems-pi"), null);
});

test("Basic offers the three first-look logs the backend declares; Expert every declared one", () => {
  const settings = { log_sources: ["appliance_web", "operations", "audit", "admin_container"] };
  assert.deepEqual(logSources(settings, false), ["admin_container", "operations", "audit"]);
  assert.deepEqual(logSources(settings, true), settings.log_sources);
  assert.deepEqual(logSources({}, true), []);
  assert.equal(logSummary({ unreadable: "permission denied" }).unreadable, "permission denied");
  assert.deepEqual(logSummary({ lines: 2, text: "a\nb", truncated: true }), { unreadable: "", lines: 2, truncated: true, text: "a\nb" });
});

test("a known refusal is translated, an unknown one keeps the server's sentence, a lost request says so", () => {
  assert.deepEqual(errorWords(new ApiError(403, { error: "password_unconfirmed", message: "x" })), { key: "error.password_unconfirmed" });
  assert.deepEqual(errorWords(new ApiError(400, { error: "new_code", message: "the server says" })), { text: "the server says", lang: "en" });
  assert.deepEqual(errorWords(new TypeError("Failed to fetch")), { key: "error.unreachable" });
  assert.deepEqual(errorWords(new ApiError(502, null), "error.planFailed"), { key: "error.planFailed", vars: { code: "http_502" } });
});

test("the EMS Admin link uses the address this browser reached the appliance by, and only while Admin is installed", () => {
  const status = { admin: { installed: true } };
  assert.equal(adminLink({ hostname: "ems-solarflow.local" }, { admin_port: 8090 }, status), "http://ems-solarflow.local:8090/");
  assert.equal(adminLink({ hostname: "[fe80::1]" }, { admin_port: 8090 }, status), "http://[fe80::1]:8090/");
  assert.equal(adminLink({ hostname: "x" }, { admin_port: 8090 }, { admin: { installed: false } }), "");
  assert.equal(adminLink({ hostname: "x" }, { admin_port: "nope" }, status), "");
});

const update = {
  type: "manager.update",
  version: "0.3.15",
  installed_version: "0.3.14",
  release_id: "r-15",
  build_id: "b-15",
  digest: "sha256:abc",
  size_bytes: 12345678,
  revert_available: true,
  verify_window_seconds: 900,
  warning: "Install the newer package, then confirm it works.",
};

test("a Manager update plan names its facts, in order, with the identities only in Expert and the warning left to its own block", () => {
  const basic = planFields(update, false);
  assert.deepEqual(basic.map((field) => field.key), ["type", "version", "installed_version", "size_bytes", "revert_available", "verify_window_seconds"]);
  assert.ok(basic.every((field) => field.known));
  assert.deepEqual(basic.map((field) => field.kind), ["text", "text", "text", "bytes", "bool", "seconds"]);
  const expert = planFields(update, true);
  assert.deepEqual(expert.slice(-3).map((field) => field.key), ["release_id", "build_id", "digest"]);
  assert.equal(expert.find((field) => field.key === "digest").mono, true);
  assert.ok(!expert.some((field) => field.key === "warning"));
});

test("a WLAN, hostname or SSH plan reads its signal as a share, its addresses as data and its note in the appliance's English", () => {
  const wifi = planFields({ type: "wifi.apply", ssid: "Home", signal: 72, revert_timeout_seconds: 120, security: "WPA2" }, false);
  assert.deepEqual(wifi.map((field) => [field.key, field.kind]), [["type", "text"], ["ssid", "text"], ["security", "text"], ["signal", "percent"], ["revert_timeout_seconds", "seconds"]]);
  assert.equal(planFields({ type: "network.hostname", new_url: "http://ems.local:8088" }, false)[1].mono, true);
  assert.deepEqual(vocabularyWords("note", "Password authentication stays disabled."), { text: "Password authentication stays disabled.", lang: "en" });
  assert.equal(vocabularyWords("current", "active"), null);
  assert.ok(!planFields({ type: "ssh.service", current: { active: "active" } }, true).some((field) => field.key === "current"));
});

test("an OS tile the agent did not answer is unknown, and a check that did not finish gives no counts", () => {
  const unknown = { key: "common.unknown", tone: "c-idle" };
  assert.deepEqual(osTiles({}), { securityCount: null, normalCount: null, kernel: null, firmware: null, security: unknown, reboot: unknown, manager: unknown });
  const failed = osTiles({ security_count: 0, normal_count: 0, kernel_update: false, firmware_update: false, error: "update_check_failed", reboot_required: false, package_manager: { healthy: true } });
  assert.deepEqual([failed.securityCount, failed.normalCount, failed.kernel, failed.firmware, failed.security.key], [null, null, null, null, "os.notCertain"]);
  assert.deepEqual([osTiles({ security_count: 1, kernel_update: true, firmware_update: false }).kernel, osTiles({ security_count: 1, kernel_update: true, firmware_update: false }).firmware], [true, false]);
  const broken = osTiles({ security_count: 3, normal_count: 1, package_manager: { healthy: false } });
  assert.deepEqual([broken.securityCount, broken.security.key, broken.manager.key], [3, "os.notCertain", "os.needsRecovery"]);
  const fine = osTiles({ security_count: 2, normal_count: 1, reboot_required: true, package_manager: { healthy: true } });
  assert.deepEqual([fine.security.key, fine.reboot.key, fine.manager.key], ["os.recommended", "os.rebootRequired", "os.healthy"]);
  assert.equal(osTiles({ security_count: 0, package_manager: { healthy: true } }).security.key, "overview.upToDate");
});

test("EMS Admin is unknown while the agent cannot say, and missing only when Docker answered that it is gone", () => {
  assert.equal(adminHealth(undefined).key, "common.unknown");
  assert.equal(adminHealth({ admin: {} }).key, "common.unknown");
  assert.equal(adminHealth(withDocker("inactive", { installed: false })).key, "common.unknown");
  assert.equal(adminHealth({ docker_missing: true, docker: { daemon: { state: "missing" } }, admin: { installed: false } }).key, "common.unknown");
  assert.equal(adminHealth(withDocker("running", { installed: false })).key, "admin.health.missing");
  assert.equal(adminHealth(withDocker("running", { installed: true, healthy: true })).key, "admin.health.healthy");
  assert.equal(adminHealth(withDocker("running", { installed: true, healthy: false })).key, "admin.health.unhealthy");
  assert.deepEqual(adminContainer(withDocker("inactive", { installed: false, container: { state: "missing", health: "none" } })), {});
  assert.deepEqual(adminContainer(withDocker("running", { installed: true, container: { state: "running", health: "healthy" } })), { state: "running", health: "healthy" });
  assert.deepEqual(adminContainer(undefined), {});
});
