// SPDX-License-Identifier: AGPL-3.0-or-later
import test from "node:test";
import assert from "node:assert/strict";
import { migrationReview } from "../src/lib/migration.js";

const answer = (review, revision = "rev-1") => ({ status: "ok", revision, review });

test("a review the server could not make is no review", () => {
  assert.equal(migrationReview({ status: "invalid", message: "x" }), null);
  assert.equal(migrationReview(null), null);
});

test("each device says what it becomes, and the losses are counted", () => {
  const view = migrationReview(answer({
    needs_migration: true,
    final_valid: true,
    changes: [
      { device: "INV_1", hardware_profile: "solarflow_800_pro", disables_control: false, message: "kept" },
      { device_id: "abc", disables_control: true, message: "telemetry only" },
    ],
  }));
  assert.deepEqual(view.devices, [
    { name: "INV_1", model: "solarflow_800_pro", disables: false, message: "kept" },
    { name: "abc", model: "", disables: true, message: "telemetry only" },
  ]);
  assert.equal(view.losing, 1);
  assert.equal(view.canApply, true);
});

test("an apply needs a required, valid migration and the review's revision", () => {
  assert.equal(migrationReview(answer({ needs_migration: false, final_valid: true })).canApply, false);
  assert.equal(migrationReview(answer({ needs_migration: true, final_valid: false, validation_errors: [{ message: "bad" }, "worse"] })).canApply, false);
  assert.deepEqual(migrationReview(answer({ needs_migration: true, final_valid: false, validation_errors: [{ message: "bad" }, "worse"] })).errors, ["bad", "worse"]);
  assert.equal(migrationReview(answer({ needs_migration: true, final_valid: true }, "")).canApply, false);
});
