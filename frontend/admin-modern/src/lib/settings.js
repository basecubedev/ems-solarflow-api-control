// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import { TUNED_KINDS, commonDefaults, deviceFallbacks, deviceFields, deviceKind, fieldKey, held, mqttDefaultPort, mqttUsesTls, sentDevices } from "./devices.js";
/**
 * The saved settings as the Maintenance config view describes them
 * (admin/maintenance_config.py), and the editor's draft of them. The draft is
 * the same object the server handed out; only its ``features`` paths change
 * here, and the server's preview and apply check every change. Devices and
 * the grid meter stay with the Classic editor for now.
 */

export const DEVICE_KINDS = Object.freeze(["local_api", "zendure_mqtt", "e3dc_modbus", "opendtu_http", "external_mqtt"]);
export const MQTT_SOURCES = Object.freeze(["local_mqtt", "zendure_cloud_mqtt"]);
/** The Classic editor's tabs, which #maintenance-settings-<tab> opens. */
export const SETTINGS_TABS = Object.freeze(["devices", "features", "safety", "expert"]);
/** Feature sections with words in every language; another section keeps the catalogue's own title. */
export const FEATURE_IDS = Object.freeze(["zendure_mqtt", "winter", "battery_care", "battery_full_charge_assist", "ac_charge_control", "energy_savings", "dashboard", "influxdb"]);

/**
 * Each feature's mark, in the tone the dashboard gives the same thing: the
 * broker violet, winter cyan as on the Control page, battery care as its
 * runtime card, the battery green, AC charging the grid's, savings the money
 * tone, analytics violet. A section of the catalogue that is not a feature
 * wears the settings mark.
 */
export const FEATURE_LOOKS = Object.freeze({
  zendure_mqtt: { icon: "radio-tower", tone: "c-soc" },
  winter: { icon: "snowflake", tone: "c-out" },
  battery_care: { icon: "battery-low", tone: "c-soc" },
  battery_full_charge_assist: { icon: "battery", tone: "c-bat" },
  ac_charge_control: { icon: "plug-zap", tone: "c-grid" },
  energy_savings: { icon: "piggy-bank", tone: "c-money" },
  dashboard: { icon: "layout-dashboard", tone: "c-out" },
  influxdb: { icon: "chart-spline", tone: "c-soc" },
});
export const SECTION_LOOK = Object.freeze({ icon: "settings-2", tone: "c-pv" });

/** Features with no switch of their own: they run once a device is set up for them. */
export const AUTOMATIC_FEATURES = Object.freeze(["zendure_mqtt"]);

const mapping = (value) => (value && typeof value === "object" && !Array.isArray(value) ? value : {});

/** The editor's tabs here; ``devices`` still opens the Classic editor. */
export const EDIT_TABS = Object.freeze(["features", "safety", "expert"]);

/** Catalogue groups that make up the safety tab (ems/config_catalog.py), the same three Classic reads. */
export const SAFETY_GROUPS = Object.freeze(["safety_gates", "safety_holds", "limits"]);

const HIDDEN_LEVELS = new Set(["deprecated", "internal"]);
const LEVELS = ["normal", "advanced", "expert"];
const NUMBER_TYPES = new Set(["number", "integer"]);
const LIST_TYPES = new Set(["month_list", "string_list"]);

/** Consequences worth naming beside a field; ``restart_required`` is on most fields, so it is no signal. */
export const RISKS = Object.freeze(["control_stability", "data_loss", "secret", "deprecated"]);

function sections(config) {
  return Array.isArray(config?.catalog?.feature_sections) ? config.catalog.feature_sections : [];
}

/** The path of a section's on/off switch, when it has one. */
export function switchPath(section) {
  const path = section?.enabled_path;
  if (typeof path === "string" && path) return path;
  if (Array.isArray(path) && path.length) return path.join(".");
  return null;
}

/**
 * Whether a section is on in these values. A switch the values do not hold is
 * not off: the catalogue's default is what EMS applies to it.
 */
export function switchedOn(section, values) {
  const path = switchPath(section);
  if (!path) return null;
  if (Object.hasOwn(mapping(values), path)) return Boolean(values[path]);
  const field = (section.fields || []).find((item) => item.path === path);
  return Boolean(field?.default);
}

