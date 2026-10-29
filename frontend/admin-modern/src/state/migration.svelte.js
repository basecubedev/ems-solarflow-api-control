// SPDX-License-Identifier: AGPL-3.0-or-later
import { adminSend } from "./auth.svelte.js";
import { system, reload } from "./system.svelte.js";

const PATH = "/api/admin/maintenance/zendure-mqtt/migration-apply";

/**
 * The migration of an older MQTT setup, held here rather than by the page:
 * the write runs on the server whatever page is open, and coming back finds
 * its result. The result belongs to the review read back after it, and is
 * shown only while that review is still the current one: a restore or another
 * console that changes the setup makes it history.
 */
class MigrationApply {
  busy = $state(false);
  outcome = $state.raw(null);
}

export const migration = new MigrationApply();

/** Applies the reviewed migration, with a backup first unless the reader turned it off. */
export async function applyMigration({ revision, backup = true }) {
  if (migration.busy || !revision) return;
  migration.busy = true;
  migration.outcome = null;
  let outcome;
  try {
    const result = await adminSend(PATH, { body: { revision, confirm: true, backup } });
    outcome = result?.ok === false ? { error: result } : { result };
  } catch (error) {
    outcome = { error, conflict: error?.payload?.status === "conflict" };
  }
  try {
    await reload("migration");
  } finally {
    migration.outcome = { ...outcome, revision: system.migration.data?.revision ?? null, at: Date.now() };
    migration.busy = false;
    reload("config", "telemetry", "overview");
  }
}
