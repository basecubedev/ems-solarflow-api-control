<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import ConfirmDialog from "../../../shared/ui/ConfirmDialog.svelte";
  import MqttDeviceWizard from "./MqttDeviceWizard.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { BUILTIN_BROKER, LIVE_LABELS, MAPPING_ISSUES, SILENCE_REASONS, TARGETS, VALUE_REASONS, newDevice, newSource, proposedId, targetSources } from "../lib/mqtt.js";
  import { mqtt, loadMapping, startTopics, stopTopics, previewMapping, applyMapping, previewValue, watchTopics } from "../state/mqtt.svelte.js";

  /**
   * adoption: ``{ state(id) -> "saved" | "draft" | null, add(id) -> Promise<boolean> }``
   * from the page this sits on; it adds a published device through the
   * existing external-device adoption of that page's draft.
   */
  let { adoption = null } = $props();

  let brokerRef = $state(BUILTIN_BROKER);
  let draft = $state.raw(null);
  let editingIndex = $state(-1);
  let target = $state("inverterPower");
  let manualTopic = $state("");
  let manualPath = $state("");
  let issues = $state.raw([]);
  let evaluation = $state.raw(null);
  let confirming = $state.raw(null);
  let wizard = $state.raw(null);
  let adopting = $state("");
  let notFound = $state("");

  const view = $derived(mqtt.mapping);
  const devices = $derived(view?.document?.devices || []);
  const statusById = $derived(Object.fromEntries((view?.status?.devices || []).map((item) => [item.id, item])));
  const groups = $derived(mqtt.topics?.groups || []);
  const busy = $derived(Boolean(mqtt.busy));

  $effect(() => {
    loadMapping();
    return () => stopTopics();
  });

  $effect(() => (mqtt.session ? watchTopics() : undefined));

  $effect(() => {
    const group = mqtt.setup;
    if (!group) return;
    mqtt.setup = null;
    startDevice(group);
  });

  async function adopt(id) {
    adopting = id;
    notFound = "";
    try {
      if (!(await adoption?.add(id))) notFound = id;
    } finally {
      adopting = "";
    }
  }

  async function wizardPreview(device) {
    const answer = await previewValue($state.snapshot(device));
    issues = answer?.issues || mqtt.error?.payload?.issues || [];
    const result = answer?.evaluation || null;
    return result ? { ...result, reasonText: reasonText(result.reason) } : null;
  }

  function correct(key) {
    const spec = draft.values[key];
    if (!spec?.sources) return;
    setTarget(key, { ...spec, sources: spec.sources.map((source) => ({ ...source, invert: !source.invert, negative: "zero" })) });
    evaluation = null;
  }

  function issueText(issue) {
    return MAPPING_ISSUES.includes(issue.code) ? t(`mqtt.code.${issue.code}`) : issue.message;
  }

  function reasonText(reason) {
    if (SILENCE_REASONS.includes(reason)) return t(`mqtt.reason.${reason}`);
    if (VALUE_REASONS.includes(reason)) return t(`mqtt.value.${reason}`);
    return reason || "";
  }

  function brokerLabel(ref) {
    return ref === BUILTIN_BROKER ? t("mqtt.mapping.builtin") : ref;
  }

  function edit(index) {
    editingIndex = index;
    draft = structuredClone($state.snapshot(devices[index]));
    target = "inverterPower";
    evaluation = null;
    issues = [];
  }

  function startDevice(group) {
    editingIndex = -1;
    draft = null;
    wizard = { group, brokerRef: mqtt.topics?.broker_ref || brokerRef, at: Date.now() };
    target = "inverterPower";
    evaluation = null;
    issues = [];
  }

  function freeEditor(device) {
    wizard = null;
    editingIndex = -1;
    draft = { ...newDevice(device.broker_ref || brokerRef), ...device, id: device.id || proposedId("") };
  }

  function update(change) {
    draft = { ...draft, ...change };
  }

  function setTarget(key, spec) {
    const values = { ...draft.values };
    if (spec) values[key] = spec;
    else delete values[key];
    update({ values });
  }

  function addSource(topic, path, side = null) {
    if (!draft || !topic) return;
    const current = draft.values[target];
    const source = newSource(topic, path);
    if (target === "batteryPower" && current && !Array.isArray(current.sources)) {
      const key = side || "charge";
      setTarget(target, { ...current, [key]: [...(current[key] || []), source] });
    } else {
      setTarget(target, { sources: [...(current?.sources || []), source] });
    }
  }

  function changeSource(key, side, index, change) {
    const spec = draft.values[key];
    const list = [...spec[side]];
    list[index] = { ...list[index], ...change };
    if (change.format === "number") delete list[index].path;
    setTarget(key, { ...spec, [side]: list });
  }

  function removeSource(key, side, index) {
    const spec = draft.values[key];
    setTarget(key, { ...spec, [side]: spec[side].filter((_, position) => position !== index) });
  }

  function splitBattery(split) {
    setTarget("batteryPower", split ? { charge: [], discharge: [] } : { sources: [] });
  }

  function documentWith(device, index) {
    const list = [...devices];
    if (device === null) list.splice(index, 1);
    else if (index >= 0) list[index] = device;
    else list.push(device);
    return { ...(view?.document || { schema_version: 1 }), schema_version: 1, devices: list };
  }

  async function preview() {
    const answer = await previewValue($state.snapshot(draft));
    issues = answer?.issues || mqtt.error?.payload?.issues || [];
    evaluation = answer?.evaluation || null;
  }

  async function review(device, index) {
    const document = documentWith(device, index);
    const answer = await previewMapping(document);
    issues = answer?.issues || [];
    if (answer?.ok) confirming = { document, removing: device === null, id: device?.id || devices[index]?.id };
  }

  async function apply() {
    const pending = confirming;
    confirming = null;
    const answer = await applyMapping(pending.document);
    issues = answer?.issues || mqtt.error?.payload?.issues || [];
    if (answer?.ok) {
      draft = null;
      wizard = null;
      editingIndex = -1;
    }
  }

  function numberOf(event) {
    const value = Number(event.currentTarget.value);
    return Number.isFinite(value) ? value : event.currentTarget.value;
  }
