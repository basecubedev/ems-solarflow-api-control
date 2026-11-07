// SPDX-License-Identifier: AGPL-3.0-or-later
import { deviceKind, nextInverterName } from "./devices.js";
import { externalDeviceFromProposal, useExternalConnection } from "./discovery.js";

/**
 * The codes the built-in broker, the MQTT mapping and the live topics answer
 * with. Each list is paired with the server's own (tests/test_admin_modern_mqtt.py),
 * and every entry has a translation (tests/strings.test.js).
 */

export const BUILTIN_BROKER = "ems-mqtt";

export const BROKER_ISSUES = Object.freeze([
  "mqtt_broker_settings_invalid",
  "mqtt_broker_port_invalid",
  "mqtt_broker_port_in_use",
  "mqtt_broker_no_access",
  "mqtt_broker_account_name_invalid",
  "mqtt_broker_account_duplicate",
  "mqtt_broker_account_reserved",
  "mqtt_broker_account_unknown",
  "mqtt_broker_account_password_missing",
  "mqtt_broker_account_password_invalid",
  "mqtt_broker_topic_prefix_invalid",
  "mqtt_broker_grid_meter_topic_unprotectable",
  "mqtt_broker_grid_meter_account_shared",
  "mqtt_broker_grid_meter_account_lacks_option",
  "mqtt_broker_shared_account_zendure",
  "mqtt_broker_runs_as_root",
  "mqtt_broker_grid_meter_reader_denied",
  "mqtt_broker_profile_name_reserved",
]);

export const MAPPING_ISSUES = Object.freeze([
  "mqtt_mapping_document_invalid",
  "mqtt_mapping_schema_unsupported",
  "mqtt_mapping_limit_exceeded",
  "mqtt_mapping_broker_invalid",
  "mqtt_mapping_device_id_invalid",
  "mqtt_mapping_device_id_duplicate",
  "mqtt_mapping_family_unknown",
  "mqtt_mapping_target_unknown",
  "mqtt_mapping_target_required_missing",
  "mqtt_mapping_battery_shape_invalid",
  "mqtt_mapping_topic_invalid",
  "mqtt_mapping_topic_native",
  "mqtt_mapping_path_invalid",
  "mqtt_mapping_option_invalid",
  "mqtt_mapping_source_shared",
  "mqtt_mapping_brokers_unreadable",
]);

/** Codes the Admin's mapping editor adds to the mapping file's own; each issue carries its ``params``. */
export const MAPPING_EDITOR_ISSUES = Object.freeze(["mqtt_mapping_device_id_taken"]);

export const LIVE_LABELS = Object.freeze([
  "shelly_gen1",
  "shelly_gen2",
  "tasmota",
  "opendtu",
  "ahoydtu",
  "zendure_native",
  "zendure_meter_native",
  "ems_output",
  "unknown",
]);

export const SILENCE_REASONS = Object.freeze(["disabled", "source_missing", "stale", "source_unreadable", "catalog_refused", "not_delivered"]);

export const VALUE_REASONS = Object.freeze(["format_invalid", "not_json", "path_invalid", "path_missing", "not_a_number", "out_of_range"]);

export const TARGETS = Object.freeze(["inverterPower", "solarPower", "batteryPower", "batterySoc"]);

/** A consumer publishes its power alone, under its own catalog family. */
export const CONSUMER_TARGETS = Object.freeze(["power"]);
export const CONSUMER_FAMILY = "ems_consumer";

/** Whether a mapping entry describes a consumer rather than an inverter. */
export const isConsumer = (device) => device?.family === CONSUMER_FAMILY;

/** The targets a mapping entry may carry. */
export const targetsOf = (device) => (isConsumer(device) ? CONSUMER_TARGETS : TARGETS);

export const BROKER_STATES = Object.freeze(["off", "running", "stopped", "unknown"]);

/** How a mapping may read a broker of the chooser, and what a check of one can answer. */
export const BROKER_ACCESS = Object.freeze(["anonymous", "account", "needs_account", "untrusted", "unreachable", "unchecked"]);

export const BROKER_CHOICE_ERRORS = Object.freeze([
  "mqtt_mapping_broker_unknown",
  "mqtt_mapping_broker_refused",
  "mqtt_mapping_broker_unreachable",
  "mqtt_mapping_broker_endpoint_invalid",
  "mqtt_mapping_broker_account_invalid",
  "mqtt_mapping_broker_search_busy",
]);

/**
 * The broker the live topics and a new mapping start on: the one chosen while
 * it can still be read, else the running built-in broker, else the first
 * other one that can be read, else the built-in broker, which says why not.
 */
