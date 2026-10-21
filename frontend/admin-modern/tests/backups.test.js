// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { CREATE_SCOPES, CREATE_STEPS, RESTORE_STEPS, FILE_ACTIONS, archiveView, latestBackup, listWarningView, newestFirst, scopeAvailable, setView, stepView, fileActionKey } from "../src/lib/backups.js";

const SERVER = readFileSync(new URL("../../../admin/backup_restore.py", import.meta.url), "utf8");

test("the scopes, steps and file actions are the server's", () => {
  const scopes = SERVER.match(/CREATE_SCOPES = \(([^)]*)\)/)[1];
  assert.deepEqual([...scopes.matchAll(/"([a-z]+)"/g)].map((match) => match[1]).sort(), [...CREATE_SCOPES].sort());
  const steps = new Set([...SERVER.matchAll(/\("([a-z_]+)", "[A-Z][^"]*"\)/g)].map((match) => match[1]));
  for (const match of SERVER.matchAll(/_step\("([a-z_]+)"/g)) steps.add(match[1]);
  for (const match of SERVER.matchAll(/"key": f?"([a-z_]+)(?:\{suffix\})?"/g)) steps.add(match[1]);
  for (const match of SERVER.matchAll(/_step\(f"([a-z_]+)\{suffix\}"/g)) steps.add(match[1]);
  for (const match of SERVER.matchAll(/f"(create|verify)_\{scope\}"/g)) ["config", "databases", "influxdb"].forEach((scope) => steps.add(`${match[1]}_${scope}`));
  const known = [...CREATE_STEPS, ...RESTORE_STEPS];
  assert.deepEqual([...steps].filter((step) => !known.includes(step)).sort(), []);
  const actions = new Set([...SERVER.matchAll(/(?:return |"action": )"((?:would|conflict)_[a-z_]+|checksum_[a-z_]+)"/g)].map((match) => match[1]));
  assert.ok(actions.size >= 6, [...actions].join());
  assert.deepEqual([...actions].filter((action) => !FILE_ACTIONS.includes(action)).sort(), []);
});

test("a step of a restore names the archive it works on when a set has several", () => {
  const targets = [{ backup_type: "config" }, { backup_type: "databases" }];
  assert.deepEqual(stepView({ key: "apply_1", state: "running", label: "Restore databases" }, targets), {
    key: "apply_1", label: "backup.step.apply.typed", text: "", type: "databases", state: "running", tone: "c-acc", icon: "loader", detail: "",
  });
  assert.equal(stepView({ key: "apply", state: "done" }, []).label, "backup.step.apply");
  assert.equal(stepView({ key: "verify_plan", state: "done" }, targets).label, "backup.step.verify_plan");
  const unknown = stepView({ key: "beam_up", state: "warp", label: "Beam up", message: "ok" });
  assert.deepEqual([unknown.label, unknown.text, unknown.state, unknown.detail], [null, "Beam up", "pending", "ok"]);
});

test("an archive the server cannot read is never offered as restorable", () => {
  const view = archiveView({ id: "a", name: "x.tar.gz", backup_type: "config", valid: false, error: "manifest could not be read", size_bytes: "12" });
  assert.equal(view.valid, false);
  assert.equal(view.problem, "manifest could not be read");
  assert.equal(view.size, 12);
  assert.equal(archiveView({ id: "b", backup_type: "mystery" }).typeText, "mystery");
  assert.equal(archiveView({ id: "c", valid: true }).valid, true);
  assert.equal(archiveView({ id: "d" }).valid, true);
});

test("a set is complete only when every member archive is there", () => {
  assert.equal(setView({ id: "s", archives: [{ type: "config", present: true }, { type: "influxdb", present: false }] }).complete, false);
  assert.equal(setView({ id: "s", archives: [{ type: "config" }] }).complete, true);
  assert.deepEqual([setView({ id: "s", status: "partial", archives: [{ type: "config" }] }).complete, setView({ id: "s", status: "partial", archives: [] }).partial], [false, true]);
  const gone = setView({ id: "s", status: "partial", archives: [{ type: "config", present: false }] });
  assert.deepEqual([gone.missing, gone.restorable], [true, false]);
  assert.equal(setView({ id: "s", archives: [] }).complete, false);
});

test("analytics can be backed up only where the bundled database runs", () => {
  assert.equal(scopeAvailable("influxdb", { influxdb: { supported: false } }), false);
  assert.equal(scopeAvailable("influxdb", { influxdb: { supported: true } }), true);
  assert.equal(scopeAvailable("system", null), true);
  assert.equal(latestBackup({ summary: { latest_created_at: "2026-10-20T10:00:00Z" } }), "2026-10-20T10:00:00Z");
  assert.equal(latestBackup({ summary: {} }), null);
  assert.equal(fileActionKey("would_replace_conflict"), "backup.file.would_replace_conflict");
  assert.equal(fileActionKey("would_explode"), null);
});

test("a list warning the server is known to give is said in the reader's language", () => {
  assert.deepEqual(listWarningView("Some backups are encrypted and locked until unlocked."), { key: "backups.warning.locked", text: "Some backups are encrypted and locked until unlocked." });
  assert.deepEqual(listWarningView("Disk is on fire."), { key: null, text: "Disk is on fire." });
  assert.equal(listWarningView(""), null);
});

test("the list reads newest first whatever order the server sends, unreadable times last", () => {
  const items = [{ id: "a", createdAt: "2026-01-01T10:00:00Z" }, { id: "b", createdAt: "" }, { id: "c", createdAt: "2026-03-01T10:00:00Z" }, { id: "d", createdAt: "2025-12-31T10:00:00Z" }, { id: "e" }];
  assert.deepEqual(newestFirst(items).map((item) => item.id), ["c", "a", "d", "b", "e"]);
});
