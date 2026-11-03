<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import ConfirmDialog from "../../../shared/ui/ConfirmDialog.svelte";
  import Dialog from "../../../shared/ui/Dialog.svelte";
  import MqttDeviceWizard from "./MqttDeviceWizard.svelte";
  import MqttBrokerChooser from "./MqttBrokerChooser.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { ADOPT_REFUSALS, BUILTIN_BROKER, LIVE_LABELS, MAPPING_EDITOR_ISSUES, MAPPING_ISSUES, MAPPING_STEPS, SILENCE_REASONS, TARGETS, VALUE_REASONS, brokerAddress, mappingStep, newDevice, newSource, proposedId, targetSources } from "../lib/mqtt.js";
  import { mqtt, loadMapping, startTopics, stopTopics, previewMapping, applyMapping, previewValue, watchMapping, watchTopics } from "../state/mqtt.svelte.js";
  import { errorView } from "../lib/errors.js";

  /**
   * adoption: what the page this sits on does with a mapped device's EMS device,
   * which is the same thing to the reader: ``{ direct, busy, state(device,
   * connection) -> "saved" | "draft" | "elsewhere" | null, name(device),
   * add(device), renamable?(device, label), rename?(device, label),
   * remove?(device) }``. ``direct`` says a change is saved at once rather than
   * joining changes that wait; ``elsewhere`` is the same id read from another
   * broker; each action answers ``{ state, error }``.
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
  let outcome = $state.raw({});
  let alsoEms = $state(true);

  const view = $derived(mqtt.mapping);
  const devices = $derived(view?.document?.devices || []);
  const statusById = $derived(Object.fromEntries((view?.status?.devices || []).map((item) => [item.id, item])));
  const groups = $derived(mqtt.topics?.groups || []);
  const busy = $derived(Boolean(mqtt.busy));
  const choices = $derived(Object.fromEntries((mqtt.brokers?.brokers || []).map((item) => [item.ref, item])));
  const readable = $derived(Boolean(choices[brokerRef]?.usable));
  const placeOf = (device) => (adoption && device ? adoption.state(device, view?.ems_connections?.[device.id]) : null);
  const inEms = $derived(devices.filter((device) => ["saved", "draft"].includes(placeOf(device))).length);
  const working = $derived(busy || Boolean(adopting) || Boolean(adoption?.busy));
  const gone = $derived(Object.entries(outcome).filter(([id, result]) => refused(result) && !devices.some((device) => device.id === id)));
  const step = $derived(mappingStep({ readable, groups: groups.length, devices: devices.length, inEms }));
  const removingFromEms = $derived(Boolean(confirming?.removing && confirming.inEms && adoption?.remove));

  $effect(() => {
    loadMapping();
    return () => stopTopics();
  });

  $effect(() => (mqtt.session ? watchTopics() : undefined));

  $effect(() => watchMapping());

  $effect(() => {
    const group = mqtt.setup;
    if (!group) return;
    mqtt.setup = null;
    startDevice(group);
  });

  async function adopt(device) {
    await adopted(device.id, () => adoption.add(device));
  }

  /** Runs an EMS change for mapped device ``id`` and keeps its answer; a refused ``retryable`` change can be tried again. */
  async function adopted(id, action, retryable = false) {
    adopting = id;
    try {
      const result = await action();
      noteOutcome(id, retryable && refused(result) ? { ...result, retry: action } : result);
    } finally {
      adopting = "";
    }
  }

  function noteOutcome(id, result) {
    const { [id]: _previous, ...others } = outcome;
    outcome = result ? { ...others, [id]: result } : others;
  }

  function refused(result) {
    return ADOPT_REFUSALS.includes(result?.state) || result?.state === "failed";
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
    if (MAPPING_EDITOR_ISSUES.includes(issue.code)) return t(`mqtt.code.${issue.code}`, { ...issue.params, where: whereText(issue.params?.where) });
    return MAPPING_ISSUES.includes(issue.code) ? t(`mqtt.code.${issue.code}`) : issue.message;
  }

  /** The broker an id already comes from: the built-in one by name, any other by its address. */
  function whereText(where) {
    return where === "builtin" ? t("mqtt.mapping.builtin") : where || "";
  }

  function reasonText(reason) {
    if (SILENCE_REASONS.includes(reason)) return t(`mqtt.reason.${reason}`);
    if (VALUE_REASONS.includes(reason)) return t(`mqtt.value.${reason}`);
    return reason || "";
  }

  function brokerLabel(ref) {
    if (ref === BUILTIN_BROKER) return t("mqtt.mapping.builtin");
    return brokerAddress(choices[ref]) || ref;
  }

  function remembers(document) {
    const refs = new Set((document?.devices || []).map((device) => device.broker_ref));
    return [...refs].filter((ref) => choices[ref]?.kind === "found").map((ref) => brokerLabel(ref));
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

  /**
   * Checks the document a save or a removal makes and asks first, saying what
   * happens to the EMS device too: a saved device joins the EMS unless it is
   * there, a new name renames it, and a removal can take it out. Only a name
   * changed here renames: an EMS device named on purpose keeps its name.
   */
  async function review(device, index) {
    const before = index >= 0 ? devices[index] : null;
    const place = placeOf(before);
    const known = !before && adoption && device ? adoption.state(device, undefined) : null;
    const document = documentWith(device, index);
    const answer = await previewMapping(document);
    issues = answer?.issues || [];
    if (!answer?.ok) return;
    const there = place === "saved" || place === "draft";
    const label = String(device?.label || "").trim();
    confirming = {
      document,
      device,
      before,
      removing: device === null,
      id: device?.id || before?.id,
      inEms: there,
      emsName: place || known ? adoption.name(before || device) || (before || device).label || (before || device).id : "",
      adds: Boolean(adoption && device && device.enabled !== false && !there),
      moves: place === "elsewhere" || Boolean(known),
      renames: Boolean(there && device && label && label !== String(before?.label || "").trim() && adoption.renamable?.(before, label)),
    };
    alsoEms = true;
  }

  async function apply() {
    const pending = confirming;
    confirming = null;
    const answer = await applyMapping(pending.document);
    issues = answer?.issues || mqtt.error?.payload?.issues || [];
    if (!answer?.ok) return;
    draft = null;
    wizard = null;
    editingIndex = -1;
    noteOutcome(pending.id, null);
    if (pending.removing && pending.inEms && alsoEms && adoption?.remove) await adopted(pending.id, () => adoption.remove(pending.before));
    else if (pending.adds) await adopt(pending.device);
    else if (pending.renames) await adopted(pending.id, () => adoption.rename(pending.before, pending.device.label), true);
  }

  function saveLines(pending) {
    const lines = [t("mqtt.mapping.saveLine"), t("mqtt.mapping.saveNoRestart")];
    if (pending.moves) lines.push(t("mqtt.mapping.saveMoves", { name: pending.emsName }));
    if (pending.adds && (!pending.moves || !adoption.direct)) lines.push(adoption.direct ? t("mqtt.mapping.saveAdds") : t("mqtt.mapping.saveJoins"));
    if (pending.renames) lines.push(t("mqtt.mapping.saveRenames", { from: pending.emsName, to: String(pending.device.label).trim() }));
    return [...lines, ...remembers(pending.document).map((broker) => t("mqtt.mapping.saveRemembers", { broker }))];
  }

  function numberOf(event) {
    const value = Number(event.currentTarget.value);
    return Number.isFinite(value) ? value : event.currentTarget.value;
  }
</script>

{#snippet refusal(result)}
  {#if ADOPT_REFUSALS.includes(result.state)}
    {t(`mqtt.mapping.adopt.${result.state}`)}
  {:else}
    {@const view = errorView(result.error, t)}
    {t("mqtt.mapping.adoptFailed")} <span lang={view.lang}>{view.text}</span>
  {/if}
{/snippet}

<div class="mapping" data-testid="mqtt-mapping">
  <p class="muted small">{t("mqtt.mapping.lead")}</p>
  <ol class="flow" aria-label={t("mqtt.step.label")} data-testid="mqtt-steps">
    {#each MAPPING_STEPS as name, index (name)}
      <li class:done={index < step} aria-current={index === step ? "step" : undefined} data-step={name}>
        <Icon name={index < step ? "circle-check" : index === step ? "chevron-right" : "circle"} size="sm" />
        <span>{t(`mqtt.step.${name}`)}</span>
      </li>
    {/each}
  </ol>
  <MqttBrokerChooser bind:value={brokerRef} locked={Boolean(mqtt.session)} />
  <div class="row-wrap">
    {#if mqtt.session}
      <button class="btn secondary small" type="button" onclick={stopTopics} data-testid="mqtt-topics-stop"><Icon name="square" size="sm" />{t("mqtt.topics.stop")}</button>
    {:else}
      <button class="btn small" type="button" onclick={() => startTopics(brokerRef)} disabled={busy || !readable} data-testid="mqtt-topics-start"><Icon name="radio-tower" size="sm" />{t("mqtt.topics.start")}</button>
    {/if}
    <button class="btn secondary small" type="button" onclick={() => startDevice(null)} disabled={busy} data-testid="mqtt-new-device"><Icon name="plus" size="sm" />{t("mqtt.mapping.newDevice")}</button>
  </div>
  {#if mqtt.error?.name === "topics" && mqtt.error.payload?.error === "mqtt_broker_off"}
    <p class="note warn"><Icon name="triangle-alert" />{t("mqtt.topics.brokerOff")}</p>
  {/if}

  {#each gone as [id, result] (id)}
    <p class="note warn small" data-testid="mqtt-remove-failed">{t("mqtt.mapping.removeFailed", { id })}{#if result.state === "failed"}{" "}{@render refusal(result)}{/if}</p>
  {/each}

  {#if devices.length}
    <h3 class="sub">{t("mqtt.mapping.devices")}</h3>
    <ul class="list" data-testid="mqtt-mapped-devices">
      {#each devices as device, index (device.id)}
        {@const status = statusById[device.id]}
        {@const there = placeOf(device)}
        {@const result = outcome[device.id]}
        <li class="inner row-wrap" data-id={device.id}>
          <strong>{device.label || device.id}</strong>
          <span class="muted small">{device.id} · {brokerLabel(device.broker_ref)}</span>
          {#if status}
            <Status tone={status.publishing ? "c-ok" : "c-warn"} text={status.publishing ? t("mqtt.mapping.publishing") : reasonText(status.reason)} />
          {:else if device.enabled !== false}
            <Status tone="c-idle" text={t("mqtt.mapping.firstValue")} />
          {/if}
          {#if there === "saved"}
            <span class="muted small" data-testid="mqtt-adopted">{t("mqtt.mapping.inSystem")}</span>
          {:else if there === "draft"}
            <span class="muted small" data-testid="mqtt-adopted">{t("mqtt.mapping.adopted")}</span>
          {:else if there === "elsewhere"}
            <span class="muted small" data-testid="mqtt-elsewhere">{t("mqtt.mapping.elsewhere")}</span>
          {/if}
          {#if adoption && device.enabled !== false && there !== "saved" && there !== "draft"}
            <button class="btn small" type="button" onclick={() => adopt(device)} disabled={working} data-testid="mqtt-adopt"><Icon name={adopting === device.id ? "loader" : "plus"} size="sm" />{there === "elsewhere" ? t("mqtt.mapping.move") : t("mqtt.mapping.adopt")}</button>
          {/if}
          {#if adopting === device.id}<span class="muted small" data-testid="mqtt-adopting">{t("mqtt.mapping.adopting")}</span>{/if}
          {#if refused(result)}
            <span class="note warn small" data-testid="mqtt-adopt-failed">{@render refusal(result)}</span>
            {#if result.retry}<button class="btn secondary small" type="button" onclick={() => adopted(device.id, result.retry, true)} disabled={working} data-testid="mqtt-adopt-retry"><Icon name="rotate-ccw" size="sm" />{t("mqtt.mapping.retry")}</button>{/if}
          {/if}
          <button class="btn secondary small" type="button" onclick={() => edit(index)} disabled={working}><Icon name="wrench" size="sm" />{t("mqtt.mapping.edit")}</button>
          <button class="btn secondary small" type="button" onclick={() => review(null, index)} disabled={working}><Icon name="trash-2" size="sm" />{t("devices.remove")}</button>
        </li>
      {/each}
    </ul>
  {/if}

  {#if wizard}
    {#key wizard.at}
      <MqttDeviceWizard
        group={wizard.group}
        brokerRef={wizard.brokerRef}
        takenIds={[...devices.map((item) => item.id), ...(view?.taken_ids?.[wizard.brokerRef] || [])]}
        busy={working}
        saveLabel={adoption ? t("mqtt.mapping.saveAndAdd") : ""}
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
        <label class="field"><span class="small muted">{t("mqtt.mapping.id")}</span><input class="input" value={draft.id} oninput={(event) => update({ id: event.currentTarget.value })} readonly={editingIndex >= 0} title={editingIndex >= 0 ? t("mqtt.mapping.idFixed") : undefined} data-testid="mqtt-device-id" /></label>
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
        <button class="btn small" type="button" onclick={() => review($state.snapshot(draft), editingIndex)} disabled={working || !draft.id} data-testid="mqtt-device-save"><Icon name="save" size="sm" />{adoption && draft.enabled !== false && !["saved", "draft"].includes(placeOf(devices[editingIndex])) ? t("mqtt.mapping.saveAndAdd") : t("mqtt.mapping.save")}</button>
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
          {#if group.also_on?.length}
            <p class="note warn small" data-testid="mqtt-topic-also-on"><Icon name="triangle-alert" size="sm" />{t("mqtt.topics.alsoOn", { brokers: group.also_on.map(whereText).join(", ") })}</p>
          {/if}
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
  {#if adoption}<p class="muted small">{t("mqtt.mapping.adoptHint")}</p>{/if}
</div>

<ConfirmDialog
  open={Boolean(confirming) && !removingFromEms}
  title={confirming?.removing ? t("mqtt.mapping.removeTitle") : t("mqtt.mapping.saveTitle")}
  lines={!confirming ? [] : confirming.removing ? [t("mqtt.mapping.removeLine", { id: confirming.id })] : saveLines(confirming)}
  confirmLabel={confirming?.removing ? t("devices.remove") : confirming?.adds && !confirming.moves ? t("mqtt.mapping.saveAndAdd") : t("mqtt.mapping.save")}
  danger={Boolean(confirming?.removing)}
  onconfirm={apply}
  oncancel={() => (confirming = null)}
/>

<Dialog open={removingFromEms} title={t("mqtt.mapping.removeTitle")} onclose={() => (confirming = null)}>
  <p class="muted">{t("mqtt.mapping.removeLine", { id: confirming?.id || "" })}</p>
  <label class="check"><input type="checkbox" bind:checked={alsoEms} data-testid="mqtt-remove-ems" /><span>{t("mqtt.mapping.removeEms", { name: confirming?.emsName || "" })}</span></label>
  <p class="muted small">{alsoEms ? (adoption?.direct ? t("mqtt.mapping.removeEmsNow") : t("mqtt.mapping.removeEmsDraft")) : t("mqtt.mapping.removeEmsKept")}</p>
  {#snippet actions()}
    <button class="btn secondary" type="button" onclick={() => (confirming = null)}>{t("auth.cancel")}</button>
    <button class="btn danger" type="button" onclick={apply} data-testid="confirm">{t("devices.remove")}</button>
  {/snippet}
</Dialog>

<style>
  .mapping { display: grid; gap: var(--s3); }
  .flow { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: var(--s2) var(--s4); font-size: 13px; color: var(--m-muted); }
  .flow li { display: inline-flex; gap: 6px; align-items: center; }
  .flow li.done :global(svg.i) { color: var(--m-ok); }
  .flow li[aria-current="step"] { color: var(--m-text); font-weight: 600; }
  .flow li[aria-current="step"] :global(svg.i) { color: var(--m-accent); }
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
