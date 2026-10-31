// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Backups as admin/backup_restore.py describes them, in the reader's words.
 * The server owns what a backup holds and what a restore does; this names
 * its codes. A step or an action this build does not know keeps the server's
 * own label rather than disappearing.
 */

/** What a new backup holds. "system" is all three; analytics only where the bundled InfluxDB runs. */
export const CREATE_SCOPES = Object.freeze(["system", "config", "databases", "influxdb"]);
export const BACKUP_TYPES = Object.freeze(["config", "databases", "influxdb", "system"]);
export const CREATE_STEPS = Object.freeze([
  "preflight",
  "create_config",
  "verify_config",
  "create_databases",
  "verify_databases",
  "create_influxdb",
  "verify_influxdb",
  "write_set",
  "refresh",
]);
export const RESTORE_STEPS = Object.freeze(["verify_plan", "rollback", "apply", "postcheck", "broker", "done"]);
/** Restore steps that repeat once per archive of a set, so each names its archive. */
const TYPED_STEPS = Object.freeze(["rollback", "apply", "postcheck"]);
export const FILE_ACTIONS = Object.freeze([
  "would_restore_new",
  "would_replace_conflict",
  "would_skip_identical",
  "would_skip_outside_project",
  "would_keep_current",
  "would_skip_other_key",
  "conflict_requires_policy",
  "would_restore_influxdb",
  "checksum_invalid",
]);
export const STEP_STATES = Object.freeze({ done: "c-ok", running: "c-acc", pending: "c-idle", skipped: "c-idle", failed: "c-fault" });
export const STEP_ICONS = Object.freeze({ done: "circle-check", running: "loader", pending: "circle", skipped: "circle-minus", failed: "circle-x" });

/** Why the analytics data cannot be backed up here (ems/backup.py, the influxdb block's "reason"). */
export const INFLUX_REASONS = Object.freeze(["disabled", "external"]);

export function scopeAvailable(scope, listing) {
  return scope !== "influxdb" || Boolean(listing?.influxdb?.supported);
}

/** A step of a create or a restore job; ``targets`` names the archives a restore works through. */
export function stepView(step, targets = []) {
  const key = String(step?.key || "");
  const state = Object.hasOwn(STEP_STATES, step?.state) ? step.state : "pending";
  const base = key.replace(/_\d+$/, "");
  const index = key === base ? 0 : Number(key.slice(base.length + 1));
  const type = targets[index]?.backup_type;
  const known = CREATE_STEPS.includes(key) || RESTORE_STEPS.includes(base);
  const typed = Boolean(type) && TYPED_STEPS.includes(base);
  return {
    key,
    label: known ? `backup.step.${base}${typed ? ".typed" : ""}` : null,
    text: known ? "" : String(step?.label || key),
    type: BACKUP_TYPES.includes(type) ? type : null,
    state,
    tone: STEP_STATES[state],
    icon: STEP_ICONS[state],
    detail: typeof step?.message === "string" ? step.message : "",
  };
}

export function fileActionKey(action) {
  return FILE_ACTIONS.includes(action) ? `backup.file.${action}` : null;
}

/** The newest backup, or null when there is none. */
export function latestBackup(listing) {
  const stamp = listing?.summary?.latest_created_at;
  return typeof stamp === "string" && stamp ? stamp : null;
}

/** An archive the server says cannot be read is never offered for a restore. */
export function archiveView(backup) {
  const type = BACKUP_TYPES.includes(backup?.backup_type) ? backup.backup_type : null;
  return {
    id: String(backup?.id || ""),
    name: String(backup?.name || backup?.id || ""),
    type,
    typeText: type ? null : String(backup?.backup_type || ""),
    createdAt: backup?.created_at || backup?.mtime || null,
    size: Number(backup?.size_bytes) || 0,
    version: backup?.source_version || backup?.source_build || backup?.source_commit || null,
    encrypted: Boolean(backup?.encrypted),
    locked: Boolean(backup?.locked),
    valid: backup?.valid !== false,
    problem: backup?.valid === false ? String(backup?.error || "") : "",
  };
}

export function setView(set) {
  const members = Array.isArray(set?.archives) ? set.archives : [];
  return {
    id: String(set?.id || ""),
    name: String(set?.label || set?.id || ""),
    createdAt: set?.created_at || null,
    status: String(set?.status || ""),
    members: members.map((member) => ({ type: BACKUP_TYPES.includes(member?.type) ? member.type : null, typeText: String(member?.type || ""), present: member?.present !== false })),
    complete: members.length > 0 && members.every((member) => member?.present !== false) && set?.status !== "partial",
    missing: members.some((member) => member?.present === false),
    partial: set?.status === "partial",
    restorable: members.some((member) => member?.present !== false),
  };
}

/** Why a backup was made (ems/backup.py BACKUP_PURPOSES). */
export const BACKUP_PURPOSES = Object.freeze(["manual", "auto", "rollback"]);

/**
 * Newest first by creation time, whatever order the server lists them in (it
 * sorts by file name, sets apart from archives). Anything without a readable
 * time goes last, in the given order, so the top of the list is always the
 * latest backup.
 */
export function newestFirst(items) {
  const at = (item) => {
    const value = Date.parse(item?.createdAt || "");
    return Number.isNaN(value) ? null : value;
  };
  return items
    .map((item, index) => ({ item, index, at: at(item) }))
    .sort((a, b) => {
      if (a.at === null || b.at === null) return (a.at === null) - (b.at === null) || a.index - b.index;
      return b.at - a.at || a.index - b.index;
    })
    .map(({ item }) => item);
}

/**
 * The backup list's fixed warnings (admin/backup_restore.py) in the reader's
 * language; tests/test_admin_modern.py holds each sentence to that source.
 */
export const LIST_WARNINGS = Object.freeze({
  "The backup directory is outside the EMS install root.": "backups.unsafeLocation",
  "Some backups are encrypted and locked until unlocked.": "backups.warning.locked",
  "Some archives could not be read and are marked invalid.": "backups.warning.invalid",
});

/** A list warning as words: a translation key where the sentence is known, else the server's English. */
export function listWarningView(warning) {
  const text = String(warning || "").trim();
  if (!text) return null;
  return { key: Object.hasOwn(LIST_WARNINGS, text) ? LIST_WARNINGS[text] : null, text };
}
