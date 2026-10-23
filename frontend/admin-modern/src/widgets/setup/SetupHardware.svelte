<script>
  import Icon from "../../../../shared/ui/Icon.svelte";
  import Status from "../../../../shared/ui/Status.svelte";
  import DeviceCard from "../DeviceCard.svelte";
  import SettingField from "../SettingField.svelte";
  import CatalogText from "../CatalogText.svelte";
  import { t, tn, say, catalogText } from "../../i18n/i18n.svelte.js";
  import { generations, hardwareModels, maturityWords, operationWords, reasonKey } from "../../lib/devices.js";
  import { isExternalProposal, observationKey, sourcesOf } from "../../lib/discovery.js";
  import { CONTINUITY, D0_TYPE, DEFAULT_GRID_METER, DEFAULT_INVERTER, SWITCH_REASONS, candidateState, catalogSection, deviceFieldKey, deviceValue, featureValue, formGeneration, gridFields, gridMeterType, hasSource, inverterCards, isReady, manualControl, meterIsMqtt, modelsForGeneration, pendingConfirmations, selectionControllable, topicMode, unselectedProposals } from "../../lib/setupDraft.js";
  import { setup } from "../../state/setup.svelte.js";
  import { setupDiscovery } from "../../state/setupDiscovery.svelte.js";
  import {
    setupDraft,
    addDevice,
    addManualDevice,
    addManualItem,
    addProposal,
    answerSwitch,
    moveItem,
    removeItem,
    removeManualDevice,
    removeSelection,
    resetItemName,
    resetSelectionName,
    setBroker,
    setBrokerPassword,
    setGridEnabled,
    setGridEndpoint,
    setGridFeature,
    setGridType,
    setItemEnabled,
    setItemField,
    setSelectionEnabled,
    setSelectionName,
    suggestedName,
    switchConnection,
    useGeneratedTopic,
    useGridMeter,
  } from "../../state/setupDraft.svelte.js";

  const LABELS = { local_api: "API", local_mqtt: "MQTT", zendure_mqtt: "Zendure MQTT", modbus_tcp: "Modbus TCP" };
  const HIDDEN = new Set(["deprecated", "internal"]);

  let manual = $state({ name: "", role: "inverter", type: "", host: "", port: "", serial: "" });
  let manualError = $state("");
  let device = $state({ name: suggestedName(), serial: "", routeId: "", generation: "", model: "", productKey: "" });
  let deviceError = $state("");

  const draft = $derived(setupDraft.draft);
  const catalog = $derived(setupDraft.catalog);
  const index = $derived(setupDraft.index);
  const template = $derived(setup.template?.template || null);
  const grid = $derived(setupDraft.grid);
  const gridType = $derived(grid ? gridMeterType(grid) : "shelly");
  const variants = $derived(catalog?.grid_meter_variants || {});
  const meterFields = $derived(gridFields(catalog, gridType));
  const deviceFields = $derived((catalogSection(catalog, "devices")?.fields || []).filter((field) => !HIDDEN.has(field.level)));
  const levels = $derived({ normal: deviceFields.filter((field) => !["advanced", "expert"].includes(field.level)), advanced: deviceFields.filter((field) => field.level === "advanced"), expert: deviceFields.filter((field) => field.level === "expert") });
  const cards = $derived(inverterCards(draft, index));
  const confirmations = $derived(pendingConfirmations(setupDraft.plan, setupDraft.confirmedSwitches));
  const offered = $derived(unselectedProposals(draft, setupDiscovery.proposals, index));
  const available = $derived(setupDraft.candidates);
  const roleVariants = $derived(Array.isArray(catalog?.hardware_variants?.[manual.role]) ? catalog.hardware_variants[manual.role] : []);
  const generation = $derived(formGeneration(catalog, device.generation));
  const models = $derived(modelsForGeneration(catalog, generation?.id || ""));
  const control = $derived(manualControl(catalog, device));
  const model = $derived(hardwareModels(catalog).find((entry) => entry.id === device.model) || null);
  const maxPower = $derived(deviceFields.find((field) => field.path === "devices[].max_power") || null);

  $effect(() => {
    const list = roleVariants;
    if (list.length && !list.some((variant) => variant.id === manual.type)) {
      const chosen = list.find((variant) => variant.default) || list[0];
      manual.type = chosen.id;
      manual.port = chosen.default_port != null ? String(chosen.default_port) : "";
    }
  });

  function chooseType(id) {
    manual.type = id;
    const variant = roleVariants.find((entry) => entry.id === id);
    manual.port = variant?.default_port != null ? String(variant.default_port) : "";
  }

  function chooseGeneration(id) {
    device.generation = id;
    if (!modelsForGeneration(catalog, id).some((entry) => entry.id === device.model)) device.model = "";
  }

  function endpoint(entry) {
    return `${entry?.ip || ""}${entry?.port ? `:${entry.port}` : ""}`;
  }

  function inverterMeta(item) {
    const parts = [item.config_name, endpoint(item), item.serial_number ? `SN ${item.serial_number}` : t("setup.hardware.serialMissing")];
    if (maxPower) {
      const value = deviceValue(item, maxPower, template);
      if (value !== "" && value != null) parts.push(`${value} W`);
    }
    return parts.filter(Boolean).join(" · ");
  }

  function selectionSource(entry) {
    return String(entry?.connection_source || "") === "zendure_cloud_mqtt" ? "zendure_mqtt" : "local_mqtt";
  }

  function identity(entry) {
    if (isExternalProposal(entry)) return entry.device_id ? `ID ${entry.device_id}` : t("setup.devices.idMissing");
    return entry.serial_number ? `SN ${entry.serial_number}` : t("setup.devices.snMissing");
  }

  function selectionModel(entry) {
    const proposal = setupDiscovery.proposals.find((candidate) => String(candidate.id || "") === String(entry.id || ""));
    return String(entry.display_name || proposal?.display_name || proposal?.hardware_model || entry.config_fragment?.name || DEFAULT_INVERTER);
  }

  function addManual(event) {
    event.preventDefault();
    manualError = addManualItem(manual);
    if (!manualError) manual = { name: "", role: manual.role, type: "", host: "", port: "", serial: "" };
  }

  function addMqttDevice(event) {
    event.preventDefault();
    deviceError = addManualDevice(device);
    if (!deviceError) device = { name: suggestedName(), serial: "", routeId: "", generation: device.generation, model: "", productKey: "" };
  }

  function generationLabel(id) {
    return generations(catalog).find((entry) => entry.id === id)?.label || id;
  }

  function modelLabel(id) {
    return hardwareModels(catalog).find((entry) => entry.id === id)?.label || t("setup.hardware.unknownModel");
  }
