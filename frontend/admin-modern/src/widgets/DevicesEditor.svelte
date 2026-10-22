<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import DeviceCard from "./DeviceCard.svelte";
  import SettingField from "./SettingField.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { settings, setHardwareValues } from "../state/settings.svelte.js";
  import {
    NO_GRID_METER,
    commonDefaults,
    connectionSource,
    d0Serial,
    deviceFields,
    deviceKind,
    fieldKey,
    generations,
    gridFields,
    gridIsMqtt,
    gridValue,
    gridVariant,
    hardwareModel,
    modelsForGeneration,
    mqttControl,
    newE3dc,
    newLocalInverter,
    newMqttInverter,
    reasonKey,
    removeDevice,
    setBroker,
    setD0Serial,
    setDeviceValue,
    setGridPassword,
    setGridPort,
    setGridValue,
    setMqttValue,
    switchGridType,
  } from "../lib/devices.js";

  let { config } = $props();


  const catalog = $derived(config.catalog);
  const draft = $derived(settings.draft);
  const devices = $derived(Array.isArray(draft?.devices) ? draft.devices : []);
  const meter = $derived(draft?.grid_meter || {});
  const broker = $derived(draft?.zendure_mqtt || {});
  const fields = $derived(deviceFields(catalog));
  const defaults = $derived(commonDefaults(catalog));
  const variants = $derived(Object.values(catalog?.grid_meter_variants || {}));
  const meterType = $derived(String(meter.type || ""));
  const meterIsMqtt = $derived(gridIsMqtt(catalog, meterType));
  const meterFields = $derived(gridFields(catalog, meter));
  const named = $derived(broker.managed === "named");
  let ports = {};
  let added = $state(-1);
  let keys = $state([]);
  let made = 0;

  /**
   * A key per device that stays with it when another one is removed, so a
   * card's open state and inputs never pass to its neighbour. Devices the
   * page did not add here (loaded, discarded, found) get theirs when they appear.
   */
  $effect.pre(() => {
    const count = devices.length;
    if (keys.length === count) return;
    keys = count < keys.length ? keys.slice(0, count) : [...keys, ...Array.from({ length: count - keys.length }, () => `device-${(made += 1)}`)];
  });

  function update(index, next) {
    const list = [...devices];
    list[index] = next;
    setHardwareValues({ devices: list });
  }

  function add(device) {
    added = devices.length;
    setHardwareValues({ devices: [...devices, device] });
  }

  function remove(index) {
    added = -1;
    keys = keys.filter((_, position) => position !== index);
    setHardwareValues(removeDevice(draft, index));
  }

  function liveFor(device, key) {
    const entry = config.overrides?.devices?.[device?.original_name]?.[key];
    return entry?.source === "dashboard_override" ? entry : null;
  }

  function sourceLabel(device) {
    const source = connectionSource(device);
    if (source === "zendure_mqtt") return t("settings.source.zendure_cloud_mqtt");
    if (source === "local_mqtt") return t("settings.source.local_mqtt");
    if (source === "modbus_tcp") return t("settings.kind.e3dc_modbus");
    if (source === "local_api") return t("settings.kind.local_api");
    return "";
  }

  function endpoint(item) {
    const address = String(item?.ip || "") + (item?.port ? `:${item.port}` : "");
    return address || t("devices.addressMissing");
  }

  function localSummary(device) {
    const parts = [device.name || t("settings.unnamed"), endpoint(device), device.sn ? `SN ${device.sn}` : t("devices.serialMissing")];
    if (device.max_power != null && device.max_power !== "") parts.push(`${device.max_power} W`);
    return parts.join(" · ");
  }

  function mqttSummary(device) {
    const identity = device.serial_number || device.device_id || "";
    const generation = generations(catalog).find((entry) => entry.id === device.hardware_generation);
    return [device.name || t("settings.unnamed"), identity ? `ID ${identity}` : t("devices.idMissing"), generation?.label || ""].filter(Boolean).join(" · ");
  }

  function externalState(device) {
    if (!device.entry) return "added";
    if (device.proposal_id) return device.catalog_retired ? "readOnApply" : "moves";
    return device.catalog_retired ? "notRead" : "read";
  }

  function meterMeta() {
    if (meterIsMqtt) {
      const mqtt = meter.mqtt || {};
      if (mqtt.broker_ref) return t("devices.grid.brokerProfile", { ref: mqtt.broker_ref });
      if (mqtt.host) return String(mqtt.host) + (mqtt.port ? `:${mqtt.port}` : "");
      return meterType ? t("devices.grid.brokerMissing") : t("devices.notConfigured");
    }
    if (!meter.ip) return meterType ? t("devices.addressMissing") : t("devices.notConfigured");
    return endpoint(meter);
  }

  function chooseMeterType(type) {
    const result = switchGridType(meter, type, catalog, ports);
    ports = result.ports;
    setHardwareValues({ grid_meter: result.meter });
  }

  function own(key, description = true) {
    return { path: key, label: t(`devices.field.${key}`), description: description ? t(`devices.field.${key}.hint`) : "" };
  }
