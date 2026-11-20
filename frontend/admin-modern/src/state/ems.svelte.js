// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import { getJson } from "../../../shared/lib/api.js";
import { emsRefusal } from "../lib/ems.js";
import { startLive } from "../../../dashboard-modern/src/state/live.svelte.js";
import {
  loadAuth as loadEmsAuth,
  login as emsLogin,
  logout as emsLogout,
  watchActivity as watchEmsActivity,
  watchSession as watchEmsSession,
} from "../../../dashboard-modern/src/state/auth.svelte.js";

export const EMS_CHECK_MS = 15000;

/**
 * Whether the EMS answers through this console: "checking" until the first
 * answer, "running", or the refusal that says why not. The dashboard's live
 * feed and its session start once it answers, and not before, so a console
 * without an EMS asks nothing of one.
 */
class Ems {
  state = $state("checking");
  answered = $state(false);
}

export const ems = new Ems();

let started = false;
let timer = null;

export async function checkEms() {
  try {
    await getJson("/api/ui-config");
    ems.state = "running";
    ems.answered = true;
    if (started) return;
    started = true;
    startLive();
    loadEmsAuth();
    watchEmsSession();
    watchEmsActivity();
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

/**
 * One sign-in for both: the console's password is the EMS's, so the EMS is
 * signed in with it too. Each server checks it and keeps its own session; an
 * EMS that does not answer now asks for it again on its own pages.
 */
export async function signInToEms(password) {
  try {
    await emsLogin(password);
  } catch {
    await loadEmsAuth();
  }
}

export async function signOutOfEms() {
  try {
    await emsLogout();
  } catch {
    await loadEmsAuth();
  }
}
