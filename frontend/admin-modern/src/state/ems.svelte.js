// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import { getJson } from "../../../shared/lib/api.js";
import { emsRefusal } from "../lib/ems.js";
import { startLive } from "../../../dashboard-modern/src/state/live.svelte.js";

export const EMS_CHECK_MS = 15000;

/**
 * Whether the EMS answers through this console: "checking" until the first
 * answer, "running", or the refusal that says why not. The dashboard's live
 * feed starts once it answers, and not before, so a console without an EMS
 * asks nothing of one. Its session is the console's (``App.svelte``), which the
 * EMS takes only as ``console_operator`` in its /api/ui-config says: an EMS
 * older than this console does not name it ("older"), and one this console
 * reaches without its socket file, or that cannot read the console's key,
 * names it false ("unlinked"). Its pages then read, but nothing on them
 * needing a sign-in works.
 */
class Ems {
  state = $state("checking");
  answered = $state(false);
  vouch = $state("linked");
}

export const ems = new Ems();

let started = false;
let timer = null;

export async function checkEms() {
  try {
    const config = await getJson("/api/ui-config");
    ems.vouch = config?.console_operator === true ? "linked" : config && "console_operator" in config ? "unlinked" : "older";
    ems.state = "running";
    ems.answered = true;
    if (started) return;
    started = true;
    startLive();
  } catch (error) {
    ems.state = emsRefusal(error);
  }
}

export function watchEms() {
  if (timer) return;
  checkEms();
  timer = setInterval(() => {
    if (!document.hidden) checkEms();
  }, EMS_CHECK_MS);
}
