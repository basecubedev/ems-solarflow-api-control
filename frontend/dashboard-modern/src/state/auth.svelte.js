// SPDX-License-Identifier: AGPL-3.0-or-later
import { ApiError, getJson, sendJson } from "../../../shared/lib/api.js";

export const HEARTBEAT_MIN_INTERVAL_MS = 60000;
export const SESSION_CHECK_INTERVAL_MS = 60000;

/**
 * The browser's view of the dashboard session. The server is the authority:
 * this only mirrors /api/auth/status, and every write is still checked there.
 */
class Auth {
  loaded = $state(false);
  configured = $state(false);
  authenticated = $state(false);
  csrfToken = $state(null);
  loginOpen = $state(false);
}

export const auth = new Auth();

let lastHeartbeat = 0;

function adopt(payload) {
  auth.configured = Boolean(payload?.auth_configured);
  auth.authenticated = Boolean(payload?.authenticated);
  auth.csrfToken = auth.authenticated ? payload?.csrf_token || auth.csrfToken : null;
  auth.loaded = true;
}

export async function loadAuth() {
  try {
    adopt(await getJson("/api/auth/status"));
  } catch {
    auth.configured = false;
    auth.authenticated = false;
    auth.csrfToken = null;
    auth.loaded = true;
  }
}

export async function login(password) {
  const payload = await sendJson("/api/auth/login", { body: { password } });
  auth.configured = Boolean(payload?.auth_configured ?? true);
  auth.authenticated = true;
  auth.csrfToken = payload?.csrf_token || null;
  auth.loaded = true;
  return payload;
}

export async function logout() {
  await sendJson("/api/auth/logout", {});
  auth.authenticated = false;
  auth.csrfToken = null;
}

/** A session slides only on genuine interaction, at most once a minute, never from background polling. */
export async function heartbeat() {
  if (!auth.authenticated || !auth.csrfToken) return;
  const now = Date.now();
  if (now - lastHeartbeat < HEARTBEAT_MIN_INTERVAL_MS) return;
  lastHeartbeat = now;
  try {
    await sendJson("/api/auth/refresh", { csrfToken: auth.csrfToken });
  } catch (error) {
    if (error instanceof ApiError && (error.status === 401 || error.status === 403)) await loadAuth();
  }
}

/** Classic's minute check: a session that ended on a screen nobody touches still leaves the operator pages. */
export function watchSession() {
  setInterval(loadAuth, SESSION_CHECK_INTERVAL_MS);
}

/** An operator read that comes back 401 means the session is gone, as for a write. */
export async function operatorGet(path, options = {}) {
  try {
    return await getJson(path, options);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) await loadAuth();
    throw error;
  }
}

export function watchActivity() {
  const onActivity = () => {
    if (!document.hidden) heartbeat();
  };
  ["mousemove", "keydown", "click"].forEach((name) => document.addEventListener(name, onActivity, { passive: true }));
  document.addEventListener("visibilitychange", onActivity);
}

/** A write that comes back 401/403 means the session is gone; the UI then shows the sign-in state again. */
export async function authorizedSend(path, options = {}) {
  try {
    return await sendJson(path, { ...options, csrfToken: auth.csrfToken });
  } catch (error) {
    if (error instanceof ApiError && (error.status === 401 || error.code === "csrf_failed")) await loadAuth();
    throw error;
  }
}
