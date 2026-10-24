// SPDX-License-Identifier: AGPL-3.0-or-later
import { ApiError, getJson, sendJson } from "../../../shared/lib/api.js";

export const CSRF_HEADER = "X-Appliance-CSRF";
export const UNREACHABLE_RETRY_MS = 5000;

/**
 * The browser's view of the appliance session. The server is the authority:
 * this mirrors /api/session, and every request is still checked there. The
 * whole console sits behind it; nothing but the sign-in is reachable without
 * a session.
 */
class Session {
  loaded = $state(false);
  reachable = $state(true);
  authenticated = $state(false);
  configured = $state(false);
  fileMissing = $state(false);
  confirmed = $state(null);
  csrf = $state("");
  audit = $state.raw(null);
  version = $state("");
  notice = $state("");
}

export const session = new Session();

let retryTimer = null;
let request = 0;
let epoch = 0;

/** password_confirmed absent means the agent could not be asked, which is not a "no". */
function confirmedOf(payload) {
  return payload && payload.password_confirmed !== undefined ? payload.password_confirmed : null;
}

function adopt(payload) {
  if (Boolean(payload.authenticated) !== session.authenticated) epoch += 1;
  session.authenticated = Boolean(payload.authenticated);
  session.configured = Boolean(payload.password_configured);
  session.fileMissing = Boolean(payload.password_file_missing);
  session.confirmed = confirmedOf(payload);
  session.csrf = session.authenticated ? payload.csrf_token || "" : "";
  session.audit = payload.security_audit || null;
  if (payload.appliance_version) session.version = String(payload.appliance_version);
  session.reachable = true;
  session.loaded = true;
}

function retryLater() {
  if (retryTimer) return;
  retryTimer = setTimeout(() => {
    retryTimer = null;
    loadSession();
  }, UNREACHABLE_RETRY_MS);
}

export async function loadSession() {
  request += 1;
  const mine = request;
  try {
    const payload = await getJson("/api/session");
    if (mine === request) adopt(payload || {});
  } catch {
    if (mine !== request) return;
    session.reachable = false;
    session.loaded = true;
    retryLater();
  }
}

/** The first password, created here during the first start; the Admin console and the dashboard share it. */
export async function createPassword(password, confirmation) {
  request += 1;
  const payload = await sendJson("/api/session/setup", { body: { password, confirmation } });
  adopt({ ...payload, authenticated: true, password_configured: true });
  session.notice = "";
}

export async function login(password) {
  request += 1;
  const payload = await sendJson("/api/session/login", { body: { password } });
  adopt({ ...payload, authenticated: true, password_configured: true });
  session.notice = "";
}

/** The session is gone: the sign-in shows again, with the reason when there is one to give. */
export function sessionEnded(noticeKey = "") {
  request += 1;
  epoch += 1;
  session.authenticated = false;
  session.csrf = "";
  session.notice = noticeKey;
}

/**
 * Signing out has to be proven: a failed request leaves the session as it
 * was, unless the status then shows it ended anyway.
 */
export async function logout() {
  try {
    await sendJson("/api/session/logout", { body: {}, headers: { [CSRF_HEADER]: session.csrf } });
  } catch (error) {
    await loadSession();
    if (session.authenticated) throw error;
  }
  sessionEnded();
}

function lost(error) {
  return error instanceof ApiError && error.status === 401;
}

/** A read; a 401 means the session ended, and the sign-in shows again. */
export async function applianceGet(path) {
  const mine = epoch;
  try {
    return await getJson(path);
  } catch (error) {
    if (lost(error) && session.authenticated && mine === epoch) sessionEnded("signIn.expired");
    throw error;
  }
}

/** Every write carries the session's CSRF token; the server decides whether it is allowed. */
export async function applianceSend(path, { body = {}, account } = {}) {
  const headers = { [CSRF_HEADER]: session.csrf };
  if (account) headers["X-Appliance-Account"] = account;
  const mine = epoch;
  try {
    return await sendJson(path, { body, headers });
  } catch (error) {
    if (lost(error) && session.authenticated && mine === epoch) sessionEnded("signIn.expired");
    throw error;
  }
}
