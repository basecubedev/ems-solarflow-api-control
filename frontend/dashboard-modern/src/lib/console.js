// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import { DOCS_SITE } from "../../../shared/lib/about.js";

/**
 * The Admin console, the product's one address, as the EMS's own dashboard
 * names it (``admin_console`` in /api/ui-config, written by the console). The
 * host name is the one this browser used, so only the ports come from the
 * EMS. Pure.
 */
export const REDIRECT_SECONDS = 5;
export const INSTALL_CONSOLE = `${DOCS_SITE}/user/admin-console.html`;

function port(value) {
  return Number.isInteger(value) && value >= 1 && value <= 65535 ? value : null;
}

/**
 * Where this page continues in the console: the same page (the address's
 * ``#/<page>``), on the console's port for the scheme this browser uses. A
 * console that has no listener for that scheme is offered over the one it
 * has, and never opened by itself, since that would change the scheme under
 * the reader.
 */
export function consoleTarget(record, location = globalThis.location) {
  if (!record || typeof record !== "object" || !location?.hostname) return null;
  const secure = location.protocol === "https:";
  const same = port(secure ? record.https_port : record.port);
  const other = port(secure ? record.port : record.https_port);
  if (!same && !other) return null;
  const scheme = same ? location.protocol : secure ? "http:" : "https:";
  const host = location.hostname.includes(":") && !location.hostname.startsWith("[") ? `[${location.hostname}]` : location.hostname;
  const hash = /^#\/[a-z]+$/.test(location.hash || "") ? location.hash : "";
  return { href: `${scheme}//${host}:${same || other}/${hash}`, automatic: Boolean(same) };
}