export function preferredBroker(brokers, current = "") {
  const list = brokers || [];
  if (list.some((item) => item.ref === current && item.usable)) return current;
  const usable = list.filter((item) => item.usable);
  const pick = usable.find((item) => item.kind === "builtin") || usable[0] || list.find((item) => item.kind === "builtin");
  return pick?.ref || BUILTIN_BROKER;
}

/** ``host:port``, or nothing for a broker the server could not place. */
export function brokerAddress(item) {
  return item?.host ? `${item.host}:${item.port}` : "";
}

/** off, running, stopped (switched on, container not running) or unknown, from the server's view. */
export function brokerState(view) {
  if (!view || !view.settings) return "unknown";
  if (!view.settings.enabled) return "off";
  return view.running ? "running" : "stopped";
}

/** A source as the mapping file stores it, with every default the server would fill in. */
export function newSource(topic, path) {
  return path == null || path === ""
    ? { topic, format: "number", factor: 1, invert: false, negative: "keep", max_age_seconds: 60 }
    : { topic, format: "json", path, factor: 1, invert: false, negative: "keep", max_age_seconds: 60 };
}

/** A new mapped device on ``brokerRef`` with an empty inverter power target. */
export function newDevice(brokerRef) {
  return { id: "", label: "", enabled: true, broker_ref: brokerRef || BUILTIN_BROKER, values: { inverterPower: { sources: [] } } };
}

/** The sources of one target, whichever shape it has, as [side, index, source]. */
export function targetSources(target) {
  if (!target) return [];
  if (Array.isArray(target.sources)) return target.sources.map((source, index) => ["sources", index, source]);
  return [
    ...(target.charge || []).map((source, index) => ["charge", index, source]),
    ...(target.discharge || []).map((source, index) => ["discharge", index, source]),
  ];
}

/** A device id proposed from a live-topic group key: what the catalog accepts, at most 64 characters. */
export function proposedId(key) {
  return String(key || "").replace(/[^A-Za-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 64);
}

/** What a mapped device measures, asked before any target: each answer maps one power value to the catalog keys. */
export const DEVICE_KINDS = Object.freeze(["balcony", "hybrid", "ac_battery", "consumer", "other"]);

/** Units a reading can come in, and the factor that turns it into what the catalog expects. */
export const UNITS = Object.freeze({ W: 1, kW: 1000, fraction: 100, percent: 1 });
export const POWER_UNITS = Object.freeze(["W", "kW"]);
export const SOC_UNITS = Object.freeze(["percent", "fraction"]);

/** Targets that must never be negative; a negative reading there is a sign to turn round. */
export const NON_NEGATIVE_TARGETS = Object.freeze(["inverterPower", "solarPower", "batterySoc", "power"]);

export const MIN_MAX_AGE = 60;
export const MAX_MAX_AGE = 900;
export const CHANGE_ONLY_MAX_AGE = 600;

export function unitFactor(unit) {
  return UNITS[unit] ?? 1;
}

/** The unit a stored factor stands for, or null for a factor only the advanced editor can show. */
export function unitOf(factor, units = Object.keys(UNITS)) {
  return units.find((unit) => UNITS[unit] === factor) ?? null;
}

/** A Shelly Gen2+ status topic is sent when the value changes, not on a clock. */
export function isChangeOnly(topic, label) {
  return label === "shelly_gen2" && /\/status\/[a-z0-9]+:\d+$/.test(String(topic || ""));
}

/**
 * How old a source may grow before the device falls silent: three times the
 * interval the live topics measured between messages, between 60 and 900
 * seconds. A source that sends only on change, or one not yet seen often enough
 * to measure, gets 600: a quiet minute says nothing about either.
 */
export function maxAgeFor(intervalSeconds, { changeOnly = false } = {}) {
  const interval = Number(intervalSeconds);
  if (changeOnly || intervalSeconds == null || !Number.isFinite(interval) || interval <= 0) return CHANGE_ONLY_MAX_AGE;
  return Math.min(MAX_MAX_AGE, Math.max(MIN_MAX_AGE, Math.ceil(3 * interval)));
}

/** Invert and clip a value that reads negative for a target that cannot be. */
export function signPreset(value, target) {
  const turn = NON_NEGATIVE_TARGETS.includes(target) && Number(value) < 0;
  return { invert: turn, negative: turn ? "zero" : "keep" };
}

/** A device id from the name a person gives it: what the catalog accepts, never empty. */
export function idFromName(name) {
  const plain = String(name || "")
    .toLowerCase()
    .replace(/ä/g, "ae").replace(/ö/g, "oe").replace(/ü/g, "ue").replace(/ß/g, "ss")
    .normalize("NFKD").replace(/[̀-ͯ]/g, "");
  return plain.replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 64) || "device";
}

