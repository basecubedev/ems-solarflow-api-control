// SPDX-License-Identifier: AGPL-3.0-or-later
import { ApiError, getJson, sendJson } from "../../../shared/lib/api.js";

export const HEARTBEAT_MIN_INTERVAL_MS = 60000;
export const SESSION_CHECK_INTERVAL_MS = 60000;
export const RECONNECT_INTERVAL_MS = 5000;

/** The auth failures that mean "this browser has no usable session". */
export const SESSION_ERRORS = Object.freeze(["not_authenticated", "auth_not_configured", "csrf_failed", "auth_file_invalid", "config_unreadable"]);

/**
 * The browser's view of the Admin session. The server is the authority: this
 * mirrors /api/admin/auth/status, and every request is still checked there.
 * The whole console sits behind it; nothing but the sign-in is reachable
 * without a session.
 */
class Auth {
  loaded = $state(false);
  reachable = $state(true);
  configured = $state(false);
  authenticated = $state(false);
  requiresInitialPassword = $state(false);
  recoveryRequired = $state(false);
  recoveryError = $state("");
  recoveryMessage = $state("");
  csrfToken = $state(null);
  instanceId = $state(null);
  notice = $state("");
}

export const auth = new Auth();

let lastHeartbeat = 0;
let reconnectTimer = null;
let authRequest = 0;
let actions = 0;
let actionSeq = 0;
let replacing = false;

/** Whether the page is being reloaded from a new Admin process; nothing it followed survives that process anyway. */
export function replacingPage() {
  return replacing;
}

/**
 * A different instance id means another Admin process answers now: an
 * update or a restart replaced the one this page was loaded from, and the
 * page has to come from it as well.
 */
function adopt(payload) {
  const instance = typeof payload?.admin_instance_id === "string" ? payload.admin_instance_id : null;
  if (auth.instanceId && instance && instance !== auth.instanceId) {
    replacing = true;
    globalThis.location.reload();
    return;
  }
  if (instance) auth.instanceId = instance;
  auth.configured = Boolean(payload?.auth_configured);
  auth.authenticated = Boolean(payload?.authenticated);
  auth.requiresInitialPassword = Boolean(payload?.requires_initial_password);
  auth.recoveryRequired = Boolean(payload?.recovery_required);
  auth.recoveryError = auth.recoveryRequired ? String(payload?.error || "") : "";
  auth.recoveryMessage = auth.recoveryRequired && typeof payload?.message === "string" ? payload.message : "";
  auth.csrfToken = auth.authenticated ? payload?.csrf_token || null : null;
  auth.reachable = true;
  auth.loaded = true;
}

/**
 * Status reads overlap sign-ins, sign-outs and refreshes (the minute check, a
 * tab coming back, a 401 elsewhere). A status read the server answered before
 * the action took effect can arrive after it, so status answers are dropped
 * while an action is in flight and any read started before it settled is
 * stale: the action's own answer is the one adopted. Of two actions in flight
 * (a heartbeat from the click that signs out), only the later one's answer is.
 */
async function action(request) {
  actions += 1;
  authRequest += 1;
  actionSeq += 1;
  const mine = actionSeq;
  try {
    const payload = await request();
    if (mine === actionSeq) adopt(payload);
  } finally {
    actions -= 1;
    authRequest += 1;
  }
}

function retryLater() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    loadAuth();
  }, RECONNECT_INTERVAL_MS);
}

export async function loadAuth() {
  authRequest += 1;
  const mine = authRequest;
  try {
    const payload = await getJson("/api/admin/auth/status");
    if (mine === authRequest && actions === 0) adopt(payload);
  } catch (error) {
    if (mine !== authRequest || actions > 0) return;
    if (error instanceof ApiError && error.status < 500) {
      adopt(error.payload);
      return;
    }
    auth.reachable = false;
    auth.loaded = true;
    retryLater();
  }
}

export async function createPassword(password, confirm) {
  await action(() => sendJson("/api/admin/auth/setup", { body: { password, confirm_password: confirm } }));
  auth.notice = "";
}

export async function login(password) {
  await action(() => sendJson("/api/admin/auth/login", { body: { password } }));
  auth.notice = "";
}

/**
 * Signing out has to be proven: a failed request leaves the session as it
 * was, unless the status then shows it ended anyway (the answer was lost, the
 * sign-out was not). A chosen sign-out explains itself.
 */
export async function logout() {
  try {
    await action(() => sendJson("/api/admin/auth/logout"));
  } catch (error) {
    await loadAuth();
    if (auth.authenticated) throw error;
  }
  auth.notice = "";
}

/**
 * What the sign-in should explain if the session ends while a step runs that
 * can end it -- a restore that puts back an older password file. Set before
 * the step, cleared after it; a sign-in clears it as well.
 */
export function expectSessionEnd(key) {
  auth.notice = key || "";
}

/** A session slides only on genuine interaction, at most once a minute, never from background polling. */
export async function heartbeat() {
  if (!auth.authenticated || !auth.csrfToken) return;
  const now = Date.now();
  if (now - lastHeartbeat < HEARTBEAT_MIN_INTERVAL_MS) return;
  lastHeartbeat = now;
  try {
    await action(() => sendJson("/api/admin/auth/refresh", { csrfToken: auth.csrfToken }));
  } catch (error) {
    if (error instanceof ApiError) await loadAuth();
  }
}

export function watchSession() {
  setInterval(loadAuth, SESSION_CHECK_INTERVAL_MS);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) loadAuth();
  });
}

export function watchActivity() {
  const onActivity = () => {
    if (!document.hidden) heartbeat();
  };
  ["pointerdown", "keydown"].forEach((name) => document.addEventListener(name, onActivity, { passive: true }));
}

/** A failure that means this browser's session is gone, not that the request was wrong. */
export function sessionLost(error) {
  return error instanceof ApiError && (error.status === 401 || SESSION_ERRORS.includes(error.code));
}

/** A read that comes back 401 means the session is gone; the sign-in shows again. */
export async function adminGet(path, options = {}) {
  try {
    return await getJson(path, options);
  } catch (error) {
    if (sessionLost(error)) await loadAuth();
    throw error;
  }
}

/** Every write carries the session's CSRF token; the server decides whether it is allowed. */
export async function adminSend(path, options = {}) {
  try {
    return await sendJson(path, { ...options, csrfToken: auth.csrfToken });
  } catch (error) {
    if (sessionLost(error)) await loadAuth();
    throw error;
  }
}

/** A file from the server, behind the session and the CSRF token, as a blob. */
export async function adminDownload(path, body) {
  const response = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { Accept: "application/octet-stream, application/json", "Content-Type": "application/json", "X-CSRF-Token": auth.csrfToken || "" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    const error = new ApiError(response.status, payload);
    if (sessionLost(error)) await loadAuth();
    throw error;
  }
  return response.blob();
}

/** A file to the server, sent as it is; its name travels in a header. */
export async function adminUpload(path, file) {
  const response = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { Accept: "application/json", "Content-Type": "application/octet-stream", "X-Backup-Filename": file.name, "X-CSRF-Token": auth.csrfToken || "" },
    body: file,
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok || payload?.ok !== true) {
    const error = new ApiError(response.ok ? 400 : response.status, payload);
    if (sessionLost(error)) await loadAuth();
    throw error;
  }
  return payload;
}
