// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The saved settings as the Maintenance config view describes them
 * (admin/maintenance_config.py). Read-only here: editing stays with the
 * Classic editor, whose preview and apply the server checks.
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