export function isSafetyField(field) {
  return SAFETY_GROUPS.includes(field?.group);
}

/** Every section lands on one tab, by the catalogue's audience axis. */
export function sectionTab(section) {
  return section?.setup_group === "advanced" ? "expert" : "features";
}

/** A section's fields by level, without its switch, its safety fields and the hidden levels. */
export function levelled(section) {
  const own = switchPath(section);
  const out = { normal: [], advanced: [], expert: [] };
  for (const field of section?.fields || []) {
    if (!field?.path || field.path === own || isSafetyField(field) || HIDDEN_LEVELS.has(field.level)) continue;
    out[LEVELS.includes(field.level) ? field.level : "normal"].push(field);
  }
  return out;
}

/** The sections of one tab, in catalogue order. */
export function tabSections(config, tab) {
  return sections(config)
    .filter((section) => sectionTab(section) === tab)
    .filter((section) => switchPath(section) || LEVELS.some((level) => levelled(section)[level].length));
}

/**
 * The safety tab: one flat block per safety group, in the catalogue's group
 * order. It is flat on purpose: a level disclosure would hide most write gates.
 */
export function safetyBlocks(config) {
  const blocks = [];
  for (const section of sections(config)) {
    for (const group of section.groups || []) {
      if (!SAFETY_GROUPS.includes(group?.id)) continue;
      const fields = (section.fields || []).filter((field) => field.group === group.id && !HIDDEN_LEVELS.has(field.level));
      if (fields.length) blocks.push({ id: group.id, title: String(group.title || group.id), summary: String(group.summary || ""), order: group.order || 0, fields });
    }
  }
  return blocks.sort((a, b) => a.order - b.order);
}

/** A risk every field of a block shares, said once above it instead of on every row. */
export function sharedRisk(fields) {
  if (!Array.isArray(fields) || fields.length < 2) return null;
  const risk = fields[0]?.risk;
  if (!["control_stability", "data_loss"].includes(risk)) return null;
  return fields.every((field) => field.risk === risk) ? risk : null;
}

/**
 * What a field's input shows. A switch the draft does not hold shows the
 * catalogue's default, because that is what EMS applies; four of the five
 * write gates are absent from most config files and default to on.
 */
export function shownValue(field, value) {
  const unset = value === null || value === undefined;
  if (field?.type === "boolean") return Boolean(unset ? field.default : value);
  if (unset) return "";
  return Array.isArray(value) ? value.join(", ") : String(value);
}

/**
 * The default a field can name, or undefined: none, an empty one, a secret, an
 * example such as an address, or an installation value, whose default is only
 * the template's example, unless ``installation`` lets it name the value the
 * EMS falls back to.
 */
export function nameableDefault(field, fallback = field?.default, { installation = false } = {}) {
  if (!field || field.secret || field.example_default || (field.installation_value && !installation) || field.type === "password") return undefined;
  if (fallback === undefined || fallback === null || fallback === "" || (Array.isArray(fallback) && !fallback.length)) return undefined;
  return fallback;
}

/** The default named in an empty input; an installation value names the value the EMS falls back to. */
export function defaultHint(field, value, fallback = field?.default) {
  const standard = nameableDefault(field, fallback, { installation: true });
  if (field?.type === "boolean" || standard === undefined) return "";
  if (value !== null && value !== undefined && value !== "") return "";
  return Array.isArray(standard) ? standard.join(", ") : String(standard);
}

function comparable(field, value) {
  const text = shownValue(field, value);
  if (!Array.isArray(value) && !LIST_TYPES.has(field.type)) return text.trim();
  const items = text.split(",").map((item) => item.trim()).filter(Boolean);
  return (field.type === "month_list" ? items.map(Number).sort((a, b) => a - b) : items).join(",");
}

/**
 * The default a set value departs from, or undefined: the value is unset or at
 * its default, or the field has no default to name. Numbers compare by value,
 * lists without their spacing and months in any order. An installation value
 * has a default to depart from only with ``installation``.
 */