/** How long a new session is given to receive the retained messages of devices that were there all along. */
export const SETTLE_MS = 3000;

/**
 * The first mappable group that showed up after waiting began, or null. A group
 * known when the wait began never counts, nor does one first seen before
 * ``since``: a broker hands a new session its retained messages at once, from
 * devices that were there all along. ``first_seen_seconds`` is the server's age
 * of a topic at ``polledAt``, the moment its answer arrived.
 */
export function freshGroup(groups, { before = [], since = 0, polledAt = 0 } = {}) {
  return (
    (groups || []).find((group) => {
      if (!group.mappable || before.includes(groupKey(group))) return false;
      const seen = (group.topics || []).map((topic) => polledAt - 1000 * Number(topic.first_seen_seconds));
      return seen.length > 0 && seen.every((at) => Number.isFinite(at) && at >= since);
    }) || null
  );
}

/**
 * The proposal a saved mapping makes for its device: the server builds it from
 * the mapping itself and marks it with the mapping's broker, so no search has
 * to find the device first. ``null`` when the server offers none, as for a
 * broker that is off.
 */
export function mappedProposal(proposals, deviceId, brokerRef) {
  const matches = (proposals || []).filter(
    (proposal) =>
      proposal?.mapping_broker_ref === brokerRef && proposal.config_fragment?.type === "external_mqtt" && proposal.config_fragment.mqtt?.device_id === deviceId,
  );
  return matches.length === 1 ? matches[0] : null;
}

/** Whether an EMS device is the one a mapping publishes as ``id``: a new entry carries ``mqtt``, a saved one its ``entry``. */
export function mapsTo(id) {
  return (device) =>
    (deviceKind(device) === "external_mqtt" || device?.type === "external_mqtt") && (device?.mqtt?.device_id ?? device?.entry?.mqtt?.device_id) === id;
}

/**
 * Where in ``devices`` the EMS device a mapping publishes as ``id`` is, -1 for
 * none: of entries sharing the id, the first switched on, as EMS reads one
 * switched off not at all; a draft's own switch counts before its saved one.
 */
export function mappedIndex(devices, id) {
  const list = devices || [];
  const switchedOn = list.findIndex((device) => mapsTo(id)(device) && (device?.enabled ?? device?.entry?.enabled ?? true) === true);
  return switchedOn >= 0 ? switchedOn : list.findIndex(mapsTo(id));
}

/** The EMS name a mapped device gets: its label where no other device has it, else ``fallback``, by default the next free INV_ name. */
export function emsDeviceName(label, devices, fallback = nextInverterName(devices)) {
  const name = String(label ?? "").trim();
  const used = new Set((devices || []).map((device) => String(device?.name ?? "").trim().toLowerCase()));
  return name && !used.has(name.toLowerCase()) ? name : fallback;
}

/**
 * What a saved mapping change did to its EMS device, each said by
 * mqtt.saved.*.<kind>: ``again`` maps one again that the running EMS still
 * has, and ``changed`` gives one back the name the running EMS reads it under
 * while other saved changes to it wait.
 */
export const SAVED_KINDS = Object.freeze(["added", "moved", "renamed", "removed", "again", "changed"]);

/**
 * Whether the running EMS reads a mapped device as saved, and how a saved
 * entry differs from what it read, as the server tells them in the mapping
 * view's ``ems_devices`` (admin/ems_device_states.py); each change is said by
 * mqtt.restart.item.<change>.
 */
export const EMS_DEVICE_STATES = Object.freeze(["running", "off", "waiting", "leaving"]);
export const EMS_CHANGES = Object.freeze(["added", "renamed", "changed", "removed"]);

/** Why the server cannot tell what the running EMS reads, each said by mqtt.saved.silent.<reason>: none runs, or it is of another release. */
export const EMS_UNKNOWN_REASONS = Object.freeze(["stopped", "unnamed"]);

/** How long after a restart a new EMS may take to report what it reads before its silence is said. */
export const EMS_REPORT_GRACE_MS = 120_000;

/** The mapped devices the running EMS has yet to read as saved: ``[{ id, state, change, name, ran }]``, none while that cannot be told. */
export function emsWaiting(emsDevices) {
  if (!emsDevices?.known) return [];
  return Object.entries(emsDevices.devices || {})
    .filter(([, item]) => item?.state === "waiting" || item?.state === "leaving")
    .map(([id, item]) => ({ id, ...item }));
}

