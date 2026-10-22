// SPDX-License-Identifier: AGPL-3.0-or-later
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
export const FEATURE_IDS = Object.freeze(["zendure_mqtt", "winter", "battery_full_charge_assist", "ac_charge_control", "energy_savings", "dashboard", "influxdb"]);

/** Features with no switch of their own: they run once a device is set up for them. */
export const AUTOMATIC_FEATURES = Object.freeze(["zendure_mqtt"]);

const mapping = (value) => (value && typeof value === "object" && !Array.isArray(value) ? value : {});

/**
 * One device: what it is, how it is reached and whether EMS uses it. Where the
 * dashboard changed "in use" or the power limit at runtime (the config
 * answer's ``overrides.devices``, admin/config_runtime_overlap.py), the value
 * EMS follows is shown, and marked as changed there.
 */
export function deviceView(item, overrides) {
  const kind = DEVICE_KINDS.includes(item?.kind) ? item.kind : null;
  const mqtt = item?.mqtt && typeof item.mqtt === "object" ? item.mqtt : {};
  let connection = kind ? `settings.kind.${kind}` : null;
  if (kind === "zendure_mqtt" && MQTT_SOURCES.includes(mqtt.effective_source)) connection = `settings.source.${mqtt.effective_source}`;
  const address = item?.ip || (kind === "zendure_mqtt" || kind === "external_mqtt" ? mqtt.broker_ref || "" : "");
  const live = mapping(mapping(mapping(overrides).devices)[item?.original_name || item?.name]);
  const changed = (key) => mapping(live[key]).source === "dashboard_override";
  const enabled = changed("enabled") ? live.enabled.effective_value !== false : item?.enabled !== false;
  const maxPower = changed("max_power") && typeof live.max_power.effective_value === "number" ? live.max_power.effective_value : typeof item?.max_power === "number" ? item.max_power : null;
  return {
    name: String(item?.name || item?.original_name || ""),
    kind,
    connection,
    connectionText: kind ? "" : String(item?.kind || ""),
    model: item?.catalog_label || item?.hardware_model || "",
    address: String(address || ""),
    enabled,
    maxPower,
    overridden: changed("enabled") || changed("max_power"),
    readOnly: kind === "e3dc_modbus" || kind === "external_mqtt",
  };
}

/**
 * Each feature with an on/off switch, as EMS applies it: the saved value, or
 * the catalogue's default when the key is not saved at all, and the
 * dashboard's runtime override where one is in force.
 */
export function featureViews(config) {
  const sections = Array.isArray(config?.catalog?.feature_sections) ? config.catalog.feature_sections : [];
  const saved = mapping(config?.draft?.features);
  const overrides = mapping(config?.overrides);
  return sections
    .filter((section) => section?.kind === "feature")
    .map((section) => {
      const switchField = (section.fields || []).find((field) => field.path === `${section.path}.enabled` && field.type === "boolean");
      let enabled = null;
      let overridden = false;
      if (switchField) {
        enabled = Object.hasOwn(saved, switchField.path) ? saved[switchField.path] === true : switchField.default === true;
        const override = mapping(overrides[switchField.path]);
        if (override.source && override.source !== "config" && typeof override.effective_value === "boolean") {
          overridden = override.effective_value !== enabled;
          enabled = override.effective_value;
        }
      }
      return { id: String(section.id || ""), title: String(section.title || section.id || ""), summary: String(section.summary || ""), enabled, overridden };
    });
}

/** A grid meter's name from the catalogue the same answer carries (ems/config_catalog.py GRID_METER_VARIANTS). */
export function gridMeterName(type, variants) {
  return variants?.[type]?.label || type || "";
}

/** The editor's tabs here; ``devices`` still opens the Classic editor. */
export const EDIT_TABS = Object.freeze(["features", "safety", "expert"]);

/** Catalogue groups that make up the safety tab (ems/config_catalog.py), the same three Classic reads. */
export const SAFETY_GROUPS = Object.freeze(["safety_gates", "safety_holds", "limits"]);

const HIDDEN_LEVELS = new Set(["deprecated", "internal"]);
const LEVELS = ["normal", "advanced", "expert"];

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

/** The catalogue default named in an empty input. */
export function defaultHint(field, value) {
  if (field?.type === "boolean" || field?.default === undefined || field.default === null) return "";
  if (value !== null && value !== undefined && value !== "") return "";
  return Array.isArray(field.default) ? field.default.join(", ") : String(field.default);
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
 * Whether EMS needs a restart after this save. A removed key is mirrored to
 * the running EMS by nothing, and the server names any value it could not
 * hand over at once in ``runtime_sync``; either leaves the old value running.
 */
export function restartNeeded(diff, result) {
  if ((diff?.removed || []).length) return true;
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

/** Whether a field matches the search: label, description or config path, ignoring case. */
export function fieldMatches(field, query) {
  const needle = String(query || "").trim().toLowerCase();
  if (!needle) return true;
  return [field?.label, field?.description, field?.path].some((text) => String(text || "").toLowerCase().includes(needle));
}
