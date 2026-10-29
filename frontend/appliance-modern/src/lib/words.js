// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Closed vocabularies the host reports in, said in the reader's language. A
 * value outside the list keeps the host's own word, so a state nobody
 * anticipated stays readable.
 */

/** Docker's container states, and "missing" for a configured container that does not exist. */
export const CONTAINER_STATES = Object.freeze(["created", "running", "restarting", "exited", "paused", "dead", "removing", "missing"]);

/** The Docker daemon's states (appliance/docker_backend.py DAEMON_*). */
export const DAEMON_STATES = Object.freeze(["running", "stopped", "unavailable"]);

/** NetworkManager's device states, as the network section reports them. */
export const DEVICE_STATES = Object.freeze(["connected", "connecting", "disconnected", "unavailable", "unmanaged"]);

/** NetworkManager's connectivity answers. */
export const CONNECTIVITY = Object.freeze(["full", "limited", "portal", "none", "unknown"]);

/** A systemd unit's ActiveState, and "unknown" when systemctl answered nothing (appliance/systemd.py). */
export const UNIT_STATES = Object.freeze(["active", "inactive", "failed", "activating", "deactivating", "reloading", "unknown"]);

/** { key } when the value is in the vocabulary, else the value itself as English text. */
export function word(prefix, vocabulary, value) {
  if (value === null || value === undefined || value === "") return null;
  return vocabulary.includes(value) ? { key: `${prefix}.${value}` } : { text: String(value), lang: "en" };
}

export function containerTone(state) {
  if (state === "running") return "c-ok";
  if (state === "restarting" || state === "created" || state === "paused") return "c-warn";
  if (!state) return "c-idle";
  return "c-fault";
}
