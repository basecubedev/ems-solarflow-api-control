<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import { tick } from "svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import ConfirmDialog from "../../../shared/ui/ConfirmDialog.svelte";
  import Dialog from "../../../shared/ui/Dialog.svelte";
  import MqttDeviceWizard from "./MqttDeviceWizard.svelte";
  import MqttBrokerChooser from "./MqttBrokerChooser.svelte";
  import MqttTopicList from "./MqttTopicList.svelte";
  import MqttSavedStep from "./MqttSavedStep.svelte";
  import RestartEms from "./RestartEms.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { prefs } from "../../../shared/state/prefs.svelte.js";
  import { ADOPT_REFUSALS, BUILTIN_BROKER, MAPPING_EDITOR_ISSUES, MAPPING_ISSUES, MAPPING_STEPS, SILENCE_REASONS, VALUE_REASONS, brokerAddress, emsReads, emsWaiting, groupKey, mappingStep, newDevice, newSource, proposedId, savedKind, targetSources, targetsOf } from "../lib/mqtt.js";
  import { mqtt, loadMapping, startTopics, stopTopics, previewMapping, applyMapping, previewValue, watchMapping, watchTopics } from "../state/mqtt.svelte.js";
  import { errorView } from "../lib/errors.js";

  /**
   * adoption: what the page this sits on does with a mapped device's EMS
   * device, which is the same thing to the reader: ``{ direct, busy,
   * restartable?, state(device, connection) -> "saved" | "draft" | "elsewhere"
   * | null, name(device), add(device), renamable?(device, label),
   * rename?(device, label), remove?(device), restart? }``. ``direct`` says a
   * change is saved at once rather than joining changes that wait;
   * ``elsewhere`` is the same id read from another broker; each action answers
   * ``{ state, error }``, ``state`` being ``"saved"`` when it wrote the EMS
   * device and ``"unchanged"`` when there was nothing to write; ``restartable``
   * says the page can restart EMS, and ``restart`` is ``{ needed, since }``
   * once a change saved there waits for that. Whether the running EMS reads
   * each mapped device as saved is the server's answer (``ems_devices`` in the
   * mapping view). A change the dialog saves at once, which the running EMS
   * does not read as saved, ends in a step that offers the restart; one a row's
   * own button saves brings the restart offer into view.
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
  let wizardDevice = $state.raw(null);
  let context = $state.raw(null);
  let fresh = $state("");
  let session = 0;
  let root = $state();
  let adopting = $state("");
  let outcome = $state.raw({});
  let alsoEms = $state(true);
  let finished = $state.raw(null);

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
  const editing = $derived(Boolean(wizard || draft));
  const subject = $derived(wizard ? wizardDevice : draft);
  const saveLabel = $derived(
    adoption && subject?.enabled !== false && (wizard || !["saved", "draft"].includes(placeOf(devices[editingIndex]))) ? t("mqtt.mapping.saveAndAdd") : t("mqtt.mapping.save"),
  );
  const editorTitle = $derived(
    draft && editingIndex >= 0 ? t("mqtt.mapping.editTitle", { id: draft.id }) : context ? t("mqtt.mapping.mapTitle", { name: context.key }) : t("mqtt.mapping.newTitle"),
  );
  const pickOpen = $derived.by(() => {
    if (context) return [groupKey(context)];
    const topics = new Set(Object.values(draft?.values || {}).flatMap((spec) => targetSources(spec).map(([, , source]) => source.topic)));
    return groups.filter((group) => group.topics.some((topic) => topics.has(topic.topic))).map(groupKey);
  });
  const emsDevices = $derived(view?.ems_devices);
  const waiting = $derived(adoption?.restartable ? emsWaiting(emsDevices) : []);
  const waitingIds = $derived(new Set(waiting.filter((item) => item.state === "waiting").map((item) => item.id)));
  const waitingNames = $derived(waiting.map((item) => t(`mqtt.restart.item.${item.change}`, { name: item.change === "removed" ? item.ran : item.name })));
  const usedValues = $derived(new Set(draft?.values?.[target] ? targetSources(draft.values[target]).map(([, , source]) => `${source.topic}|${source.path ?? ""}`) : []));

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

  function nameOf(device) {
    return String(device?.label || "").trim() || device?.id || "";
  }

  function adopt(device) {
    return adopted(device.id, { run: () => adoption.add(device), kind: placeOf(device) === "elsewhere" ? "moved" : "added", device });
  }

  /**
   * Runs an EMS change for mapped device ``id``, ``{ run, kind, device | name,
   * retryable }``, and keeps its answer: a refused retryable change can be
   * tried again. A saved one is read again from the server, asked after the
   * write, which says whether the running EMS reads it as saved; the restart
   * offer is then brought into view unless a step stands for the change
   * (``stepped``). Answers ``{ result, view, at }``: the view read after the
   * write at ``at``, when it was saved.
   */
  async function adopted(id, change, stepped = false) {
    adopting = id;
    let result = null;
    let view = null;
    let at = 0;
    try {
      result = await change.run();
      noteOutcome(id, change.retryable && refused(result) ? { ...result, retry: change } : result);
      if (result?.state === "saved") {
        at = Date.now();
        view = await loadMapping(at);
      }
    } finally {
      adopting = "";
    }
    if (!stepped) await revealRestart();
    return { result, view, at };
  }

  function noteOutcome(id, result) {
    const { [id]: _previous, ...others } = outcome;
    outcome = result ? { ...others, [id]: result } : others;
  }

  function refused(result) {
    return ADOPT_REFUSALS.includes(result?.state) || result?.state === "failed";
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
    session += 1;
    context = null;
    fresh = "";
    editingIndex = index;
    draft = structuredClone($state.snapshot(devices[index]));
    target = targetsOf(draft)[0];
    evaluation = null;
    issues = [];
  }

  function editById(id) {
    edit(devices.findIndex((device) => device.id === id));
  }

  function startDevice(group) {
    session += 1;
    editingIndex = -1;
    draft = null;
    context = group;
    fresh = "";
    wizardDevice = null;
    wizard = { group, brokerRef: mqtt.topics?.broker_ref || brokerRef, at: Date.now() };
    target = "inverterPower";
    evaluation = null;
    issues = [];
  }

  function freeEditor(device) {
    wizard = null;
    editingIndex = -1;
    draft = { ...newDevice(device.broker_ref || brokerRef), ...device, id: device.id || proposedId("") };
    target = targetsOf(draft)[0];
    evaluation = null;
    issues = [];
    session += 1;
  }

  function closeEditor() {
    session += 1;
    wizard = null;
    draft = null;
    context = null;
    editingIndex = -1;
    evaluation = null;
    issues = [];
  }

  /** Scrolls a part of this panel into view at ``block``, smoothly where the reader allows motion. */
  function bring(element, block) {
    element?.scrollIntoView({ block, behavior: prefs.animate ? "smooth" : "auto" });
  }

  /** Brings a device the dialog just saved into view and gives it the focus the closed dialog held. */
  async function reveal(id) {
    fresh = id;
    await tick();
    const row = root?.querySelector(`[data-testid="mqtt-mapped-devices"] li[data-id="${CSS.escape(id)}"]`);
    bring(row, "center");
    row?.focus({ preventScroll: true });
  }

  /**
   * Keeps the restart a change now waits for in view, scrolling only when it
   * is not, and can hand the focus to it: its button, its link when the button
   * is off, or the offer itself once it only says how the restart went. With no
   * offer left, the focus goes to the mapped devices, or to New device.
   */
  async function revealRestart(focus = false) {
    await tick();
    const offer = root?.querySelector('[data-testid="restart-offer"]');
    bring(offer, "nearest");
    if (!focus) return;
    const target = offer?.querySelector('[data-testid="restart-ems"]:not(:disabled), a[href]') || offer || root?.querySelector('[data-testid="mqtt-mapped-devices"] li') || root?.querySelector('[data-testid="mqtt-new-device"]');
    if (!offer) bring(target, "nearest");
    target?.focus({ preventScroll: true });
  }

  /**
   * Ends the step a saved change ended in, onto what it changed: the device's
   * row, or, for a removed one, why its EMS device stayed when it did, else the
   * restart still on offer.
   */
  async function leave(ending = finished) {
    finished = null;
    if (!ending) return;
    if (!ending.removing) return reveal(ending.id);
    await tick();
    const failed = root?.querySelector(`[data-testid="mqtt-remove-failed"][data-id="${CSS.escape(ending.id)}"]`);
    if (!failed) return revealRestart(true);
    bring(failed, "nearest");
    failed.focus({ preventScroll: true });
  }

  /** Ends the step a saved device ended in and goes on to the next one: the live topics' search when they are listed, else a new device. */
  async function another() {
    finished = null;
    await tick();
    const search = root?.querySelector('[data-testid="mqtt-topics-filter"]');
    if (!search) return startDevice(null);
    bring(search, "center");
    search.focus({ preventScroll: true });
    search.select();
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

  function heard(device) {
    session += 1;
    wizardDevice = device;
    evaluation = null;
  }

  async function preview() {
    const asked = session;
    const result = await previewValue($state.snapshot(subject));
    if (asked !== session) return;
    issues = result?.issues || mqtt.error?.payload?.issues || [];
    evaluation = result?.evaluation || null;
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
    const asked = session;
    const answer = await previewMapping(document);
    if (asked !== session) return;
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
      emsName: place || known ? adoption.name(before || device) || nameOf(before || device) : "",
      adds: Boolean(adoption && device && device.enabled !== false && !there),
      moves: place === "elsewhere" || Boolean(known),
      renames: Boolean(there && device && label && label !== String(before?.label || "").trim() && adoption.renamable?.(before, label)),
    };
    alsoEms = true;
  }

  /** The EMS change a confirmed save or removal makes, or null when its EMS device stays as it is. */
  function emsChangeOf(pending) {
    if (pending.removing) return pending.inEms && alsoEms && adoption?.remove ? { run: () => adoption.remove(pending.before), kind: "removed", name: pending.emsName || nameOf(pending.before) } : null;
    if (pending.adds) return { run: () => adoption.add(pending.device), kind: pending.moves ? "moved" : "added", device: pending.device };
    if (pending.renames) return { run: () => adoption.rename(pending.before, pending.device.label), kind: "renamed", device: pending.device, retryable: true };
    return null;
  }

  /**
   * Writes the confirmed mapping, then its EMS change. One saved at once ends
   * in the step that offers the restart it waits for, in place of the editor,
   * named as the server names the device; one that joins other changes, is
   * refused, or that the server says the running EMS already reads as saved
   * closes onto the device's row.
   */
  async function apply() {
    const pending = confirming;
    confirming = null;
    const change = emsChangeOf(pending);
    const title = pending.removing ? t("mqtt.saved.removeTitle", { name: nameOf(pending.before) }) : editorTitle;
    const answer = await applyMapping(pending.document);
    issues = answer?.issues || mqtt.error?.payload?.issues || [];
    if (!answer?.ok) return;
    const ending = change && adoption.direct ? { title, id: pending.id, removing: pending.removing, name: "", kind: change.kind, at: Date.now(), saved: false } : null;
    finished = ending;
    draft = null;
    wizard = null;
    context = null;
    editingIndex = -1;
    noteOutcome(pending.id, null);
    if (!pending.removing && !ending) await reveal(pending.id);
    if (!change) return;
    const { result, view: seen, at } = await adopted(pending.id, change, Boolean(ending));
    if (!ending || finished !== ending) return;
    if (result?.state !== "saved" || emsReads(seen?.ems_devices, pending.id, pending.removing)) return leave(ending);
    const item = seen?.ems_devices?.devices?.[pending.id];
    const name = pending.removing ? item?.ran || change.name : item?.name || adoption.name(change.device) || nameOf(change.device);
    finished = { ...ending, kind: savedKind(ending.kind, item), name, ran: item?.ran || name, at, saved: true };
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

{#snippet issueList()}
  {#each issues as issue, index (`${issue.code}-${index}`)}
    <p class="form-error"><Icon name="triangle-alert" size="sm" /><span lang={MAPPING_ISSUES.includes(issue.code) ? undefined : "en"}>{issueText(issue)}</span>{#if issue.path}<code class="small">{issue.path}</code>{/if}</p>
  {/each}
{/snippet}

{#snippet editorActions()}
  {#if issues.length}<div class="outcome">{@render issueList()}</div>{/if}
  {#if evaluation}
    <div class="outcome">
      {#if evaluation.state}
        <p class="note ok" data-testid="mqtt-preview-state"><Icon name="circle-check" />{t("mqtt.mapping.wouldPublish")} <code>{JSON.stringify(evaluation.state)}</code></p>
      {:else}
        <p class="note warn" data-testid="mqtt-preview-silent"><Icon name="triangle-alert" />{t("mqtt.mapping.wouldNotPublish", { reason: reasonText(evaluation.reason) })}</p>
        {#each evaluation.targets.filter((item) => item.reason === "catalog_refused") as item (item.key)}
          <p class="muted small row-wrap">
            {t("mqtt.mapping.refusedValue", { target: t(`mqtt.target.${item.key}`), value: item.value })}
            {#if draft && Number(item.value) < 0 && draft.values[item.key]?.sources}
              <button class="btn secondary small" type="button" onclick={() => correct(item.key)} data-testid="mqtt-correct">{t("mqtt.mapping.correct")}</button>
            {/if}
          </p>
        {/each}
      {/if}
    </div>
  {/if}
  <button class="btn secondary" type="button" onclick={closeEditor}>{t("auth.cancel")}</button>
  <button class="btn secondary" type="button" onclick={preview} disabled={busy || !subject} data-testid={wizard ? "mqtt-wizard-preview" : "mqtt-device-preview"}>{t("mqtt.mapping.preview")}</button>
  <button class="btn" type="button" onclick={() => review($state.snapshot(subject), wizard ? -1 : editingIndex)} disabled={working || (wizard ? !wizardDevice?.label : !draft?.id)} data-testid={wizard ? "mqtt-wizard-save" : "mqtt-device-save"}>{saveLabel}</button>
{/snippet}

{#snippet refusal(result)}
  {#if ADOPT_REFUSALS.includes(result.state)}
    {t(`mqtt.mapping.adopt.${result.state}`)}
  {:else}
    {@const view = errorView(result.error, t)}
    {t("mqtt.mapping.adoptFailed")} <span lang={view.lang}>{view.text}</span>
  {/if}
{/snippet}

<div class="mapping" bind:this={root} data-testid="mqtt-mapping">
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
    <p class="note warn small" tabindex="-1" data-id={id} data-testid="mqtt-remove-failed">{t("mqtt.mapping.removeFailed", { id })}{#if result.state === "failed"}{" "}{@render refusal(result)}{/if}</p>
  {/each}

  {#if devices.length}
    <h3 class="sub">{t("mqtt.mapping.devices")}</h3>
    <ul class="list" data-testid="mqtt-mapped-devices">
      {#each devices as device, index (device.id)}
        {@const status = statusById[device.id]}
        {@const there = placeOf(device)}
        {@const result = outcome[device.id]}
        <li class="inner row-wrap" class:fresh={fresh === device.id} tabindex="-1" onblur={() => (fresh = "")} data-id={device.id}>
          <strong>{nameOf(device)}</strong>
          <span class="muted small">{device.id} · {brokerLabel(device.broker_ref)}</span>
          {#if status}
            <Status tone={status.publishing ? "c-ok" : "c-warn"} text={status.publishing ? t("mqtt.mapping.publishing") : reasonText(status.reason)} />
          {:else if device.enabled !== false}
            <Status tone="c-idle" text={t("mqtt.mapping.firstValue")} />
          {/if}
          {#if there === "saved" && waitingIds.has(device.id)}
            <span data-testid="mqtt-adopted"><Status tone="c-warn" text={t("mqtt.mapping.waitsRestart")} /></span>
          {:else if there === "saved" && emsDevices?.devices?.[device.id]?.state === "running"}
            <span data-testid="mqtt-adopted"><Status tone="c-ok" text={t("mqtt.mapping.live")} /></span>
          {:else if there === "saved"}
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
            {#if result.retry}<button class="btn secondary small" type="button" onclick={() => adopted(device.id, result.retry)} disabled={working} data-testid="mqtt-adopt-retry"><Icon name="rotate-ccw" size="sm" />{t("mqtt.mapping.retry")}</button>{/if}
          {/if}
          <button class="btn secondary small" type="button" onclick={() => edit(index)} disabled={working}><Icon name="wrench" size="sm" />{t("mqtt.mapping.edit")}</button>
          <button class="btn secondary small" type="button" onclick={() => review(null, index)} disabled={working}><Icon name="trash-2" size="sm" />{t("devices.remove")}</button>
        </li>
      {/each}
    </ul>
  {/if}

  {#if adoption?.restartable}
    <RestartEms
      needed={waiting.length > 0 || Boolean(adoption.restart?.needed)}
      since={adoption.restart?.since}
      reason={waiting.length ? t("mqtt.restart.waiting", { names: waitingNames.join(", ") }) : ""}
    />
  {/if}

  {#if !editing}{@render issueList()}{/if}

  {#if mqtt.topics}
    <h3 class="sub">{t("mqtt.topics.title")}</h3>
    {#if mqtt.topics.truncated}<p class="note warn"><Icon name="triangle-alert" />{t("mqtt.topics.truncated")}</p>{/if}
    {#if !groups.length}<p class="muted small">{t("mqtt.topics.waiting")}</p>{/if}
    <MqttTopicList {groups} {busy} onmap={startDevice} onedit={editById} editable={new Set(devices.map((device) => device.id))} waiting={waitingIds} {whereText} />
  {/if}
  {#if adoption}<p class="muted small">{t("mqtt.mapping.adoptHint")}</p>{/if}
</div>

<Dialog open={editing} wide title={editorTitle} onclose={closeEditor} actions={editorActions}>
  {#if wizard}
    {#key wizard.at}
      <MqttDeviceWizard
        group={wizard.group}
        brokerRef={wizard.brokerRef}
        takenIds={[...devices.map((item) => item.id), ...(view?.taken_ids?.[wizard.brokerRef] || [])]}
        onanswer={heard}
        onfree={freeEditor}
      />
    {/key}
  {:else if draft}
    <section class="editor" data-testid="mqtt-mapping-editor">
      <div class="row-wrap">
        <label class="field"><span class="small muted">{t("mqtt.mapping.id")}</span><input class="input" value={draft.id} oninput={(event) => update({ id: event.currentTarget.value })} readonly={editingIndex >= 0} title={editingIndex >= 0 ? t("mqtt.mapping.idFixed") : undefined} data-testid="mqtt-device-id" /></label>
        <label class="field"><span class="small muted">{t("mqtt.mapping.label")}</span><input class="input" value={draft.label || ""} oninput={(event) => update({ label: event.currentTarget.value })} /></label>
        <label class="check"><input type="checkbox" checked={draft.enabled !== false} onchange={(event) => update({ enabled: event.currentTarget.checked })} /><span>{t("mqtt.mapping.enabled")}</span></label>
      </div>
      <div class="row-wrap" role="group" aria-label={t("mqtt.mapping.targets")}>
        {#each targetsOf(draft) as key (key)}
          <button class="btn small" class:secondary={target !== key} type="button" onclick={() => (target = key)} aria-pressed={target === key ? "true" : "false"}>
            {t(`mqtt.target.${key}`)}{draft.values[key] ? ` (${targetSources(draft.values[key]).length})` : ""}
          </button>
        {/each}
      </div>
      {#if target === "batteryPower"}
        <label class="check"><input type="checkbox" checked={Boolean(draft.values.batteryPower && !Array.isArray(draft.values.batteryPower.sources))} onchange={(event) => splitBattery(event.currentTarget.checked)} /><span>{t("mqtt.mapping.split")}</span></label>
      {/if}
      {#each targetsOf(draft).filter((key) => draft.values[key]) as key (key)}
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
        {#if target !== targetsOf(draft)[0] && draft.values[target]}
          <button class="btn secondary small" type="button" onclick={() => setTarget(target, null)}><Icon name="x" size="sm" />{t("mqtt.mapping.dropTarget")}</button>
        {/if}
      </form>
      {#if mqtt.topics}
        <h3 class="sub">{t("mqtt.topics.title")}</h3>
        <MqttTopicList {groups} onuse={addSource} useLabel={t("mqtt.topics.useAs", { target: t(`mqtt.target.${target}`) })} usedLabel={t("mqtt.topics.usedAs", { target: t(`mqtt.target.${target}`) })} used={usedValues} opened={pickOpen} {whereText} />
      {/if}
    </section>
  {/if}
</Dialog>

{#if finished}
  <MqttSavedStep
    step={finished}
    waiting={waitingNames}
    seen={{ at: mqtt.mappingAt, known: emsDevices?.known === true, reason: emsDevices?.reason, startedAt: emsDevices?.started_at ?? null, reads: emsReads(emsDevices, finished.id, finished.removing) === true }}
    onanother={finished.removing ? null : another}
    onclose={() => leave()}
  />
{/if}

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
  .topic { overflow-wrap: anywhere; }
  .fresh { outline: 2px solid var(--m-accent); outline-offset: 2px; }
  .outcome { flex-basis: 100%; display: grid; gap: var(--s1); }
</style>