/**
 * Whether the running EMS reads mapped device ``id`` as saved, or, ``removing``,
 * no longer has it or reads what is left under its id as saved; ``null`` while
 * that cannot be told.
 */
export function emsReads(emsDevices, id, removing = false) {
  if (!emsDevices?.known) return null;
  const item = emsDevices.devices?.[id];
  const asSaved = item?.state === "running" || item?.state === "off";
  return removing ? !item || asSaved : asSaved;
}

/**
 * What a saved step says the change did, as the running EMS will see it: as
 * asked, ``added`` for any change to a device that EMS never had, ``again``
 * for an added device it still has from before it was removed, or ``changed``
 * for a new name that is the one EMS reads it under, other changes waiting.
 */
export function savedKind(kind, item) {
  if (item?.change === "added" && kind !== "removed") return "added";
  if (kind === "renamed" && item?.change === "changed") return "changed";
  return kind === "added" && item?.ran ? "again" : kind;
}

/**
 * Why a restart that ended at ``restartedAt`` is not followed by the restarted
 * EMS saying what it reads, as a server answer ``seen`` asked for after it says
 * at ``now``; ``before`` is the start of the EMS that ran when the restart
 * began. A new EMS that cannot tell this console is ``unnamed`` at once; when
 * no new one has reported by EMS_REPORT_GRACE_MS, the answer's own reason, or
 * ``stopped``. Else null.
 */
export function emsSilence(seen, restartedAt, now, before = null) {
  if (!restartedAt || !(seen?.at > restartedAt)) return null;
  if (seen.startedAt && seen.startedAt !== before) return seen.known ? null : "unnamed";
  if (now - restartedAt <= EMS_REPORT_GRACE_MS) return null;
  return seen.reason === "unnamed" ? "unnamed" : "stopped";
}

/**
 * Where a mapped device stands in an EMS device list: ``null`` when it is not
 * there, ``"there"`` on its mapping's connection, ``"elsewhere"`` when the EMS
 * reads the same id from another broker.
 */
export function emsPlace(devices, id, connection) {
  const device = (devices || [])[mappedIndex(devices, id)];
  if (!device) return null;
  return connection && device.catalog_connection_id && device.catalog_connection_id !== connection ? "elsewhere" : "there";
}

/**
 * The device list with the mapped device in it: added from its proposal and
 * named after ``label``, or moved to the proposal's connection when the EMS
 * reads the same id elsewhere. ``null`` when the list already has it there.
 */
export function withMappedDevice(devices, proposal, label) {
  const list = devices || [];
  const moved = useExternalConnection(list, proposal);
  if (moved) return list[moved.index].catalog_connection_id === moved.devices[moved.index].catalog_connection_id ? null : moved.devices;
  if (list.some(mapsTo(proposal?.config_fragment?.mqtt?.device_id))) return null;
  const device = externalDeviceFromProposal(proposal, list);
  return [...list, { ...device, name: emsDeviceName(label, list, device.name) }];
}

/** The device list with mapped device ``id`` named ``label``; ``null`` when that changes nothing or another device has the name. */
export function withRenamedDevice(devices, id, label) {
  const list = devices || [];
  const index = mappedIndex(list, id);
  const name = String(label ?? "").trim();
  if (index < 0 || !name || list[index].name === name) return null;
  const taken = list.some((device, position) => position !== index && String(device?.name ?? "").trim().toLowerCase() === name.toLowerCase());
  return taken ? null : list.map((device, position) => (position === index ? { ...device, name } : device));
}

/** Why adding, moving, renaming or removing an EMS device did not happen, each with its own words. */
export const ADOPT_REFUSALS = Object.freeze(["missing", "busy", "changed", "cancelled"]);

/** The steps from a broker to a device in the EMS, and how far the panel has come. */
export const MAPPING_STEPS = Object.freeze(["broker", "connect", "map", "ems"]);

/**
 * Which step is next: a broker that can be read, a device that publishes on it,
 * a saved mapping, and that mapping in the EMS. ``inEms`` counts the mapped
 * devices the EMS has, saved or about to be.
 */
export function mappingStep({ readable = false, groups = 0, devices = 0, inEms = 0 } = {}) {
  if (devices > 0) return inEms >= devices ? MAPPING_STEPS.length : 3;
  if (groups > 0) return 2;
  return readable ? 1 : 0;
}

/** ``base``, or ``base-2``, ``base-3`` … when the mapping already uses it; never longer than 64 characters. */
export function uniqueId(base, taken = []) {
  const used = new Set(taken);
  if (!used.has(base)) return base;
  for (let number = 2; ; number += 1) {
    const suffix = `-${number}`;
    const candidate = `${base.slice(0, 64 - suffix.length)}${suffix}`;
    if (!used.has(candidate)) return candidate;
  }
}