export function departedDefault(field, value, fallback = field?.default, { installation = false } = {}) {
  const standard = nameableDefault(field, fallback, { installation });
  if (standard === undefined) return undefined;
  if (field.type === "boolean") {
    if (value === null || value === undefined) return undefined;
    return Boolean(value) === Boolean(standard) ? undefined : Boolean(standard);
  }
  if (value === null || value === undefined || value === "") return undefined;
  const set = comparable(field, value);
  const known = comparable(field, standard);
  const numeric = NUMBER_TYPES.has(field.type) && set !== "" && Number.isFinite(Number(set)) && Number.isFinite(Number(known));
  return (numeric ? Number(set) === Number(known) : set === known) ? undefined : standard;
}

/**
 * The features as the server receives them. An emptied value that names a
 * default is sent as that default, so what its input showed grey is what the
 * EMS runs. An installation value goes empty, so it is removed and the EMS
 * falls back as its input said.
 */
export function sentFeatures(features, edits, fields) {
  const sent = { ...mapping(features) };
  for (const [path, value] of Object.entries(mapping(edits))) {
    if (held(value)) continue;
    const standard = nameableDefault(fields?.get?.(path));
    if (standard !== undefined) sent[path] = Array.isArray(standard) ? [...standard] : standard;
  }
  return sent;
}

/**
 * The Maintenance draft as the server receives it, laid over ``saved`` (the
 * editable draft of the saved config). Settings and device values take what
 * their emptied inputs named. An emptied broker port, which an empty answer
 * would leave as it was, goes out as the default port, unless the broker's
 * address is emptied too.
 */
export function sentDraft(draft, edits, fields, saved, catalog) {
  if (!draft) return draft;
  const next = { ...draft, features: sentFeatures(draft.features, edits, fields) };
  if (Array.isArray(draft.devices)) next.devices = sentDevices(draft.devices, catalog, deviceFallbacks(commonDefaults(catalog), draft.features, fields), saved?.devices);
  const broker = mapping(draft.zendure_mqtt);
  if (broker.port === "") {
    next.zendure_mqtt = { ...broker, port: mqttDefaultPort(mqttUsesTls(broker)) };
    if (!held(broker.host)) delete next.zendure_mqtt.port;
  }
  return next;
}

/** The grid meter's bridge password reference as a choice of the saved bridge passwords, shown by name. */
export function bridgePasswordField(field, passwords) {
  const entries = passwords || [];
  return {
    ...field,
    options: entries.map((entry) => String(entry.id)),
    optionLabels: Object.fromEntries(entries.map((entry) => [String(entry.id), String(entry.label || entry.id)])),
  };
}

/** A field's choices: the catalogue's options, with the saved value kept when it is none of them. */
export function choices(field, value) {
  const options = (field?.options || []).map(String);
  const current = value === null || value === undefined ? "" : String(value);
  return current && !options.includes(current) ? [current, ...options] : options;
}

