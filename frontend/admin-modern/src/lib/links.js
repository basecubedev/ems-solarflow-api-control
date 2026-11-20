// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The dashboard's address as this browser reaches it. The overview names it
 * as http://localhost:<port>, which is only true on the machine itself; the
 * browser already knows the host it used to reach this console, and the
 * dashboard is on the same host, so only the scheme and the port are taken.
 */
export function dashboardHref(dashboardUrl, location = globalThis.location) {
  if (typeof dashboardUrl !== "string" || !dashboardUrl || !location?.hostname) return null;
  let parsed;
  try {
    parsed = new URL(dashboardUrl);
  } catch {
    return null;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
  const host = location.hostname.includes(":") && !location.hostname.startsWith("[") ? `[${location.hostname}]` : location.hostname;
  const port = parsed.port ? `:${parsed.port}` : "";
  return `${parsed.protocol}//${host}${port}/`;
}

/**
 * The dashboard's pages are in this app; a dashboard the overview names is
 * opened here, at its overview, and none is offered while it names none.
 */
export function dashboardPage(dashboardUrl) {
  return dashboardHref(dashboardUrl, { hostname: "localhost" }) ? "#/overview" : null;
}
