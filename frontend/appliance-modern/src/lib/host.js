// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Readings of the host status the pages share: Docker, the Admin container,
 * the package updates and storage. Pure, derived from the backend payloads
 * alone, so the node tests can walk every state.
 */

/** The one reading of a daemon state: running, unavailable (not installed), stopped, or unknown when nothing was read. */
export function dockerCondition(daemonState) {
  if (!daemonState) return "unknown";
  if (daemonState === "running" || daemonState === "unavailable") return daemonState;
  return "stopped";
}

export function dockerTone(daemonState) {
  const condition = dockerCondition(daemonState);
  if (condition === "running") return "c-ok";
  if (condition === "unavailable") return "c-warn";
  return "c-fault";
}

export function daemonState(status) {
  return String((((status && status.docker) || {}).daemon || {}).state || "");
}

/** An unread daemon is not a stopped one. */
export function dockerStopped(status) {
  const condition = dockerCondition(daemonState(status));
  return condition === "stopped" || condition === "unavailable";
}

export function adminTransitionLive(status) {
  return ((((status && status.admin) || {}).transition || {}).state) === "live";
}

/**
 * Which Admin version actions this host can take now: unread, docker (the
 * daemon is down or absent), bootstrap (nothing deployed), missing (the
 * container is gone), live (Admin is replacing itself), ready.
 */
export function adminGate(status) {
  const admin = (status && status.admin) || {};
  if (admin.installed === undefined) return "unread";
  if (dockerStopped(status)) return "docker";
  if (admin.bootstrap_required) return "bootstrap";
  if (!admin.installed) return "missing";
  if (adminTransitionLive(status)) return "live";
  return "ready";
}

/** The installed Admin version, or why there is none: unknown, unknownVersion, notInstalled. */
export function adminVersion(status) {
  const admin = (status && status.admin) || {};
  if (admin.installed === undefined || (!admin.installed && dockerStopped(status))) return { key: "admin.version.unknown" };
  if (admin.version) return { text: admin.version };
  return { key: admin.installed ? "admin.version.unknownVersion" : "admin.version.notInstalled" };
}

/** healthy, unhealthy or missing, and its tone. */
export function adminHealth(admin) {
  const value = admin || {};
  if (!value.installed) return { key: "admin.health.missing", tone: "c-warn" };
  return value.healthy ? { key: "admin.health.healthy", tone: "c-ok" } : { key: "admin.health.unhealthy", tone: "c-fault" };
}

/** The configured EMS container, by the name the backend says is the EMS -- never by a name pattern. */
export function emsContainer(status) {
  const docker = (status && status.docker) || {};
  return (docker.containers || []).find((item) => item.name === docker.ems_container) || null;
}

/** The words and their order are the Admin console's, so a version reads the same on both surfaces. */
export const RELEASE_TRACKS = Object.freeze(["stable", "unstable", "experimental"]);

/** An entry without a known track is shown with the least trusted group. */
export function groupByTrack(items) {
  return RELEASE_TRACKS.map((track) => ({
    track,
    items: (items || []).filter((item) => (RELEASE_TRACKS.includes(item.track) ? item.track : "experimental") === track),
  })).filter((group) => group.items.length > 0);
}

/** Only an upgrade gets a button: an unknown direction may be the same image. */
export function latestVerdict(latest) {
  const direction = latest && latest.direction;
  if (direction === "upgrade") return { key: "updates.verdict.available", tone: "c-warn" };
  if (direction === "downgrade") return { key: "updates.verdict.newer", tone: "c-idle" };
  if (direction === "reinstall") return { key: "updates.verdict.current", tone: "c-ok" };
  return { key: "updates.verdict.uncomparable", tone: "c-idle" };
}

/** A failed check or a package manager needing recovery; a stale index is not one. */
export function updatesInDoubt(updates) {
  const value = updates || {};
  return value.error === "update_check_failed" || (value.package_manager || {}).healthy === false;
}

export function securityUpdatesOffered(updates) {
  return Number((updates || {}).security_count) > 0 && !updatesInDoubt(updates);
}

/**
 * The operating system's verdict: unknown, a finding that puts the counts in
 * doubt, security updates waiting, a stale index, or up to date.
 */
export function systemVerdict(updates, findings) {
  const value = updates || {};
  if (value.security_count === undefined) return { key: "updates.os.unknown", tone: "c-idle" };
  const ofUpdates = (findings || []).filter((item) => item.section === "updates" && item.code !== "security_updates_pending");
  const stale = ofUpdates.find((item) => item.code === "package_index_stale");
  if (updatesInDoubt(value)) {
    const doubt = ofUpdates.find((item) => item !== stale);
    return { finding: doubt || null, key: doubt ? null : "updates.os.doubt", tone: doubt && doubt.severity === "error" ? "c-fault" : "c-warn" };
  }
  if (Number(value.security_count) > 0) return { key: "updates.os.security", tone: "c-warn" };
  if (stale) return { finding: stale, tone: "c-warn" };
  if (Number(value.normal_count) > 0) return { key: "updates.os.noSecurity", tone: "c-ok" };
  return { key: "updates.os.current", tone: "c-ok" };
}

/** Days when the index is a day old or more, else hours; null when nobody knows. */
export function indexAge(updates) {
  const seconds = Number((updates || {}).index_age_seconds);
  if (!Number.isFinite(seconds) || seconds < 0) return null;
  const days = Math.floor(seconds / 86400);
  if (days >= 1) return { unit: "day", count: days };
  return { unit: "hour", count: Math.floor(seconds / 3600) };
}

/**
 * Megabytes are what the host reports and gigabytes are what a person reads:
 * whole gigabytes from 10 GB, one decimal below. Null for no reading.
 */
export function gigabytes(megabytes) {
  const value = Number(megabytes);
  if (!value || value < 0) return null;
  return value >= 10240 ? { value: Math.round(value / 1024), digits: 0 } : { value: value / 1024, digits: 1 };
}

/**
 * A session limit in the largest unit it divides into exactly: 5400 seconds
 * rounded to "2 hours" would hide half an hour of a session that ends at 90
 * minutes.
 */
export function exactDuration(seconds) {
  const total = Number(seconds);
  if (!total || total < 0 || total !== Math.floor(total)) return null;
  if (total >= 86400 && total % 86400 === 0) return { unit: "day", count: total / 86400 };
  if (total >= 3600 && total % 3600 === 0) return { unit: "hour", count: total / 3600 };
  if (total >= 60 && total % 60 === 0) return { unit: "minute", count: total / 60 };
  return { unit: "second", count: total };
}