</script>

{#snippet candidateAction(state, add)}
  {#if state.state === "identity_conflict"}
    <button class="btn secondary small" type="button" disabled>{t("setup.hardware.identityConflict")}</button>
  {:else if state.state === "active"}
    <button class="btn secondary small" type="button" disabled>{t("setup.hardware.active")}</button>
  {:else if state.state === "alternative"}
    <button class="btn small" type="button" onclick={() => switchConnection(state.candidateId)} data-testid="setup-use-connection">{t("setup.hardware.useConnection")}</button>
  {:else}
    {@render add()}
  {/if}
{/snippet}

{#snippet candidateNote(state)}
  {#if state.state === "alternative"}<p class="muted small">{t("setup.hardware.alreadyAs", { name: state.configuredName || t("setup.hardware.anotherInverter"), via: LABELS[state.currentSource] || t("setup.hardware.unknown") })}</p>{/if}
  {#if state.state === "identity_conflict"}<p class="note warn"><Icon name="triangle-alert" />{t("setup.hardware.conflictNote")}</p>{/if}
{/snippet}

{#snippet fieldList(list, item)}
  <div class="fields">
    {#each list as field (field.path)}
      <SettingField {field} value={deviceValue(item, field, template)} onchange={(value) => setItemField(item.source_id, field, value)} />
    {/each}
  </div>
{/snippet}

{#snippet enabledRow(checked, onchange, hint)}
  <label class="check"><input type="checkbox" {checked} onchange={(event) => onchange(event.currentTarget.checked)} /><span>{t("setup.hardware.enabled")}</span></label>
  <p class="muted small">{hint}</p>
{/snippet}

<div class="hardware" data-testid="setup-hardware">
  <section class="block">
    <h3 class="sub">{t("setup.hardware.gridMeter")}</h3>
    <p class="muted small">{t("setup.hardware.gridLead")}</p>
    {#if grid}
      <DeviceCard id={grid.source_id} title={t("setup.hardware.gridMeter")} model={variants[gridType]?.label ? catalogText(variants[gridType].label).text : grid.display_name || DEFAULT_GRID_METER} modelLang={variants[gridType]?.label ? catalogText(variants[gridType].label).lang : undefined} meta={endpoint(grid)} enabled={grid.enabled !== false} status={grid.enabled !== false ? t("setup.hardware.on") : t("setup.hardware.off")} connection={grid.auto_selected ? t("setup.hardware.autoSelected") : ""} onremove={() => removeItem(grid.source_id)}>
        {#if setupDraft.gridStale}<p class="note warn"><Icon name="triangle-alert" />{t("setup.hint.gridMeterStale")}</p>{/if}
        {@render enabledRow(grid.enabled !== false, setGridEnabled, t("setup.hardware.gridEnabledHint"))}
        <label class="field"><span>{t("setup.hardware.meterType")}</span>
          <select class="input" value={gridType} onchange={(event) => setGridType(event.currentTarget.value)} data-testid="setup-grid-type">
            {#each Object.keys(variants) as key (key)}<option value={key} lang={catalogText(variants[key].label).lang}>{catalogText(variants[key].label).text}</option>{/each}
          </select>
        </label>
        {#if gridType === D0_TYPE}
          <label class="field"><span>{t("setup.hardware.d0Serial")}</span><input class="input num" type="text" required value={grid.serial_number || ""} oninput={(event) => setGridEndpoint("serial_number", event.currentTarget.value)} /></label>
          <p class="muted small">{t("setup.hardware.d0Hint")}</p>
          {#if topicMode(grid, draft.features["grid_meter.mqtt.topic"]) === "manual"}<button class="btn secondary small" type="button" onclick={useGeneratedTopic}>{t("setup.hardware.useGeneratedTopic")}</button>{/if}
        {/if}
        {#if !meterIsMqtt(gridType)}
          <div class="two">
            <label class="field"><span>{t("setup.hardware.host")}</span><input class="input num" type="text" value={grid.ip || ""} oninput={(event) => setGridEndpoint("ip", event.currentTarget.value)} data-testid="setup-grid-ip" /></label>
            <label class="field"><span>{t("setup.hardware.port")}</span><input class="input num" type="number" value={grid.port ?? ""} oninput={(event) => setGridEndpoint("port", event.currentTarget.value)} /></label>
          </div>
          <p class="muted small">{#if variants[gridType]?.port_description}<CatalogText text={variants[gridType].port_description} />{:else}{t("setup.hardware.httpPort")}{/if}</p>
        {/if}
        <div class="fields">
          {#each meterFields.normal as field (field.path)}<SettingField {field} value={featureValue(draft.features, field)} onchange={(value) => setGridFeature(field.path, value)} type={field.secret ? "password" : ""} />{/each}
        </div>
        {#each ["advanced", "expert"] as level (level)}
          {#if meterFields[level].length}
            <details>
              <summary>{t(`settings.edit.level.${level}`)}</summary>
              <div class="fields">
                {#each meterFields[level] as field (field.path)}<SettingField {field} value={featureValue(draft.features, field)} onchange={(value) => setGridFeature(field.path, value)} type={field.secret ? "password" : ""} />{/each}
              </div>
            </details>
          {/if}
        {/each}
      </DeviceCard>
    {:else if setupDraft.meters.length >= 2}
      <div class="inner choose" data-testid="setup-grid-choose">
        <div class="row-wrap"><strong>{t("setup.hint.chooseGridMeter")}</strong><span class="badge num">{tn("setup.hardware.metersFound", setupDraft.meters.length)}</span></div>
        {#each setupDraft.meters as meter (observationKey(meter))}
          <div class="row-wrap"><button class="btn small" type="button" onclick={() => useGridMeter(observationKey(meter))}>{t("setup.hardware.useThis")}</button><span lang="en">{meter.display_name || meter.model || DEFAULT_GRID_METER}</span><span class="muted small num">{endpoint(meter)}</span></div>
        {/each}
      </div>
    {:else if !setupDraft.meters.length}
      <p class="muted small">{t("setup.hardware.noGridMeter")}</p>
    {/if}
  </section>

  <section class="block">
    <h3 class="sub">{t("setup.hardware.inverters")}</h3>
    <p class="muted small">{t("setup.hardware.invertersLead")}</p>
    {#each confirmations as entry (entry.token)}
      <div class="inner confirm" data-testid="setup-switch-confirmation">
        <strong>{t("setup.hardware.switchQuestion", { name: draft.items.find((item) => item.draft_item_id === entry.current_ref)?.config_name || draft.selections.find((item) => String(item.id) === String(entry.current_ref))?.config_name || t("setup.hardware.thisDevice"), from: LABELS[entry.current_source] || t("setup.hardware.unknown"), to: LABELS[entry.candidate_source] || t("setup.hardware.unknown") })}</strong>
        <p class="muted small">{CONTINUITY.includes(entry.control_continuity) ? t(`setup.continuity.${entry.control_continuity}`) : t("setup.continuity.unresolved")}</p>
        <div class="row-wrap">
          <button class="btn small" type="button" onclick={() => answerSwitch(entry.token, true)}>{t("setup.hardware.use", { source: LABELS[entry.candidate_source] || t("setup.hardware.unknown") })}</button>
          <button class="btn secondary small" type="button" onclick={() => answerSwitch(entry.token, false)}>{t("setup.hardware.keep", { source: LABELS[entry.current_source] || t("setup.hardware.unknown") })}</button>
        </div>
      </div>
    {/each}
    {#if setupDraft.notice?.kind === "switchBlocked"}
      <p class="note warn" role="alert"><Icon name="triangle-alert" />{t("setup.hardware.notSwitched", { reason: SWITCH_REASONS.includes(setupDraft.notice.reason) ? t(`setup.switchReason.${setupDraft.notice.reason}`) : t("setup.switchReason.other") })}</p>
    {/if}
    {#if cards.length}
      <div class="list" data-testid="setup-inverters">
        {#each cards as card, position (card.key)}
          {#if card.kind === "http"}
            {@const item = card.item}
            <DeviceCard id={card.key} title={t("setup.hardware.inverterN", { n: position + 1 })} model={String(item.display_name || item.model || DEFAULT_INVERTER)} meta={inverterMeta(item)} enabled={item.enabled !== false} status={item.enabled !== false ? t("setup.hardware.on") : t("setup.hardware.off")} connection="API" onremove={() => removeItem(item.source_id)}>
              {@render enabledRow(item.enabled !== false, (on) => setItemEnabled(item.source_id, on), t("setup.hardware.inverterEnabledHint"))}
              {#if levels.normal.length || levels.advanced.length || levels.expert.length}
                {@render fieldList(levels.normal, item)}
                {#each ["advanced", "expert"] as level (level)}
                  {#if levels[level].length}
                    <details>
                      <summary>{t(`settings.edit.level.${level}`)}</summary>
                      {#if level === "expert"}<p class="note warn"><Icon name="triangle-alert" />{t("setup.hardware.expertWarning")}</p>{/if}
                      {@render fieldList(levels[level], item)}
                    </details>
                  {/if}
                {/each}
              {:else}
                <p class="muted small">{t("setup.hardware.fieldsLater")}</p>
              {/if}
              <div class="row-wrap">
                <button class="icon-btn" type="button" onclick={() => moveItem(item.source_id, -1)} aria-label={t("setup.hardware.moveUp")} title={t("setup.hardware.moveUp")}><Icon name="arrow-up" size="sm" /></button>
                <button class="icon-btn" type="button" onclick={() => moveItem(item.source_id, 1)} aria-label={t("setup.hardware.moveDown")} title={t("setup.hardware.moveDown")}><Icon name="arrow-down" size="sm" /></button>
                <button class="btn secondary small" type="button" onclick={() => resetItemName(item.source_id)}>{t("setup.hardware.resetName")}</button>
              </div>
            </DeviceCard>
          {:else}
            {@const entry = card.entry}
            {@const controllable = selectionControllable(entry)}
            {@const external = isExternalProposal(entry)}
            <DeviceCard id={card.key} title={t("setup.hardware.inverterN", { n: position + 1 })} model={selectionModel(entry)} meta={[entry.config_name, identity(entry), LABELS[selectionSource(entry)]].filter(Boolean).join(" · ")} enabled={entry.enabled !== false} status={entry.enabled !== false ? t("setup.hardware.on") : t("setup.hardware.off")} connection={LABELS[selectionSource(entry)]} onremove={() => removeSelection(entry.id)}>
              {@render enabledRow(entry.enabled !== false, (on) => setSelectionEnabled(entry.id, on), t("setup.hardware.inverterEnabledHint"))}
              <label class="field"><span>{t("setup.hardware.deviceName")}</span><input class="input" type="text" value={entry.config_name || ""} oninput={(event) => setSelectionName(entry.id, event.currentTarget.value)} /></label>
              <p class="muted small">{t("setup.hardware.nameHint")}</p>
              <p class="badges"><span class="badge">{controllable ? t("setup.hardware.controlEnabled") : external ? t("setup.hardware.readOnlyNever") : t("setup.hardware.telemetryOnly")}</span><span class="muted small num">{identity(entry)}</span></p>
              <div class="row-wrap"><button class="btn secondary small" type="button" onclick={() => resetSelectionName(entry.id)}>{t("setup.hardware.resetName")}</button></div>
            </DeviceCard>
          {/if}
        {/each}
      </div>
    {:else}
      <p class="muted small">{t("setup.hardware.noInverters")}</p>
    {/if}

    <details class="add-more" data-testid="setup-add-more">
      <summary>{t("setup.hardware.addMore")} <span class="badge num">{tn("setup.hardware.ready", available.length + offered.length)}</span></summary>
      <p class="muted small">{t("setup.hardware.addMoreLead")}</p>
      {#if available.length || offered.length}
        <ul class="list">
          {#each available as found (observationKey(found))}
            {@const sourceId = observationKey(found)}
            {@const isGrid = found.role_suggestion === "grid_meter"}
            {@const added = hasSource(draft, sourceId)}
            {@const state = isGrid ? { state: added ? "active" : "new" } : candidateState(draft, index, sourceId)}
            <li class="inner candidate" data-role={isGrid ? "grid_meter" : "inverter"} data-candidate-state={state.state}>
              <div class="row-wrap">
                <span class="words"><strong>{isGrid ? t("setup.hardware.gridCandidate") : t("setup.hardware.inverterCandidate")}</strong><span class="muted small" lang="en">{found.display_name || found.device_type || t("setup.devices.device")}</span><span class="muted small num">{[endpoint(found), found.serial_number ? `SN ${found.serial_number}` : t("setup.devices.snMissing"), found.api_family, found.device_type].filter(Boolean).join(" · ")}</span></span>
                <Status tone={isReady(found) ? "c-ok" : "c-warn"} text={isReady(found) ? t("setup.hardware.readyOne") : t("setup.devices.needsInfo")} />
                {#if !isGrid}<span class="badge">API</span>{/if}
                {#snippet add()}
                  {#if added}<button class="btn secondary small" type="button" disabled>{t("setup.hardware.addedToDraft")}</button>
                  {:else}<button class="btn small" type="button" onclick={() => addDevice(sourceId, isGrid ? "grid_meter" : "inverter")} data-testid="setup-add-device">{isGrid ? t("setup.hardware.addAsGrid") : t("setup.hardware.addInverter")}</button>{/if}
                {/snippet}
                {#if isGrid}{@render add()}{:else}{@render candidateAction(state, add)}{/if}
              </div>
              {#if !isGrid}{@render candidateNote(state)}{/if}
              <p class="badges">{#each sourcesOf(found) as source (source)}<span class="badge" lang="en">{source}</span>{/each}</p>
            </li>
          {/each}
          {#each offered as proposal (proposal.id)}
            {@const state = candidateState(draft, index, proposal.id)}
            {@const source = selectionSource(proposal)}
            <li class="inner candidate" data-role="inverter" data-candidate-state={state.state}>
              <div class="row-wrap">
                <span class="words"><strong>{t("setup.hardware.inverterCandidate")}</strong><span class="muted small" lang="en">{proposal.display_name || proposal.hardware_model || DEFAULT_INVERTER}</span><span class="muted small num">{LABELS[source]} · {identity(proposal)}</span></span>
                <Status tone="c-ok" text={t("setup.hardware.readyOne")} />
                <span class="badge">{LABELS[source]}</span>
                {#snippet add()}<button class="btn small" type="button" onclick={() => addProposal(proposal.id)} data-testid="setup-add-proposal">{t("setup.hardware.addInverter")}</button>{/snippet}
                {@render candidateAction(state, add)}
              </div>
              {@render candidateNote(state)}
              <p class="muted small">{proposal.output_control_supported ? t("setup.hardware.controlSupported") : isExternalProposal(proposal) ? t("setup.devices.readOnly") : t("setup.hardware.telemetryOnly")}</p>
            </li>
          {/each}
        </ul>
      {:else}
        <p class="muted small">{t("setup.hardware.noneReady")}</p>
      {/if}

      <details class="manual">
        <summary>{t("setup.hardware.manual")}</summary>
        <p class="muted small">{t("setup.hardware.manualLead")}</p>
        <form class="form" onsubmit={addManual} data-testid="setup-manual-device">
          <label class="field"><span>{t("setup.hardware.displayName")}</span><input class="input" type="text" placeholder="SolarFlow 800 Pro" bind:value={manual.name} /></label>
          <label class="field"><span>{t("setup.hardware.role")}</span>
            <select class="input" bind:value={manual.role}><option value="inverter">{t("setup.devices.inverter")}</option><option value="grid_meter">{t("setup.devices.gridMeter")}</option></select>
          </label>
          <label class="field"><span>{t("setup.hardware.type")}</span>
            <select class="input" value={manual.type} onchange={(event) => chooseType(event.currentTarget.value)}>{#each roleVariants as variant (variant.id)}<option value={variant.id} lang={catalogText(variant.label || variant.id).lang}>{catalogText(variant.label || variant.id).text}</option>{/each}</select>
          </label>
          <label class="field"><span>{t("setup.hardware.host")}</span><input class="input num" type="text" placeholder="192.168.1.50" bind:value={manual.host} /></label>
          <label class="field"><span>{t("setup.hardware.port")}</span><input class="input num" type="text" inputmode="numeric" placeholder="80" bind:value={manual.port} /></label>
          <label class="field"><span>{t("setup.hardware.serialOptional")}</span><input class="input num" type="text" bind:value={manual.serial} /></label>
          <button class="btn small" type="submit">{t("setup.hardware.addToDraft")}</button>
        </form>
        {#if manualError}<p class="form-error">{t(`setup.manualError.${manualError}`)}</p>{/if}

        <h4 class="sub">{t("setup.hardware.broker")}</h4>
        {#if catalog?.zendure_mqtt_broker?.help}<p class="muted small"><CatalogText text={catalog.zendure_mqtt_broker.help} /></p>{/if}
        <div class="form" data-testid="setup-manual-broker">
          <label class="field"><span>{t("devices.mqttForm.name")}</span><input class="input" type="text" placeholder="local_mqtt" value={draft.broker.name || ""} oninput={(event) => setBroker("name", event.currentTarget.value)} /></label>
          <label class="field"><span>{t("setup.hardware.host")}</span><input class="input num" type="text" placeholder="192.168.1.20" value={draft.broker.host || ""} oninput={(event) => setBroker("host", event.currentTarget.value)} /></label>
          <label class="field"><span>{t("setup.hardware.port")}</span><input class="input num" type="text" inputmode="numeric" placeholder="1883" value={draft.broker.port || ""} oninput={(event) => setBroker("port", event.currentTarget.value)} /></label>
          <label class="field"><span>{t("setup.hardware.security")}</span>
            <select class="input" value={draft.broker.security || "plain"} onchange={(event) => setBroker("security", event.currentTarget.value)}><option value="plain">{t("setup.hardware.plainMqtt")}</option><option value="tls">{t("setup.hardware.tlsMqtt")}</option></select>
          </label>
          <label class="field"><span>{t("setup.hardware.usernameOptional")}</span><input class="input" type="text" autocomplete="off" value={draft.broker.username || ""} oninput={(event) => setBroker("username", event.currentTarget.value)} /></label>
          <label class="field"><span>{t("setup.hardware.passwordOptional")}</span><input class="input" type="password" autocomplete="new-password" value={setupDraft.brokerPassword} oninput={(event) => setBrokerPassword(event.currentTarget.value)} /></label>
        </div>

        <h4 class="sub">{t("setup.hardware.mqttDevice")}</h4>
        <p class="muted small">{t("setup.hardware.mqttDeviceLead")}</p>
        <form class="form" onsubmit={addMqttDevice} data-testid="setup-manual-mqtt">
          <label class="field"><span>{t("setup.hardware.deviceName")}</span><input class="input" type="text" placeholder="INV_1" bind:value={device.name} /></label>
          <label class="field"><span>{t("setup.hardware.physicalSerial")}</span><input class="input num" type="text" bind:value={device.serial} /></label>
          <label class="field"><span>{t("setup.hardware.mqttId")}</span><input class="input num" type="text" bind:value={device.routeId} /></label>
          <label class="field"><span>{t("setup.hardware.generation")}</span>
            <select class="input" value={generation?.id || ""} onchange={(event) => chooseGeneration(event.currentTarget.value)}>{#each generations(catalog) as entry (entry.id)}<option value={entry.id} lang={catalogText(entry.label || entry.id).lang}>{catalogText(entry.label || entry.id).text}</option>{/each}</select>
          </label>
          <label class="field"><span>{t("setup.hardware.model")}</span>
            <select class="input" bind:value={device.model}>{#each models as entry (entry.id || "unknown")}<option value={entry.id || ""} lang={entry.label || entry.id ? catalogText(entry.label || entry.id).lang : undefined}>{entry.label || entry.id ? catalogText(entry.label || entry.id).text : t("setup.hardware.unknownModel")}</option>{/each}</select>
          </label>
          {#if control.supported || generation?.product_key}<label class="field"><span>{t("setup.hardware.productKey")}</span><input class="input num" type="text" bind:value={device.productKey} /></label>{/if}
          <button class="btn small" type="submit">{t("setup.hardware.addMqttDevice")}</button>
        </form>
        <p class="muted small">{t("setup.hardware.mqttIdHint")}</p>
        <p class="muted small" data-testid="setup-manual-control">{control.status === "not_ready" ? t("setup.manualControl.not_ready", { missing: t(`setup.manualControl.missing.${control.missing}`) }) : control.status === "source_unavailable" ? t("setup.manualControl.source_unavailable", { reason: t(reasonKey("broker_source_write_unverified")) }) : control.status === "unavailable" ? t("setup.manualControl.unavailable", { reason: t(reasonKey("hardware_profile_deferred")) }) : t(`setup.manualControl.${control.status}`)}</p>
        {#if generation?.description}<p class="muted small"><CatalogText text={generation.description} /></p>{/if}
        <p class="muted small">{model?.id ? t("setup.hardware.modelFacts", { profile: model.power_write_profile || "—", maturity: model.validation_maturity ? say(maturityWords(model.validation_maturity)) : "—", operations: operationWords(model.supported_operations).map(say).join(", ") || t("setup.hardware.telemetryOnly") }) : t("setup.hardware.unknownHardware")}</p>
        {#if deviceError}<p class="form-error">{t(`setup.manualError.${deviceError}`)}</p>{/if}
        {#if draft.manual.length}
          <ul class="list">
            {#each draft.manual as entry, position (`${entry.serial_number}-${position}`)}
              <li class="inner row-wrap"><strong class="grow">{entry.name || entry.serial_number || t("discovery.mqttDevice")}</strong><span class="muted small"><CatalogText text={generationLabel(entry.hardware_generation || entry.generation || "")} /> · <CatalogText text={modelLabel(entry.hardware_model || entry.power_hardware_profile || "")} /></span><button class="btn secondary small" type="button" onclick={() => removeManualDevice(position)}>{t("devices.remove")}</button></li>
            {/each}
          </ul>
        {/if}
      </details>
    </details>
  </section>
</div>

<style>
  .hardware { display: flex; flex-direction: column; gap: var(--s5); }
  .block { display: flex; flex-direction: column; gap: var(--s3); min-width: 0; }
  .block p, .manual p, .add-more p { margin: 0; }
  .sub { font-size: 13px; font-weight: 620; margin: 0; }
  .small { font-size: 12.5px; overflow-wrap: anywhere; }
  .list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--s2); }
  .fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--s3) var(--s4); }
  .two { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--s3); }
  .field { display: flex; flex-direction: column; gap: 4px; font-size: 13px; min-width: 0; }
  .check { display: flex; align-items: center; gap: var(--s2); font-weight: 560; }
  .choose, .confirm, .candidate { display: flex; flex-direction: column; gap: var(--s2); }
  .row-wrap { gap: var(--s2); align-items: center; }
  .words { flex: 1 1 220px; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
  .grow { flex: 1 1 160px; min-width: 0; }
  .badges { display: flex; flex-wrap: wrap; gap: var(--s2); align-items: center; }
  .form { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--s3); align-items: end; }
  details > summary { cursor: pointer; font-weight: 560; }
  details[open] > summary { margin-bottom: var(--s2); }
  details.add-more, details.manual { display: flex; flex-direction: column; gap: var(--s3); }
  @container content (max-width: 720px) { .fields, .two, .form { grid-template-columns: minmax(0, 1fr); } }
</style>