</script>

<div class="mapping" data-testid="mqtt-mapping">
  <p class="muted small">{t("mqtt.mapping.lead")}</p>
  <div class="row-wrap">
    <label class="field">
      <span class="small muted">{t("mqtt.mapping.broker")}</span>
      <select class="input" bind:value={brokerRef} disabled={Boolean(mqtt.session)} data-testid="mqtt-mapping-broker">
        {#each view?.broker_refs || [BUILTIN_BROKER] as ref (ref)}<option value={ref}>{brokerLabel(ref)}</option>{/each}
      </select>
    </label>
    {#if mqtt.session}
      <button class="btn secondary small" type="button" onclick={stopTopics} data-testid="mqtt-topics-stop"><Icon name="square" size="sm" />{t("mqtt.topics.stop")}</button>
    {:else}
      <button class="btn small" type="button" onclick={() => startTopics(brokerRef)} disabled={busy} data-testid="mqtt-topics-start"><Icon name="radio-tower" size="sm" />{t("mqtt.topics.start")}</button>
    {/if}
    <button class="btn secondary small" type="button" onclick={() => startDevice(null)} disabled={busy} data-testid="mqtt-new-device"><Icon name="plus" size="sm" />{t("mqtt.mapping.newDevice")}</button>
  </div>
  {#if mqtt.error?.name === "topics" && mqtt.error.payload?.error === "mqtt_broker_off"}
    <p class="note warn"><Icon name="triangle-alert" />{t("mqtt.topics.brokerOff")}</p>
  {/if}

  {#if devices.length}
    <h3 class="sub">{t("mqtt.mapping.devices")}</h3>
    <ul class="list" data-testid="mqtt-mapped-devices">
      {#each devices as device, index (device.id)}
        {@const status = statusById[device.id]}
        <li class="inner row-wrap">
          <strong>{device.label || device.id}</strong>
          <span class="muted small">{device.id} · {brokerLabel(device.broker_ref)}</span>
          {#if status}
            <Status tone={status.publishing ? "c-ok" : "c-warn"} text={status.publishing ? t("mqtt.mapping.publishing") : reasonText(status.reason)} />
          {/if}
          {#if adoption && adoption.state(device.id) === "saved"}
            <span class="muted small" data-testid="mqtt-adopted">{t("mqtt.mapping.inSystem")}</span>
          {:else if adoption && adoption.state(device.id) === "draft"}
            <span class="muted small" data-testid="mqtt-adopted">{t("mqtt.mapping.adopted")}</span>
          {:else if adoption && status?.publishing}
            <button class="btn small" type="button" onclick={() => adopt(device.id)} disabled={busy || Boolean(adopting)} data-testid="mqtt-adopt"><Icon name={adopting === device.id ? "loader" : "plus"} size="sm" />{t("mqtt.mapping.adopt")}</button>
          {/if}
          {#if notFound === device.id}<span class="note warn small">{t("mqtt.mapping.adoptNotFound")}</span>{/if}
          <button class="btn secondary small" type="button" onclick={() => edit(index)} disabled={busy}><Icon name="wrench" size="sm" />{t("mqtt.mapping.edit")}</button>
          <button class="btn secondary small" type="button" onclick={() => review(null, index)} disabled={busy}><Icon name="trash-2" size="sm" />{t("devices.remove")}</button>
        </li>
      {/each}
    </ul>
  {/if}

  {#if wizard}
    {#key wizard.at}
      <MqttDeviceWizard
        group={wizard.group}
        brokerRef={wizard.brokerRef}
        takenIds={devices.map((item) => item.id)}
        {busy}
        onpreview={wizardPreview}
        onsave={(device) => review($state.snapshot(device), -1)}
        onfree={freeEditor}
        oncancel={() => (wizard = null)}
      />
    {/key}
  {/if}

  {#if draft}
    <section class="editor inner" data-testid="mqtt-mapping-editor">
      <h3 class="sub">{editingIndex >= 0 ? t("mqtt.mapping.editTitle", { id: draft.id }) : t("mqtt.mapping.newTitle")}</h3>
      <div class="row-wrap">
        <label class="field"><span class="small muted">{t("mqtt.mapping.id")}</span><input class="input" value={draft.id} oninput={(event) => update({ id: event.currentTarget.value })} data-testid="mqtt-device-id" /></label>
        <label class="field"><span class="small muted">{t("mqtt.mapping.label")}</span><input class="input" value={draft.label || ""} oninput={(event) => update({ label: event.currentTarget.value })} /></label>
        <label class="check"><input type="checkbox" checked={draft.enabled !== false} onchange={(event) => update({ enabled: event.currentTarget.checked })} /><span>{t("mqtt.mapping.enabled")}</span></label>
      </div>
      <div class="row-wrap" role="group" aria-label={t("mqtt.mapping.targets")}>
        {#each TARGETS as key (key)}
          <button class="btn small" class:secondary={target !== key} type="button" onclick={() => (target = key)} aria-pressed={target === key ? "true" : "false"}>
            {t(`mqtt.target.${key}`)}{draft.values[key] ? ` (${targetSources(draft.values[key]).length})` : ""}
          </button>
        {/each}
      </div>
      {#if target === "batteryPower"}
        <label class="check"><input type="checkbox" checked={Boolean(draft.values.batteryPower && !Array.isArray(draft.values.batteryPower.sources))} onchange={(event) => splitBattery(event.currentTarget.checked)} /><span>{t("mqtt.mapping.split")}</span></label>
      {/if}
      {#each TARGETS.filter((key) => draft.values[key]) as key (key)}
        {#each targetSources(draft.values[key]) as [side, index, source] (`${key}-${side}-${index}`)}
          <div class="source" class:hidden={key !== target}>
            <span class="small muted">{side === "sources" ? t(`mqtt.target.${key}`) : side === "charge" ? t("mqtt.mapping.charge") : t("mqtt.mapping.discharge")}</span>
            <code class="topic">{source.topic}{source.path ? ` → ${source.path}` : ""}</code>
            <label class="field"><span class="small muted">{t("mqtt.mapping.factor")}</span><input class="input narrow" type="number" step="any" value={source.factor ?? 1} onchange={(event) => changeSource(key, side, index, { factor: numberOf(event) })} /></label>
            <label class="check"><input type="checkbox" checked={Boolean(source.invert)} onchange={(event) => changeSource(key, side, index, { invert: event.currentTarget.checked })} data-testid="mqtt-source-invert" /><span>{t("mqtt.mapping.invert")}</span></label>
            <label class="check"><input type="checkbox" checked={source.negative === "zero"} onchange={(event) => changeSource(key, side, index, { negative: event.currentTarget.checked ? "zero" : "keep" })} data-testid="mqtt-source-negative" /><span>{t("mqtt.mapping.negativeZero")}</span></label>
            <label class="field"><span class="small muted">{t("mqtt.mapping.maxAge")}</span><input class="input narrow" type="number" min="10" max="900" value={source.max_age_seconds ?? 60} onchange={(event) => changeSource(key, side, index, { max_age_seconds: numberOf(event) })} /></label>
            <button class="icon-btn" type="button" onclick={() => removeSource(key, side, index)} aria-label={t("devices.remove")} title={t("devices.remove")}><Icon name="trash-2" /></button>
          </div>
        {/each}
      {/each}
      <form class="row-wrap" onsubmit={(event) => { event.preventDefault(); addSource(manualTopic.trim(), manualPath.trim()); manualTopic = manualPath = ""; }}>
        <input class="input" placeholder={t("mqtt.mapping.topic")} aria-label={t("mqtt.mapping.topic")} bind:value={manualTopic} data-testid="mqtt-source-topic" />
        <input class="input" placeholder={t("mqtt.mapping.path")} aria-label={t("mqtt.mapping.path")} bind:value={manualPath} data-testid="mqtt-source-path" />
        <button class="btn secondary small" type="submit" disabled={!manualTopic.trim()} data-testid="mqtt-source-add"><Icon name="plus" size="sm" />{t("mqtt.mapping.addSource")}</button>
        {#if target !== "inverterPower" && draft.values[target]}
          <button class="btn secondary small" type="button" onclick={() => setTarget(target, null)}><Icon name="x" size="sm" />{t("mqtt.mapping.dropTarget")}</button>
        {/if}
      </form>
      <div class="row-wrap">
        <button class="btn secondary small" type="button" onclick={preview} disabled={busy} data-testid="mqtt-device-preview"><Icon name="eye" size="sm" />{t("mqtt.mapping.preview")}</button>
        <button class="btn small" type="button" onclick={() => review($state.snapshot(draft), editingIndex)} disabled={busy || !draft.id} data-testid="mqtt-device-save"><Icon name="save" size="sm" />{t("mqtt.mapping.save")}</button>
        <button class="btn secondary small" type="button" onclick={() => (draft = null)}>{t("mqtt.mapping.cancel")}</button>
      </div>
      {#if evaluation}
        {#if evaluation.state}
          <p class="note ok" data-testid="mqtt-preview-state"><Icon name="circle-check" />{t("mqtt.mapping.wouldPublish")} <code>{JSON.stringify(evaluation.state)}</code></p>
        {:else}
          <p class="note warn" data-testid="mqtt-preview-silent"><Icon name="triangle-alert" />{t("mqtt.mapping.wouldNotPublish", { reason: reasonText(evaluation.reason) })}</p>
          {#each evaluation.targets.filter((item) => item.reason === "catalog_refused") as item (item.key)}
            <p class="muted small row-wrap">
              {t("mqtt.mapping.refusedValue", { target: t(`mqtt.target.${item.key}`), value: item.value })}
              {#if Number(item.value) < 0 && draft.values[item.key]?.sources}
                <button class="btn secondary small" type="button" onclick={() => correct(item.key)} data-testid="mqtt-correct">{t("mqtt.mapping.correct")}</button>
              {/if}
            </p>
          {/each}
        {/if}
      {/if}
    </section>
  {/if}

  {#each issues as issue, index (`${issue.code}-${index}`)}
    <p class="form-error"><Icon name="triangle-alert" size="sm" /><span lang={MAPPING_ISSUES.includes(issue.code) ? undefined : "en"}>{issueText(issue)}</span>{#if issue.path}<code class="small">{issue.path}</code>{/if}</p>
  {/each}

  {#if mqtt.topics}
    <h3 class="sub">{t("mqtt.topics.title")}</h3>
    {#if mqtt.topics.truncated}<p class="note warn"><Icon name="triangle-alert" />{t("mqtt.topics.truncated")}</p>{/if}
    {#if !groups.length}<p class="muted small">{t("mqtt.topics.waiting")}</p>{/if}
    <ul class="list" data-testid="mqtt-topic-groups">
      {#each groups as group (`${group.label}/${group.key}`)}
        <li class="inner group" data-label={group.label} data-key={group.key}>
          <div class="row-wrap">
            <Status tone={group.label === "unknown" ? "c-idle" : group.mappable ? "c-inv" : "c-pv"} text={LIVE_LABELS.includes(group.label) ? t(`mqtt.label.${group.label}`) : group.label} />
            <strong>{group.key}</strong>
            {#if group.mappable}
              <button class="btn secondary small" type="button" onclick={() => startDevice(group)} disabled={busy} data-testid="mqtt-map-group"><Icon name="plus" size="sm" />{t("mqtt.topics.mapGroup")}</button>
            {:else}
              <span class="muted small">{group.label === "ems_output" ? t("mqtt.topics.output") : t("mqtt.topics.native")}</span>
            {/if}
          </div>
          {#each group.topics as topic (topic.topic)}
            <div class="topic-row">
              <code class="topic">{topic.topic}</code>
              <span class="muted small">{t("mqtt.topics.rate", { rate: topic.messages_per_minute })}{topic.retained ? ` · ${t("mqtt.topics.retained")}` : ""}</span>
              {#each topic.values as value (`${value.path}`)}
                <div class="value-row">
                  <span class:likely={Boolean(value.likely)}>{value.path || t("mqtt.topics.whole")}</span>
                  <strong>{value.value}</strong>
                  {#if value.mapped_to.length}<span class="muted small">{t("mqtt.topics.mappedTo", { ids: value.mapped_to.join(", ") })}</span>{/if}
                  {#if draft && group.mappable}
                    <button class="btn secondary small" type="button" onclick={() => addSource(topic.topic, value.path)} data-testid="mqtt-use-value">{t("mqtt.topics.useAs", { target: t(`mqtt.target.${target}`) })}</button>
                  {/if}
                </div>
              {/each}
            </div>
          {/each}
        </li>
      {/each}
    </ul>
  {/if}
  <p class="muted small">{t("mqtt.mapping.adoptHint")}</p>
</div>

<ConfirmDialog
  open={Boolean(confirming)}
  title={confirming?.removing ? t("mqtt.mapping.removeTitle") : t("mqtt.mapping.saveTitle")}
  lines={confirming?.removing ? [t("mqtt.mapping.removeLine", { id: confirming.id })] : [t("mqtt.mapping.saveLine"), t("mqtt.mapping.saveNoRestart")]}
  confirmLabel={confirming?.removing ? t("devices.remove") : t("mqtt.mapping.save")}
  danger={Boolean(confirming?.removing)}
  onconfirm={apply}
  oncancel={() => (confirming = null)}
/>

<style>
  .mapping { display: grid; gap: var(--s3); }
  .list { list-style: none; margin: 0; padding: 0; display: grid; gap: var(--s2); }
  .editor { display: grid; gap: var(--s2); }
  .source { display: flex; flex-wrap: wrap; gap: var(--s2); align-items: center; }
  .source.hidden { display: none; }
  .field { display: grid; gap: 2px; }
  .narrow { width: 6rem; }
  .check { display: inline-flex; gap: var(--s1); align-items: center; }
  .group { display: grid; gap: var(--s2); }
  .topic-row { display: grid; gap: var(--s1); }
  .value-row { display: flex; flex-wrap: wrap; gap: var(--s2); align-items: center; padding-left: var(--s3); }
  .topic { overflow-wrap: anywhere; }
  .likely { font-weight: 600; }
</style>
