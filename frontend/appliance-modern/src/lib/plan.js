// SPDX-License-Identifier: AGPL-3.0-or-later
import { CONTAINER_STATES } from "./words.js";
import { MANAGER_DIRECTIONS } from "./manager.js";
import { INSTALL_CHANNELS, LIFECYCLE_ACTIONS, REPAIR_ACTIONS } from "./plans.js";

/**
 * The plan an owner agrees to before this appliance is changed, read as named
 * facts rather than as the payload's own keys. A key this build has no name for
 * still appears, spelled out: a field worth planning is worth showing badly
 * rather than hiding.
 */

/** What is about to happen comes before which image it happens with (Classic's PLAN_FIELD_ORDER). */
export const PLAN_FIELD_ORDER = Object.freeze([
  "type",
  "action",
  "scope",
  "direction",
  "container",
  "current_state",
  "current_version",
  "target_tag",
  "new_version",
  "kept_version",
  "target_channel",
  "reinstall",
  "hostname",
  "ssid",
  "security_count",
  "normal_count",
  "package_count",
  "reboot_required",
  "reboot_required_before",
  "minimum_free_megabytes",
  "free_megabytes",
  "lock_state",
  "healthy",
  "repository",
  "target_reference",
  "target_digest",
  "current_digest",
  "target_revision",
  "target_source",
  "target_architecture",
  "legacy_labels_accepted",
  "image_available_locally",
  "authority",
  "authority_plan",
]);

/**
 * Fields Classic shows under their raw key that Modern names; they read after
 * Classic's order. tests/test_appliance_modern.py holds the agent's plans to
 * this list, so a new field is named before an owner meets it.
 */
export const PLAN_EXTRA_FIELDS = Object.freeze([
  "free_space_known",
  "version",
  "installed_version",
  "size_bytes",
  "revert_available",
  "state_compatibility_known",
  "verify_window_seconds",
  "release_id",
  "build_id",
  "digest",
  "account",
  "enabled",
  "currently_enabled",
  "key_count",
  "note",
  "hidden",
  "security",
  "signal",
  "previous_profile",
  "revert_timeout_seconds",
  "previous_hostname",
  "new_url",
  "admin_url",
  "timezone",
  "previous_timezone",
  "destination",
]);

/** The fingerprint seals the plan; a mechanism, not a statement about this appliance, so it sits with the digests. */
export const PLAN_EXPERT_FIELDS = Object.freeze([
  "authority",
  "authority_plan",
  "target_digest",
  "target_architecture",
  "current_digest",
  "legacy_labels_accepted",
  "target_reference",
  "target_source",
]);

/** Of the fields Modern names, those only Expert shows: identities rather than facts an owner decides on. */
export const PLAN_EXTRA_EXPERT_FIELDS = Object.freeze(["release_id", "build_id", "digest"]);

/** The update scopes a package plan names (appliance/packages.py). */
export const PLAN_SCOPES = Object.freeze(["security", "all"]);

/**
 * Fields whose value is a word of the appliance's vocabulary rather than data
 * such as a version or a name: translated where this build has the words,
 * otherwise kept as the appliance's English.
 */
const VOCABULARY = Object.freeze({
  action: (value) => (LIFECYCLE_ACTIONS.includes(value) ? `admin.${value}` : REPAIR_ACTIONS.includes(value) ? `repair.${value}` : null),
  scope: (value) => (PLAN_SCOPES.includes(value) ? `plan.scope.${value}` : null),
  direction: (value) => (MANAGER_DIRECTIONS.includes(value) ? `manager.direction.${value}` : null),
  current_state: (value) => (CONTAINER_STATES.includes(value) ? `container.${value}` : null),
  target_channel: (value) => (INSTALL_CHANNELS.includes(value) ? `install.channel.${value}` : null),
  lock_state: () => null,
  security: () => null,
  note: () => null,
});

/** The words for a vocabulary field's value, or null for data shown as it is. */
export function vocabularyWords(key, value) {
  if (!(key in VOCABULARY) || typeof value !== "string") return null;
  const known = VOCABULARY[key](value);
  return known ? { key: known } : { text: value.replace(/_/g, " "), lang: "en" };
}

/** Drawn in a block of their own below the facts. */
export const PLAN_SKIP_FIELDS = Object.freeze(["blockers", "findings", "packages", "keys", "bootstrap", "warning"]);

const NAMED_FIELDS = Object.freeze([...PLAN_FIELD_ORDER, ...PLAN_EXTRA_FIELDS]);

function rank(key) {
  const index = NAMED_FIELDS.indexOf(key);
  return index === -1 ? NAMED_FIELDS.length : index;
}

function kindOf(key, value) {
  if (typeof value === "boolean") return "bool";
  if (/_megabytes$/.test(key)) return "megabytes";
  if (/_bytes$/.test(key)) return "bytes";
  if (/_seconds$/.test(key)) return "seconds";
  return key === "signal" ? "percent" : "text";
}

/**
 * The scalar facts of a plan in reading order; objects and lists have blocks
 * of their own. Free space the host could not measure is said as such, never
 * as the 0 MB the agent sends in its place, and a measured one needs no word.
 */
export function planFields(plan, expert) {
  return Object.keys(plan || {})
    .filter((key) => {
      if (PLAN_SKIP_FIELDS.includes(key)) return false;
      if (key === "free_space_known") return plan[key] === false;
      if (key === "free_megabytes" && plan.free_space_known === false) return false;
      if (!expert && (PLAN_EXPERT_FIELDS.includes(key) || PLAN_EXTRA_EXPERT_FIELDS.includes(key))) return false;
      const value = plan[key];
      if (value === null || value === undefined || value === "") return false;
      return typeof value !== "object";
    })
    .sort((left, right) => rank(left) - rank(right))
    .map((key) => {
      const value = plan[key];
      return {
        key,
        known: NAMED_FIELDS.includes(key),
        label: key.replace(/_/g, " "),
        kind: kindOf(key, value),
        value,
        mono: /digest|revision|reference|authority|_url$/.test(key),
      };
    });
}

export function planBlocked(plan) {
  return ((plan && plan.blockers) || []).length > 0;
}
