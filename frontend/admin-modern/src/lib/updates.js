// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The release catalogue the Guided Upgrade offers (admin/releases.py), as a
 * list to read. Which build may be installed is decided by the server and,
 * again, by the Guided Upgrade itself; this only shows its verdicts.
 */

export const CHANNELS = Object.freeze(["stable", "rc", "latest", "development", "legacy", "unknown"]);
export const UPGRADE_STATES = Object.freeze({
  upgrade_available: "c-ok",
  rollback_available: "c-acc",
  already_current: "c-idle",
  older_than_running_build: "c-idle",
  downgrade_blocked: "c-idle",
  identity_unknown: "c-idle",
});
export const RELEASES_SHOWN = 6;

/** Whether the running Admin and EMS are a proven pair (admin/system_alignment.py, RUNNING_PAIR_*). */
export const PAIR_STATES = Object.freeze({
  current: "c-ok",
  recorded: "c-ok",
  unverified: "c-idle",
  unpaired: "c-warn",
  transition_active: "c-acc",
});

export function pairView(pair) {
  if (!pair || typeof pair !== "object") return null;
  const state = Object.hasOwn(PAIR_STATES, pair.state) ? pair.state : "unverified";
  return { state, tone: PAIR_STATES[state] };
}

/**
 * The catalogue's notes are English sentences (admin/releases.py,
 * admin/image_identity.py). Each one an owner can meet has words in every
 * language; tests/test_admin_modern.py holds each sentence to the server's
 * source, so a reworded one fails there rather than turning English here.
 */
export const RELEASE_NOTES = Object.freeze({
  "Rolling Docker channel, not a stable release": "updates.note.latest",
  "Rolling Docker channel, not a stable release; switching from a stable release requires extra caution": "updates.note.latestFromStable",
  "Release candidate, not a stable release": "updates.note.rc",
  "Admin Setup supports Docker releases from v0.6.0 onward": "updates.note.tooOld",
  "This tag is not supported by Admin Setup": "updates.note.unsupportedTag",
  "Docker setup resources are missing for this release.": "updates.note.resourcesMissing",
  "Docker setup resources could not be verified.": "updates.note.resourcesUnverified",
  "Downgrades are not supported by the setup assistant. Use Backup/Restore flow instead.": "updates.note.downgrade",
  "Target is older than the running EMS build. Guided Upgrade only supports upgrades. Use Backup/Restore for recovery flows.": "updates.note.olderThanRunning",
  "Guided Upgrade cannot verify this release is newer than the running EMS build. Use Backup/Restore for recovery flows.": "updates.note.identityUnknown",
  "Guided Upgrade verifies this target's build identity (pulling the image if needed) before making any changes.": "updates.note.identityUnverified",
  "Already running this EMS build.": "updates.note.alreadyCurrent",
  "Earlier patch in the release line you are running. Selectable as a rollback; it is not an upgrade.": "updates.note.rollback",
  "Legacy image metadata missing. Upgrade check uses SemVer fallback.": "updates.note.legacySemver",
  "Legacy image metadata missing. This upgrade cannot be fully verified and is allowed only by admin test override.": "updates.note.legacyUnverified",
  "The current release cannot be compared safely; no downgrade claim was made.": "updates.note.uncomparable",
});

/** A catalogue note as words: a translation key where the sentence is known, else the server's English. */
export function noteView(note) {
  const text = typeof note === "string" ? note.trim() : "";
  if (!text) return null;
  return Object.hasOwn(RELEASE_NOTES, text) ? { key: RELEASE_NOTES[text], text } : { key: null, text };
}

export function releaseView(release, catalogue) {
  const channel = CHANNELS.includes(release?.channel) ? release.channel : "unknown";
  const state = Object.hasOwn(UPGRADE_STATES, release?.upgrade_state) ? release.upgrade_state : null;
  return {
    tag: String(release?.tag || ""),
    channel,
    state,
    tone: state ? UPGRADE_STATES[state] : "c-idle",
    publishedAt: release?.published_at || null,
    recommended: Boolean(release?.tag) && release.tag === catalogue?.default_release,
    running: Boolean(release?.active),
    selectable: release?.selectable !== false,
    note: noteView(release?.reason || release?.upgrade_warning),
  };
}

/** The releases worth showing first: the recommended one and anything newer than what runs. */
export function releaseList(catalogue) {
  const releases = Array.isArray(catalogue?.releases) ? catalogue.releases : [];
  return releases.map((release) => releaseView(release, catalogue));
}
