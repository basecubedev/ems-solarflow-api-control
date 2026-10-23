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
});

/** The words for a vocabulary field's value, or null for data shown as it is. */
export function vocabularyWords(key, value) {
  if (!(key in VOCABULARY) || typeof value !== "string") return null;
  const known = VOCABULARY[key](value);
  return known ? { key: known } : { text: value.replace(/_/g, " "), lang: "en" };
}

/** Drawn in a block of their own below the facts. */
export const PLAN_SKIP_FIELDS = Object.freeze(["blockers", "findings", "packages", "keys", "bootstrap"]);

function rank(key) {
  const index = PLAN_FIELD_ORDER.indexOf(key);
  return index === -1 ? PLAN_FIELD_ORDER.length : index;
}

/** The scalar facts of a plan in reading order; objects and lists have blocks of their own. */
export function planFields(plan, expert) {
  return Object.keys(plan || {})
    .filter((key) => {
      if (PLAN_SKIP_FIELDS.includes(key)) return false;
      if (!expert && PLAN_EXPERT_FIELDS.includes(key)) return false;
      const value = plan[key];
      if (value === null || value === undefined || value === "") return false;
      return typeof value !== "object";
    })
    .sort((left, right) => rank(left) - rank(right))
    .map((key) => {
      const value = plan[key];
      return {
        key,
        known: PLAN_FIELD_ORDER.includes(key),
        label: key.replace(/_/g, " "),
        kind: typeof value === "boolean" ? "bool" : /_megabytes$/.test(key) ? "megabytes" : "text",
        value,
        mono: /digest|revision|reference|authority/.test(key),
      };
    });
}

export function planBlocked(plan) {
  return ((plan && plan.blockers) || []).length > 0;
}