</script>

{#snippet row(target, field)}
  {#if target.grid}{@render gridField(field)}{:else}{@render deviceField(target.device, target.index, field)}{/if}
{/snippet}

{#snippet levelled(list, target)}
  {@const normal = list.filter((field) => !["advanced", "expert"].includes(field.level) && !["deprecated", "internal"].includes(field.level))}
  {@const advanced = list.filter((field) => field.level === "advanced")}
  {@const expert = list.filter((field) => field.level === "expert")}
  {#if normal.length}<div class="fields">{#each normal as field (field.path)}{@render row(target, field)}{/each}</div>{/if}
  {#each [["advanced", advanced], ["expert", expert]] as [level, group] (level)}
    {#if group.length}
      <details class="level">
        <summary>{t(`settings.edit.level.${level}`)} <span class="muted num">({group.length})</span></summary>
        <div class="fields">{#each group as field (field.path)}{@render row(target, field)}{/each}</div>
      </details>
    {/if}
  {/each}
{/snippet}

{#snippet deviceField(device, index, field)}
  {@const key = fieldKey(field.path)}
  <SettingField {field} value={device[key]} live={liveFor(device, key)} defaultValue={["name", "ip", "sn"].includes(key) ? null : defaults[key]} onchange={(value) => update(index, setDeviceValue(device, key, value))} />
{/snippet}

{#snippet gridField(field)}
  {#if field.path === "grid_meter.mqtt.password"}
    <SettingField field={{ path: field.path, label: t("devices.field.password"), description: t("devices.field.password.hint") }} lang={null} type="password" value={meter.mqtt?.password || ""} disabled={meter.mqtt?.clear_password === true} placeholder={meter.mqtt?.has_password ? t("devices.passwordKeep") : t("devices.passwordOptional")} onchange={(value) => setHardwareValues({ grid_meter: setGridPassword(meter, { password: value }) })} />
    {#if meter.mqtt?.has_password}
      <SettingField field={{ path: "grid_meter.mqtt.clear_password", type: "boolean", label: t("devices.field.clearPassword"), description: t("devices.field.clearPassword.hint") }} lang={null} value={meter.mqtt?.clear_password === true} onchange={(value) => setHardwareValues({ grid_meter: setGridPassword(meter, { clear: value }) })} />
    {/if}
  {:else}
    <SettingField {field} value={gridValue(meter, field.path)} onchange={(value) => setHardwareValues({ grid_meter: setGridValue(meter, field.path, value) })} />
    {#if field.path === "grid_meter.ip"}
      <SettingField field={{ path: "grid_meter.port", type: "number", label: t("devices.field.port"), description: gridVariant(catalog, meterType)?.port_description || t("devices.field.port.hint") }} lang={gridVariant(catalog, meterType)?.port_description ? "en" : undefined} value={meter.port} onchange={(value) => setHardwareValues({ grid_meter: setGridPort(meter, value) })} />
    {/if}
  {/if}
{/snippet}

{#snippet fact(label, value, hint)}
  <div class="fact-line"><span class="fact-name">{label}</span><span class="fact-text" lang="en">{value}</span>{#if hint}<span class="muted small">{hint}</span>{/if}</div>
{/snippet}

{#snippet enabledField(device, index, kind)}
  <SettingField field={{ path: "enabled", type: "boolean", label: t("devices.field.enabled"), description: t(`devices.enabledHint.${kind}`) }} lang={null} value={device.enabled !== false} live={liveFor(device, "enabled")} onchange={(value) => update(index, { ...device, enabled: value })} />
{/snippet}

<div class="editor" data-testid="devices-editor">
  <DeviceCard id="grid-meter" title={t("devices.grid.title")} model={gridVariant(catalog, meterType)?.label || t("devices.grid.none")} meta={meterMeta()} enabled={Boolean(meterType)} onremove={meter.present || meterType || meter.ip ? () => setHardwareValues({ grid_meter: { ...NO_GRID_METER } }) : null}>
    <div class="fields">
      <div class="setting">
        <label class="head" for="grid-type">{t("devices.grid.type")}</label>
        <select id="grid-type" class="input" value={meterType} onchange={(event) => chooseMeterType(event.currentTarget.value)} data-testid="grid-type">
          <option value="">{t("devices.grid.noneOption")}</option>
          {#each variants as variant (variant.id)}<option value={variant.id} lang="en">{variant.label}</option>{/each}
        </select>
        <p class="desc">{t("devices.grid.typeHint")}</p>
      </div>
      {#if meterType === "zendure_smartmeter_d0"}
        <SettingField field={own("d0Serial")} lang={null} value={d0Serial(meter.mqtt?.topic)} onchange={(value) => setHardwareValues({ grid_meter: setD0Serial(meter, value) })} />
      {/if}
    </div>
    {#if meterIsMqtt && meter.mqtt?.broker_ref}<p class="note"><Icon name="info" />{t("devices.grid.brokerManaged", { ref: meter.mqtt.broker_ref })}</p>{/if}
    {@render levelled(meterFields, { grid: true })}
  </DeviceCard>

  <DeviceCard id="zendure-broker" title={t("devices.broker.title")} model={named ? t("devices.broker.named") : broker.present ? t("devices.broker.local") : t("devices.notConfigured")} meta={broker.host ? String(broker.host) + (broker.port == null ? "" : `:${broker.port}`) : ""} enabled={named || broker.enabled !== false} status={named ? t("devices.readOnly") : ""}>
      <p class="note"><Icon name="info" />{#if named}{t("devices.broker.namedHelp")}{:else}<span lang="en">{catalog?.zendure_mqtt_broker?.help || ""}</span>{/if}</p>
      {#if broker.tls_stored_invalid && !named}<p class="note warn"><Icon name="triangle-alert" />{t("devices.broker.tlsInvalid")}</p>{/if}
      <div class="fields">
        <SettingField field={own("host", false)} lang={null} value={broker.host || ""} disabled={named} onchange={(value) => setHardwareValues({ zendure_mqtt: setBroker(broker, "host", value) })} />
        <SettingField field={{ ...own("port", false), type: "number" }} lang={null} value={broker.port ?? ""} disabled={named} onchange={(value) => setHardwareValues({ zendure_mqtt: setBroker(broker, "port", value) })} />
        <div class="setting">
          <label class="head" for="broker-security">{t("devices.field.security")}</label>
          <select id="broker-security" class="input" value={broker.tls ? "tls" : "plain"} disabled={named} onchange={(event) => setHardwareValues({ zendure_mqtt: setBroker(broker, "security", event.currentTarget.value) })}>
            <option value="plain">{t("devices.security.plain")}</option>
            <option value="tls">TLS</option>
          </select>
        </div>
        <SettingField field={own("username", false)} lang={null} value={broker.username || ""} disabled={named} onchange={(value) => setHardwareValues({ zendure_mqtt: setBroker(broker, "username", value) })} />
        <SettingField field={{ path: "zendure_mqtt.password", label: t("devices.field.password"), description: t("devices.field.password.hint") }} lang={null} type="password" value={broker.password || ""} disabled={named || broker.clear_password === true} placeholder={broker.has_password ? t("devices.passwordKeep") : t("devices.passwordOptional")} onchange={(value) => setHardwareValues({ zendure_mqtt: setBroker(broker, "password", value) })} />
        {#if broker.has_password}
          <SettingField field={{ path: "zendure_mqtt.clear_password", type: "boolean", label: t("devices.field.clearPassword"), description: t("devices.field.clearPassword.hint") }} lang={null} value={broker.clear_password === true} disabled={named} onchange={(value) => setHardwareValues({ zendure_mqtt: setBroker(broker, "clear", value) })} />
        {/if}
      </div>
    </DeviceCard>

  {#each devices as device, index (keys[index] ?? `extra-${index}`)}
    {@const kind = deviceKind(device)}
    {#if kind === "local_api"}
      <DeviceCard id={`inverter-${index}`} title={t("devices.inverter", { n: index + 1 })} model={t("devices.model.local")} meta={localSummary(device)} enabled={device.enabled !== false} connection={sourceLabel(device)} open={added === index} onremove={() => remove(index)}>
        <div class="fields">{@render enabledField(device, index, "local")}</div>
        {@render levelled(fields.identity, { device, index })}
        {@render levelled(fields.common, { device, index })}
      </DeviceCard>
    {:else if kind === "zendure_mqtt"}
      {@const control = mqttControl(device, catalog)}
      {@const model = hardwareModel(catalog, device.hardware_model)}
      <DeviceCard id={`mqtt-${index}`} title={t("devices.inverter", { n: index + 1 })} model={(model?.label || t("devices.mqtt.unknownModel")) + (device.alternative_layout ? ` · ${t("devices.mqtt.alternativeLayout")}` : "")} meta={mqttSummary(device)} enabled={device.enabled !== false} connection={sourceLabel(device)} open={added === index} onremove={() => remove(index)}>
        <div class="fields">
          <SettingField field={own("name")} lang={null} value={device.name || ""} onchange={(value) => update(index, { ...device, name: value })} />
          <SettingField field={own("serial")} lang={null} value={device.serial_number || ""} onchange={(value) => update(index, { ...device, serial_number: value })} />
          <SettingField field={own("deviceId")} lang={null} value={device.mqtt?.device_id || ""} onchange={(value) => update(index, setMqttValue(device, "device_id", value, catalog))} />
          <div class="setting">
            <label class="head" for={`generation-${index}`}>{t("devices.field.generation")}</label>
            <select id={`generation-${index}`} class="input" value={device.hardware_generation || ""} onchange={(event) => update(index, setMqttValue(device, "hardware_generation", event.currentTarget.value, catalog))}>
              {#if !device.hardware_generation}<option value="">—</option>{/if}
              {#each generations(catalog) as generation (generation.id)}<option value={generation.id} lang="en">{generation.label}</option>{/each}
            </select>
            <p class="desc">{t("devices.field.generation.hint")}</p>
          </div>
          <div class="setting">
            <label class="head" for={`model-${index}`}>{t("devices.field.model")}</label>
            <select id={`model-${index}`} class="input" value={device.hardware_model || ""} onchange={(event) => update(index, setMqttValue(device, "hardware_model", event.currentTarget.value, catalog))}>
              {#each modelsForGeneration(catalog, device.hardware_generation) as option (option.id)}<option value={option.id} lang="en">{option.label}</option>{/each}
            </select>
            <p class="desc">{t("devices.field.model.hint")}</p>
          </div>
          {#if control.showProductKey}
            <SettingField field={own("productKey")} lang={null} value={device.product_key || ""} onchange={(value) => update(index, setMqttValue(device, "product_key", value, catalog))} />
          {/if}
          {@render enabledField(device, index, "mqtt")}
        </div>
        <div class="facts-list">
          {@render fact(t("devices.mqtt.protocol"), model?.power_write_profile || t("devices.mqtt.noProtocol"), "")}
          {@render fact(t("devices.mqtt.maturity"), model?.validation_maturity || t("devices.mqtt.unknown"), "")}
          {@render fact(t("devices.mqtt.operations"), (model?.supported_operations || []).join(", ") || t("devices.mqtt.none"), "")}
          {@render fact(t("devices.mqtt.readiness"), t(`devices.mqtt.state.${control.state}`), "")}
          {@render fact(t("devices.mqtt.control"), control.shouldControl ? t("devices.mqtt.available") : t("devices.mqtt.unavailable", { reason: t(reasonKey(control.blockReason)) }), t("devices.mqtt.controlHint"))}
        </div>
        <p class="note"><Icon name="info" />{t(`devices.mqtt.note.${control.state === "routeMissing" ? "routeMissing" : control.shouldControl ? "controlled" : control.supported ? "completeRoute" : "telemetryOnly"}`)}</p>
        {@render levelled(fields.common, { device, index })}
      </DeviceCard>
    {:else if kind === "external_mqtt"}
      {@const mqtt = device.mqtt || device.entry?.mqtt || {}}
      {@const state = externalState(device)}
      <DeviceCard id={`external-${index}`} title={t("devices.inverter", { n: index + 1 })} model={`${device.catalog_label || device.display_name || t("devices.external.label")} · ${t(`devices.external.model.${device.catalog_retired ? (device.proposal_id ? "readOnApply" : "notRead") : "read"}`)}`} meta={[device.name || t("settings.unnamed"), mqtt.device_id ? `ID ${mqtt.device_id}` : t("devices.idMissing"), t("devices.external.broker", { ref: mqtt.broker_ref || "default" }), t(`devices.external.state.${state}`)].join(" · ")} enabled={device.enabled !== false} onremove={() => remove(index)}>
        <p class="note"><Icon name="info" />{t(`devices.external.note.${device.catalog_retired ? (device.proposal_id ? "moving" : "retired") : device.entry ? "installed" : "added"}`)}</p>
        <div class="fields">
          <SettingField field={own("name")} lang={null} value={device.name || ""} onchange={(value) => update(index, { ...device, name: value })} />
          {@render enabledField(device, index, "external")}
        </div>
        <div class="facts-list">
          {@render fact(t("devices.external.profile"), String(mqtt.broker_ref || "default"), t("devices.external.profileHint"))}
          {@render fact(t("devices.external.deviceId"), String(mqtt.device_id || "") || t("devices.external.missing"), t("devices.external.deviceIdHint"))}
        </div>
      </DeviceCard>
    {:else}
      <DeviceCard id={`e3dc-${index}`} title={t("devices.readOnlyDevice", { n: index + 1 })} model={t("devices.model.e3dc")} meta={[device.name || t("settings.unnamed"), endpoint(device), t("devices.readOnly")].join(" · ")} enabled={device.enabled !== false} connection={sourceLabel(device)} open={added === index} onremove={() => remove(index)}>
        <div class="fields">
          {@render enabledField(device, index, "e3dc")}
          <SettingField field={own("e3dcName")} lang={null} value={device.name || ""} onchange={(value) => update(index, { ...device, name: value })} />
          <SettingField field={own("e3dcHost")} lang={null} value={device.ip || ""} onchange={(value) => update(index, { ...device, ip: value })} />
          <SettingField field={{ ...own("e3dcPort"), type: "number" }} lang={null} value={device.port ?? ""} onchange={(value) => update(index, { ...device, port: value })} />
          <SettingField field={{ ...own("e3dcUnit"), type: "number" }} lang={null} value={device.unit_id ?? ""} onchange={(value) => update(index, { ...device, unit_id: value })} />
        </div>
      </DeviceCard>
    {/if}
  {/each}

  <div class="row-wrap add">
    <button class="btn secondary small" type="button" onclick={() => add(newLocalInverter(devices, catalog))} data-testid="add-inverter"><Icon name="package-plus" size="sm" />{t("devices.add.local")}</button>
    <button class="btn secondary small" type="button" onclick={() => add(newMqttInverter(devices, catalog))} data-testid="add-mqtt-inverter"><Icon name="radio-tower" size="sm" />{t("devices.add.mqtt")}</button>
    <button class="btn secondary small" type="button" onclick={() => add(newE3dc(devices))} data-testid="add-e3dc"><Icon name="eye" size="sm" />{t("devices.add.e3dc")}</button>
  </div>
</div>

<style>
  .editor { display: flex; flex-direction: column; gap: var(--s3); }
  .fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--s4) var(--s5); }
  .setting { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
  .head { font-size: 13.5px; font-weight: 560; }
  .desc { margin: 0; color: var(--m-muted); font-size: 12.5px; }
  .level summary { cursor: pointer; font-size: 13px; font-weight: 600; margin-bottom: var(--s3); }
  .facts-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--s3) var(--s5); }
  .fact-line { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .fact-name { font-size: 12.5px; color: var(--m-muted); }
  .fact-text { font-weight: 560; overflow-wrap: anywhere; }
  .small { font-size: 12px; }
  .add { gap: var(--s2); }
  @container content (max-width: 720px) { .fields, .facts-list { grid-template-columns: minmax(0, 1fr); } }
</style>