/**
 * One source from a live value: the unit's factor, the sign preset for the
 * target and a maximum age from how often the topic arrives. ``turnSign`` is
 * false where a negative reading may be the device at work rather than its
 * sign convention, such as a battery charging: then it is only clipped at 0.
 */
export function sourceFromValue(choice, target, unit = "W", { turnSign = true } = {}) {
  const source = newSource(choice.topic, choice.path ?? null);
  const clip = { invert: false, negative: NON_NEGATIVE_TARGETS.includes(target) ? "zero" : "keep" };
  const preset =
    target === "batteryPower" ? { invert: false, negative: "keep" } : turnSign ? signPreset(Number(choice.value) * unitFactor(unit), target) : clip;
  return {
    ...source,
    factor: unitFactor(unit),
    ...preset,
    max_age_seconds: maxAgeFor(choice.interval, { changeOnly: isChangeOnly(choice.topic, choice.label) }),
  };
}

/** Only a device without a battery can be read as having its sign the wrong way round from one negative value. */
function turnsSign(kind, target) {
  return kind === "balcony" || kind === "consumer" || target === "solarPower" || target === "batterySoc";
}

/**
 * The mapping document entry the answers describe. ``answers`` holds the kind,
 * the name, the broker and the chosen values (power, and where the kind asks for
 * them solar, battery and charge level), each ``{topic, path, value, rate, label, unit}``.
 * The server validates the result like any other mapping.
 */
export function deviceFromAnswers({ kind, name, brokerRef, power, solar = null, battery = null, soc = null, id = null }) {
  const values = {};
  const add = (target, choice) => {
    if (choice) {
      const unit = choice.unit || (target === "batterySoc" ? "percent" : "W");
      values[target] = { sources: [sourceFromValue(choice, target, unit, { turnSign: turnsSign(kind, target) })] };
    }
  };
  const entry = { id: id || idFromName(name), label: String(name || "").trim(), enabled: true, broker_ref: brokerRef || BUILTIN_BROKER, values };
  if (kind === "consumer") {
    add("power", power);
    return { ...entry, family: CONSUMER_FAMILY };
  }
  add("inverterPower", power);
  if (kind === "balcony") add("solarPower", power);
  if (kind === "hybrid") add("solarPower", solar);
  if (kind === "hybrid" || kind === "ac_battery") add("batteryPower", battery);
  if (kind === "hybrid" || kind === "ac_battery") add("batterySoc", soc);
  return entry;
}

/** A live-topic group's identity: one key under two labels is two devices. */
export function groupKey(group) {
  return `${group.label}/${group.key}`;
}

/** Every number a live-topic group carries, with what the mapping helpers need to know about it. */
export function groupValues(group) {
  return (group?.topics || []).flatMap((topic) =>
    (topic.values || []).map((value) => ({
      topic: topic.topic,
      path: value.path,
      value: value.value,
      likely: value.likely,
      rate: topic.messages_per_minute,
      interval: topic.interval_seconds ?? null,
      label: group.label,
    })),
  );
}

/** Real power by its common names, most specific first; apparent and reactive power come last. */
const POWER_NAMES = Object.freeze(["total_act_power", "apower", "power", "p_ac", "act_power"]);

function powerRank(value) {
  const name = String(value.path ?? String(value.topic || "").split("/").pop()).split(".").pop().toLowerCase();
  const known = POWER_NAMES.indexOf(name);
  if (known >= 0) return known;
  return /apparent|aprt|reactive|factor/.test(name) ? 2 * POWER_NAMES.length : POWER_NAMES.length;
}

/** The value a question preselects: the one that most looks like the wanted kind. */
export function likelyValue(values, likely = "power") {
  const candidates = values.filter((value) => value.likely === likely);
  if (likely !== "power") return candidates[0] || null;
  return [...candidates].sort((a, b) => powerRank(a) - powerRank(b))[0] || null;
}

/** The device families the connection guide knows, and how many short steps each takes. */
export const CONNECT_STEPS = Object.freeze({ shelly_gen2: 4, shelly_gen1: 4, tasmota: 4, dtu: 3, other: 3 });
export const CONNECT_TYPES = Object.freeze(Object.keys(CONNECT_STEPS));

/** The step keys of one family's guide, in order. */
export function connectSteps(type) {
  return Array.from({ length: CONNECT_STEPS[type] || 0 }, (_, index) => `mqtt.connect.${type}.${index + 1}`);
}
