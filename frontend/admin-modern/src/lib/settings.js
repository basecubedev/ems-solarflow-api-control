// SPDX-FileCopyrightText: 2026 basecubedev
// SPDX-License-Identifier: AGPL-3.0-or-later
import { commonDefaults, deviceFallbacks, held, mqttDefaultPort, mqttUsesTls, sentDevices } from "./devices.js";
/**
 * The saved settings as the Maintenance config view describes them
 * (admin/maintenance_config.py), and the editor's draft of them. The draft is
 * the same object the server handed out; only its ``features`` paths change
 * here, and the server's preview and apply check every change. Devices and
 * the grid meter stay with the Classic editor for now.
 */

export const DEVICE_KINDS = Object.freeze(["local_api", "zendure_mqtt", "e3dc_modbus", "external_mqtt"]);
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
 * lists without their spacing and months in any order.
 */
export function departedDefault(field, value, fallback = field?.default) {
  const standard = nameableDefault(field, fallback);
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

/** A value in the preview as text. */
export function previewValue(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "true" : "false";
  return String(value);
}

/**
 * The dashboard's runtime overrides that "Put live values back" resets, in the
 * shape POST /api/admin/maintenance/config/reset-runtime takes.
 */
export function overrideTargets(overrides) {
  const targets = [];
  for (const [path, entry] of Object.entries(mapping(overrides))) {
    if (path === "devices" || entry?.source !== "dashboard_override") continue;
    const dot = path.indexOf(".");
    if (dot < 0) continue;
    const head = path.slice(0, dot);
    const key = path.slice(dot + 1);
    targets.push(head === "system" ? { scope: "system", key } : { scope: "section", section: head, key });
  }
  for (const [name, fields] of Object.entries(mapping(mapping(overrides).devices))) {
    for (const [key, entry] of Object.entries(mapping(fields))) {
      if (entry?.source !== "dashboard_override") continue;
      const target = { scope: "device", name, key };
      const token = String(entry.physical_identity_token || "").trim();
      if (token.startsWith("opaque:v1:")) target.physical_identity_token = token;
      targets.push(target);
    }
  }
  return targets;
}

/** The live value the dashboard set for a path, when it differs from the saved config. */
export function liveOverride(overrides, path) {
  const entry = mapping(mapping(overrides)[path]);
  return entry.source === "dashboard_override" ? entry : null;
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
