// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * HTTP helpers for the EMS and Admin APIs. Writes carry the session's CSRF token;
 * the server decides whether a write is allowed, never this file.
 */

export class ApiError extends Error {
  constructor(status, payload) {
    super((payload && (payload.message || payload.error)) || `HTTP ${status}`);
    this.status = status;
    this.payload = payload || {};
    this.code = (payload && payload.error) || `http_${status}`;
  }
}

async function parse(response) {
  const type = response.headers.get("Content-Type") || "";
  if (!type.includes("application/json")) return null;
  try {
    return await response.json();
  } catch {
    return null;
  }
}

export async function getJson(path, { signal } = {}) {
  const response = await fetch(path, { credentials: "same-origin", signal, headers: { Accept: "application/json" } });
  const payload = await parse(response);
  if (!response.ok) throw new ApiError(response.status, payload);
  return payload;
}

export async function sendJson(path, { method = "POST", body, csrfToken, signal } = {}) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (csrfToken) headers["X-CSRF-Token"] = csrfToken;
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers,
    signal,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = await parse(response);
  if (!response.ok) throw new ApiError(response.status, payload);
  return payload;
}

export function query(params) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, val]) => {
    if (val === undefined || val === null || val === "") return;
    search.set(key, Array.isArray(val) ? val.join(",") : String(val));
  });
  const text = search.toString();
  return text ? `?${text}` : "";
}
