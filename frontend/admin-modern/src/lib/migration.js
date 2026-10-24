// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The review of an older Zendure MQTT setup (admin/zendure_mqtt_migration_review.py)
 * as the page shows it. The apply presents the review's revision, so a review
 * the config has moved past is refused by the server rather than applied.
 */

const text = (value) => (value === null || value === undefined ? "" : String(value));

export function migrationReview(data) {
  if (data?.status !== "ok") return null;
  const review = data.review && typeof data.review === "object" ? data.review : {};
  const changes = Array.isArray(review.changes) ? review.changes : [];
  const devices = changes.map((change) => ({
    name: text(change?.device || change?.device_id),
    model: text(change?.hardware_profile),
    disables: change?.disables_control === true,
    message: text(change?.message),
  }));
  const required = review.needs_migration === true;
  const valid = review.final_valid === true;
  const errors = (Array.isArray(review.validation_errors) ? review.validation_errors : [])
    .map((issue) => (typeof issue === "string" ? issue : text(issue?.message)))
    .filter(Boolean);
  const revision = typeof data.revision === "string" && data.revision ? data.revision : null;
  return {
    required,
    devices,
    losing: devices.filter((device) => device.disables).length,
    valid,
    errors,
    revision,
    canApply: required && valid && Boolean(revision),
  };
}
