// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The EMS Admin console runs on this host at the port appliance.conf names.
 * The link is built from the address this browser already reached the
 * appliance by, so it works by name and by IP alike; it is a link, never an
 * authority, and it is offered only while an Admin is installed.
 */
export function adminLink(location, settings, status) {
  const port = Number(settings && settings.admin_port);
  const installed = Boolean(status && status.admin && status.admin.installed);
  const hostname = location && location.hostname;
  if (!installed || !Number.isInteger(port) || port <= 0 || port > 65535 || !hostname) return "";
  return `http://${hostname}:${port}/`;
}
