// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The Appliance Manager package: which versions the signed index offers, and
 * whether an install or a revert may start now. Every decision is derived from
 * the backend payload and the appliance's own clock alone.
 */

/** How an offered package relates to the installed one (manager_releases directions). */
export const MANAGER_DIRECTIONS = Object.freeze(["upgrade", "downgrade", "reinstall", "unknown", "revert"]);

/** How the last install of a package ended (appliance/manager_install.py OUTCOME_*). */
export const MANAGER_OUTCOMES = Object.freeze(["installed", "reverted", "pending"]);

/** What the reverter decided about the last install, and its tone. */
export const MANAGER_VERDICTS = Object.freeze({
  confirmed: "c-ok",
  reverted: "c-warn",
  revert_failed: "c-fault",
  revert_unavailable: "c-fault",
});

/**
 * Whether an install or a revert may start. A deadline in flight blocks both:
 * a second install would replace the package whose verdict the appliance is
 * still waiting for. In flight means the window has not passed by the
 * appliance's clock; an unreadable clock keeps the lock, because a deadline
 * this cannot place may still fire. A record this manager cannot read is held
 * closed too: the outgoing package's reverter may still judge it.
 */
export function managerActions(manager, now) {
  const verify = (manager || {}).verify || {};
  const armed = verify.armed === true;
  const expiry = Number(verify.deadline_epoch) || 0;
  const clock = Number(now) || 0;
  const inFlight = armed && !(clock && expiry && clock >= expiry);
  const unreadable = !armed && Boolean(verify.unreadable);
  return {
    armed,
    inFlight,
    expiredUnjudged: armed && !inFlight,
    unreadable,
    canUpdate: !inFlight && !unreadable,
    canRevert: (manager || {}).can_revert === true && !inFlight && !unreadable,
  };
}

/** The appliance's own clock; the browser's belongs to another machine, and the board has no real-time clock. */
export function applianceNow(status) {
  return Number((((status && status.system) || {}).time || {}).epoch) || 0;
}

/**
 * When a release was built, to the minute in this browser's time, so two builds
 * on one day are told apart. A time without a zone is the index's UTC; a value
 * without a time of day keeps its date alone.
 */
export function releaseStamp(value) {
  const text = String(value || "");
  const zoned = /(Z|[+-]\d{2}:?\d{2})$/.test(text);
  const parsed = new Date(zoned ? text : `${text}Z`);
  if (!/T\d{2}:\d{2}/.test(text) || Number.isNaN(parsed.getTime())) return text.slice(0, 10);
  const pad = (part) => String(part).padStart(2, "0");
  return `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())} ${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`;
}

/**
 * The parts of one index entry's label: version, build time, direction. Two
 * builds of one version within the same minute read the same, so only then is
 * the release id added.
 */
export function managerOptions(releases) {
  const entries = (releases || []).map((entry) => {
    const described = entry.described || entry;
    const version = described.release_version || "";
    return {
      entry,
      id: entry.release_id,
      version,
      stamp: version ? releaseStamp(described.created_at) : "",
      direction: MANAGER_DIRECTIONS.includes(entry.direction) ? entry.direction : "",
    };
  });
  const counts = {};
  entries.forEach((item) => {
    const signature = item.version ? `${item.version}|${item.stamp}|${item.direction}` : item.id;
    item.signature = signature;
    counts[signature] = (counts[signature] || 0) + 1;
  });
  return entries.map((item) => ({ ...item, ambiguous: Boolean(item.version) && counts[item.signature] > 1 }));
}

/** The latest stable package's version, when the index offers one. */
export function latestStableVersion(sources) {
  const latest = sources && sources.latest_stable;
  return latest ? (latest.described || {}).release_version || null : null;
}

/** An older or uncomparable package is a deliberate step back: the plan asks in the danger colour. */
export function riskyDirection(entry) {
  return entry && (entry.direction === "downgrade" || entry.direction === "unknown");
}
