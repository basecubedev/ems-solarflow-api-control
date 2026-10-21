// SPDX-License-Identifier: AGPL-3.0-or-later
import { adminSend, auth, sessionLost } from "./auth.svelte.js";

/**
 * The last run of the EMS checks. It belongs to the browser tab, not to the
 * page: leaving Diagnostics while it runs and coming back finds it running,
 * and its result stays until the next run.
 */
class Diagnostics {
  result = $state.raw(null);
  at = $state(0);
  running = $state(false);
  error = $state.raw(null);
}

export const diagnostics = new Diagnostics();

/** A run the ended session refused is no result to keep once signed in again. */
export function forgetSessionError() {
  if (sessionLost(diagnostics.error)) diagnostics.error = null;
}

export async function runChecks() {
  if (diagnostics.running) return;
  diagnostics.running = true;
  diagnostics.error = null;
  try {
    diagnostics.result = await adminSend("/api/admin/maintenance/diagnostics/run");
    diagnostics.at = Date.now();
  } catch (error) {
    if (!(sessionLost(error) && !auth.authenticated)) diagnostics.error = error;
  } finally {
    diagnostics.running = false;
  }
}