function same(a, b) {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

/** Every field the editor offers, by path, with each section's switch even where the catalogue lists it apart. */
export function fieldIndex(config) {
  const index = new Map();
  for (const section of sections(config)) {
    for (const field of section.fields || []) if (field?.path) index.set(field.path, field);
    const own = switchPath(section);
    if (own && !index.has(own)) index.set(own, { path: own, type: "boolean" });
  }
  return index;
}

/**
 * Whether a value is what the saved config already says: the saved value
 * itself, or as its input shows it; for a key the config does not hold, the
 * catalogue default of a switch, or nothing typed.
 */
export function sameAsSaved(field, value, saved) {
  const values = mapping(saved);
  if (Object.hasOwn(values, field.path)) {
    return same(value, values[field.path]) || (typeof value === "string" && value === shownValue(field, values[field.path]));
  }
  return field.type === "boolean" ? value === Boolean(field.default) : value === "" || value === null || value === undefined;
}

/** The edits that still change something against this saved config, for fields the catalogue still offers. */
export function liveEdits(edits, saved, fields) {
  return Object.fromEntries(Object.entries(mapping(edits)).filter(([path, value]) => fields.has(path) && !sameAsSaved(fields.get(path), value, saved)));
}

/** How many settings the draft changed against the saved config, counted the way Classic counts them. */
export function changeCount(draft, saved) {
  if (!draft || !saved) return 0;
  const now = mapping(draft.features);
  const before = mapping(saved.features);
  let count = 0;
  for (const path of new Set([...Object.keys(now), ...Object.keys(before)])) if (!same(now[path], before[path])) count += 1;
  const devices = Array.isArray(draft.devices) ? draft.devices : [];
  const savedDevices = Array.isArray(saved.devices) ? saved.devices : [];
  for (let index = 0; index < Math.max(devices.length, savedDevices.length); index += 1) if (!same(devices[index], savedDevices[index])) count += 1;
  for (const key of ["grid_meter", "zendure_mqtt"]) if (!same(draft[key], saved[key])) count += 1;
  return count;
}

/** The preview's changes, every row of them, as "takes effect now" and "needs an EMS restart". */
export function changeGroups(diff) {
  const rows = [
    ...(diff?.changes || []).map((entry) => ({ ...entry, kind: "changed" })),
    ...(diff?.added || []).map((entry) => ({ ...entry, kind: "added" })),
    ...(diff?.removed || []).map((entry) => ({ ...entry, kind: "removed" })),
  ];
  return [
    { when: "live", rows: rows.filter((row) => row.applies_live === true) },
    { when: "restart", rows: rows.filter((row) => row.applies_live !== true) },
  ].filter((group) => group.rows.length);
}

/**
 * Whether EMS needs a restart after this save: a row the server did not mark
 * live, removed ones included, or a value it names in ``runtime_sync`` as not
 * handed over at once; either leaves the old value running.
 */
export function restartNeeded(diff, result) {
  if (changeGroups(diff).some((group) => group.when === "restart")) return true;
  const sync = mapping(result?.runtime_sync);
  return (sync.skipped || []).length > 0 || (sync.warnings || []).length > 0;
}

/**
 * The operating settings a save could not hand to the EMS (``operating_sync``
 * skipped): they are in neither file, so the page names them instead of
 * saying the EMS uses them.
 */
export function operatingSkipped(result) {
  const skipped = mapping(result?.operating_sync).skipped;
  return (Array.isArray(skipped) ? skipped : []).map((entry) => String(mapping(entry).path ?? "")).filter(Boolean);
}

/**
 * The devices of a draft without their operating values: each device's own
 * (the keys its ``operating_base`` names) and that base. The runtime state
 * holds those, and the dashboard, a schedule or emsctl changes them without
 * touching the saved config, so they are no sign that the config changed.
 */
export function installedDevices(devices) {
  if (!Array.isArray(devices)) return devices ?? null;
  return devices.map((device) => {
    const base = mapping(device).operating_base;
    if (!base || typeof base !== "object") return device;
    const kept = { ...device };
    for (const key of [...Object.keys(base), "operating_base"]) delete kept[key];
    return kept;
  });
}

/**
 * Stored device edits laid over the devices the page loaded now: each
 * operating value the stored device still holds as it loaded it takes the
 * value the EMS runs now, and every device takes the base it was loaded with
 * now, so a value the dashboard or a schedule changed meanwhile is neither
 * shown stale nor put back. A value changed here stays.
 */
export function withFreshOperating(devices, loaded) {
  if (!Array.isArray(devices)) return devices;
  const now = new Map((Array.isArray(loaded) ? loaded : []).filter((device) => device?.original_name).map((device) => [device.original_name, device]));
  return devices.map((device) => {
    const base = mapping(device).operating_base;
    const fresh = now.get(device?.original_name);
    if (!base || typeof base !== "object" || !fresh?.operating_base) return device;
    const next = { ...device, operating_base: { ...fresh.operating_base } };
    for (const [key, value] of Object.entries(base)) {
      if (same(device[key], value) && Object.hasOwn(fresh, key)) next[key] = fresh[key];
    }
    return next;
  });
}

/** A value in the preview as text. */
export function previewValue(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "true" : "false";
  return String(value);
}

/**
 * The operating value the EMS runs below a hard limit the editor shows, from
 * the config answer's ``operating`` (admin/operation_settings.py): the total
 * below ``system.max_total_power``, a device's power below its own limit, and
 * a device's control switch beside its activation. Changed on the dashboard;
 * null where the answer names none.
 */
export function operatingValue(operating, path, device = null) {
  const view = mapping(operating);
  if (device === null) return Object.hasOwn(view, path) && view[path] !== null && view[path] !== undefined ? view[path] : null;
  const values = mapping(mapping(view.devices)[device]);
  return Object.hasOwn(values, path) && values[path] !== null && values[path] !== undefined ? values[path] : null;
}

/** A switch the file spells as anything but true or false, which the EMS reads field by field ("false" on a write gate closes it). */
export function spelledSwitch(field, value) {
  return field?.type === "boolean" && value !== null && value !== undefined && typeof value !== "boolean";
}

/**
 * One setting held against its default: its value when it departs. Null when
 * the field names no default, or the value does not depart from it. A switch
 * spelled as text always departs: its text is no default, and what it does
 * depends on how the EMS reads that switch.
 */
function departure(field, value, fallback = field?.default) {
  const standard = nameableDefault(field, fallback);
  if (standard === undefined) return null;
  if (!spelledSwitch(field, value) && departedDefault(field, value, fallback) === undefined) return null;
  return { field, value, standard };
}

/**
 * Every setting held away from this release's defaults, by the rule of the
 * editor's "Default: X" badge, an operating setting as the EMS runs it:
 * the settings of every section, its switch included, and the tuning of each
 * device whose card holds it. Connections, the fields the catalogue marks as
 * such (addresses, ports, URLs, a broker's user), and the grid meter, a hardware
 * section, describe the installation and are not listed. Groups follow
 * the catalogue's sections, then one per device; each row names the tab that
 * edits it, the search that finds it there (none for a section's switch,
 * which stands at the section's head, or a device's field) and, for those,
 * the section or the device's saved name, which still finds it in a draft
 * that removed a device before it. A value the config does not hold runs its
 * default and is no departure.
 */
export function departures(config) {
  const saved = mapping(config?.draft?.features);
  const groups = [];
  for (const section of sections(config)) {
    const own = switchPath(section);
    const rows = [];
    for (const field of section.fields || []) {
      if (!field?.path || HIDDEN_LEVELS.has(field.level) || field.connection) continue;
      const row = departure(field, saved[field.path]);
      if (!row) continue;
      rows.push(field.path === own ? { ...row, tab: sectionTab(section), search: "", section: section.id } : { ...row, tab: isSafetyField(field) ? "safety" : sectionTab(section), search: field.path });
    }
    if (rows.length) groups.push({ id: `section:${section.id}`, section, rows });
  }
  const common = deviceFields(config?.catalog).common.filter((field) => !HIDDEN_LEVELS.has(field.level) && !field.connection);
  const devices = Array.isArray(config?.draft?.devices) ? config.draft.devices : [];
  devices.forEach((device, index) => {
    if (!TUNED_KINDS.includes(deviceKind(device))) return;
    const rows = [];
    for (const field of common) {
      const key = fieldKey(field.path);
      const row = departure(field, device?.[key]);
      if (row) rows.push({ ...row, tab: "devices", search: "", device: device?.original_name ?? "" });
    }
    if (rows.length) groups.push({ id: `device:${index}`, device, rows });
  });
  return groups;
}

/** Whether a departure matches the search: by its field, as fieldMatches reads it, or by the name of its device. */
export function departureMatches(group, row, query, shown = (text) => text) {
  const needle = String(query || "").trim().toLowerCase();
  if (fieldMatches(row?.field, query, shown)) return true;
  return Boolean(group?.device) && String(group.device.name || "").toLowerCase().includes(needle);
}

/**
 * Whether a field matches the search, ignoring case: its label or description
 * as the catalogue sends them and as the page shows them (``shown`` turns the
 * catalogue's words into the reader's), or its config path.
 */
export function fieldMatches(field, query, shown = (text) => text) {
  const needle = String(query || "").trim().toLowerCase();
  if (!needle) return true;
  const texts = [field?.label, field?.description, field?.label && shown(field.label), field?.description && shown(field.description), field?.path];
  return texts.some((text) => String(text || "").toLowerCase().includes(needle));
}

/** A typed password stays in memory, and so does a new pairing's token with its serial: neither is written to the tab's storage. */
export function storable(hardware) {
  const values = { ...hardware.values };
  if (values.zendure_mqtt && "password" in values.zendure_mqtt) {
    const { password, ...broker } = values.zendure_mqtt;
    values.zendure_mqtt = broker;
  }
  if (values.grid_meter?.mqtt && "password" in values.grid_meter.mqtt) {
    const { password, ...mqtt } = values.grid_meter.mqtt;
    values.grid_meter = { ...values.grid_meter, mqtt };
  }
  if (values.grid_meter && "token" in values.grid_meter) {
    const { token, serial, ...meter } = values.grid_meter;
    values.grid_meter = meter;
  }
  return { ...hardware, values };
}

/** What a change did to a consumer with recorded values (admin/consumer_history.py). */
export const HISTORY_KINDS = Object.freeze(["renamed", "moved", "reused"]);

/** The consumer history questions of a preview that have no answer yet. */
export function openHistory(preview) {
  return (Array.isArray(preview?.consumer_history) ? preview.consumer_history : []).filter((question) => !question?.answered);
}

/** A refusal with the server's validation reasons as its message, when it gave any. */
export function refused(answer) {
  const reasons = (answer?.validation?.errors || []).map((issue) => issue?.message).filter((message) => typeof message === "string" && message);
  return reasons.length ? { ...answer, message: reasons.join(" ") } : answer;
}

/**
 * The reviewed draft written through ``send()``, answered only once
 * ``reload()`` has read the config back: "Saved" must not stand above the
 * config it replaced, and a device edit made after it must start from the new
 * one. Answers ``{ applied: { result, at } }``, or ``{ error }`` for a refusal
 * or a failed request; the config is read back either way.
 */
export async function applyAndRead(send, reload) {
  let outcome;
  try {
    const result = await send();
    outcome = result?.ok === false ? { error: result } : { applied: { result, at: Date.now() } };
  } catch (error) {
    outcome = { error };
  }
  await reload().catch(() => null);
  return outcome;
}

/**
 * One hardware change written straight to the config. ``read()`` answers the
 * config as it is now, ``{ saved, editable, error }``, so a change another console
 * saved meanwhile is the base rather than undone; ``build(editable)`` makes the
 * change from it, ``null`` for nothing to change. The result is previewed and,
 * when the server finds it valid on that same revision, applied with a backup
 * first through ``send(path, body)``. Answers ``{ ok, reason, error, applied }``;
 * ``reason`` ``changed`` means the config moved on between reading and the check.
 * A change that touches a consumer's recorded values is asked about first:
 * ``decide(questions)`` answers ``{ name: "keep" | "new" }``, or ``null`` to
 * write nothing (``reason`` ``cancelled``).
 */
export async function writeAtOnce(build, { read, send, paths, decide = null }) {
  try {
    const { saved, editable, error } = await read();
    if (!saved || !editable) return { ok: false, error };
    const changes = build(editable);
    if (!changes) return { ok: true };
    const features = saved.draft?.features && typeof saved.draft.features === "object" ? saved.draft.features : {};
    let draft = { ...editable, ...changes, features: { ...features } };
    let preview = await send(paths.preview, { draft });
    const open = openHistory(preview);
    if (open.length && decide) {
      const answers = await decide(open);
      if (!answers) return { ok: false, reason: "cancelled" };
      draft = { ...draft, consumer_history: { ...answers } };
      preview = await send(paths.preview, { draft });
    }
    if (!preview?.validation?.ok || !preview.revision) return { ok: false, error: refused(preview) };
    if (saved.revision && preview.revision !== saved.revision) return { ok: false, reason: "changed" };
    const result = await send(paths.apply, { draft, revision: preview.revision, confirm: true, backup: true });
    if (result?.ok === false) return { ok: false, error: refused(result) };
    return { ok: true, applied: { result, diff: preview.diff, at: Date.now() } };
  } catch (error) {
    return { ok: false, error: error?.payload?.validation ? refused(error.payload) : error };
  }
}
