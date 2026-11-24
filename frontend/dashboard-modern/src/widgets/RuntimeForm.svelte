<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import { untrack } from "svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import ConfirmDialog from "../../../shared/ui/ConfirmDialog.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { authorizedSend } from "../state/auth.svelte.js";
  import { loadRuntime, live } from "../state/live.svelte.js";
  import { changedPayload, changeWarning, normalizeValue, reseed, boundExceeded } from "../lib/runtime.js";
  import { gridTargetMeaning, gridTargetParts, gridTargetValue } from "../lib/gridTarget.js";
  import { errorText } from "../lib/errors.js";
  import { unreadableInput } from "../../../shared/lib/forms.js";

  /**
   * One runtime card. Its values are local state seeded from /api/runtime, and
   * a save sends what differs from the value each field was seeded with. When a
   * stored value changes underneath, every field the operator has not edited
   * takes the new value, so a save never writes back a value nobody chose.
   */
  let { endpoint, title, subtitle = "", icon = "sliders-horizontal", tone = "c-soc", step = 1, fields, initial, settingLabel } = $props();

  let values = $state({});
  let baseline = $state.raw({});
  let seeded = $state("");
  let feedback = $state({ text: "", error: false, link: null });
  let busy = $state(false);
  let confirm = $state({ open: false, lines: [], payload: null });
  let directions = $state({});
  const uid = $props.id();

  const signature = $derived(JSON.stringify(fields.map((field) => [field.name, normalizeValue(field, initial[field.name])])));

  $effect(() => {
    if (signature === seeded) return;
    untrack(() => {
      const next = reseed(fields, { values, baseline, initial, fresh: !seeded });
      values = next.values;
      baseline = next.baseline;
    });
    seeded = signature;
  });

  function messageLines(items) {
    return (items || []).map((item) => {
      const vars = { ...(item.vars || {}) };
      if (item.settings) vars.settings = item.settings.map((key) => settingLabel(key)).join(", ");
      return t(item.key, vars);
    });
  }

  function labelled(message) {
    let text = message;
    fields.forEach((field) => {
      text = text.split(field.name).join(field.label);
    });
    return text;
  }

  async function save(payload) {
    busy = true;
    feedback = { text: t("common.saving"), error: false };
    try {
      await authorizedSend(endpoint, { method: "PATCH", body: payload });
      await loadRuntime();
      seeded = "";
      feedback = { text: t("common.saved"), error: false };
    } catch (error) {
      feedback = { text: labelled(errorText(error, t)), error: true };
    } finally {
      busy = false;
    }
  }

  function submit(event) {
    event.preventDefault();
    const unreadable = unreadableInput(event.currentTarget);
    if (unreadable) {
      const field = fields.find((item) => item.name === unreadable.name);
      feedback = { text: t("runtime.notANumber", { field: field?.label || unreadable.name }), error: true };
      return;
    }
    const payload = changedPayload(fields, values, baseline);
    const over = boundExceeded(fields, payload);
    if (over) {
      feedback = { text: `${t("runtime.overBound", { value: `${fmt().number(payload[over.name], Number.isInteger(payload[over.name]) ? 0 : 1)}\u00a0${over.unit || "W"}`, field: over.label })} ${over.help}`, error: true, link: over.link };
      return;
    }
    if (!Object.keys(payload).length) {
      feedback = { text: t("runtime.nothing"), error: false };
      return;
    }
    const warning = changeWarning(endpoint, payload, live.runtime);
    if (warning && warning.length) {
      confirm = { open: true, lines: messageLines(warning), payload };
      return;
    }
    save(payload);
  }
</script>

<form class="runtime card" onsubmit={submit} data-endpoint={endpoint} novalidate>
  <div class="head">
    <span class="step num">{String(step).padStart(2, "0")}</span>
    <div class="icon-tile sm {tone}"><Icon name={icon} /></div>
    <div class="ellipsis">
      <h3 class="title ellipsis">{title}</h3>
      {#if subtitle}<div class="sub">{subtitle}</div>{/if}
    </div>
  </div>
  <div class="fields">
    {#each fields as field (field.name)}
      {#if field.type === "toggle"}
        <label class="row check">
          <span>{field.label}</span>
          <input type="checkbox" name={field.name} bind:checked={values[field.name]} />
        </label>
      {:else if field.type === "number"}
        <label class="row">
          <span>{field.label}</span>
          <span class="number">
            <input class="input num" type="number" name={field.name} min={field.min} max={field.max} step={field.step || 1} placeholder={field.default !== undefined ? t("common.defaultValue", { value: field.default }) : field.placeholder || ""} bind:value={values[field.name]} />
            <span class="unit">{field.unit}</span>
          </span>
        </label>
      {:else if field.type === "gridTarget"}
        {@const parts = gridTargetParts(values[field.name], directions[field.name] || gridTargetParts(field.default, field.direction).direction)}
        {@const meaning = gridTargetMeaning(field.phase, values[field.name], field.default)}
        <div class="row" role="group" aria-labelledby="{uid}-{field.name}">
          <span id="{uid}-{field.name}">{field.label}</span>
          <span class="number">
            <select
              class="input"
              aria-label={t("gridTarget.direction")}
              value={parts.direction}
              onchange={(event) => {
                directions[field.name] = event.currentTarget.value;
                values[field.name] = gridTargetValue(event.currentTarget.value, parts.watts);
              }}
            >
              <option value="import">{t("gridTarget.import")}</option>
              <option value="export">{t("gridTarget.export")}</option>
            </select>
            <input
              class="input num watts"
              type="number"
              name={field.name}
              min="0"
              max={field.max}
              step="1"
              aria-label={t("gridTarget.watts")}
              placeholder={field.default !== undefined ? t("common.defaultValue", { value: Math.abs(field.default) }) : ""}
              value={parts.watts}
              oninput={(event) => (values[field.name] = gridTargetValue(parts.direction, event.currentTarget.value))}
            />
            <span class="unit">W</span>
          </span>
        </div>
        <p class="help meaning" data-testid="grid-target-meaning" data-field={field.name}>{t(meaning.key, { w: fmt().power(meaning.watts) })}</p>
      {:else if field.type === "select"}
        <label class="row">
          <span>{field.label}</span>
          <select class="input" name={field.name} bind:value={values[field.name]}>
            {#each field.options as option (option.value)}<option value={option.value}>{option.label}</option>{/each}
          </select>
        </label>
      {:else}
        <div class="row readonly"><span>{field.label}</span><span class="value">{field.value}</span></div>
      {/if}
      {#if field.help}<p class="help">{field.help}{#if field.link}{" "}<a href={field.link.href}>{field.link.label}</a>{/if}</p>{/if}
    {/each}
  </div>
  <div class="foot">
    <button class="btn small" type="submit" disabled={busy}>{t("common.save")}</button>
    <span class="feedback" class:form-error={feedback.error} class:form-ok={!feedback.error && feedback.text === t("common.saved")} role="status" aria-live="polite">{feedback.text}{#if feedback.link}{" "}<a href={feedback.link.href} data-testid="runtime-bound-link">{feedback.link.label}</a>{/if}</span>
  </div>
</form>

<ConfirmDialog
  open={confirm.open}
  title={title}
  lines={confirm.lines}
  confirmLabel={t("runtime.confirm")}
  onconfirm={() => {
    const payload = confirm.payload;
    confirm = { open: false, lines: [], payload: null };
    save(payload);
  }}
  oncancel={() => {
    confirm = { open: false, lines: [], payload: null };
    feedback = { text: t("runtime.notSaved"), error: false };
  }}
/>

<style>
  .runtime { display: flex; flex-direction: column; gap: var(--s3); padding: var(--s4); }
  .head { display: flex; align-items: center; gap: var(--s3); }
  .step { font-size: 11px; color: var(--m-muted-2); font-weight: 600; }
  .title { margin: 0; font-size: 14px; font-weight: 620; }
  .sub { font-size: 12px; color: var(--m-muted); }
  .fields { display: flex; flex-direction: column; gap: 6px; }
  .row { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 6px var(--s3); background: var(--m-surface-2); border-radius: var(--m-radius-inner); padding: 6px 10px; min-height: 40px; font-size: 13px; }
  .row > span:first-child { color: var(--m-muted); }
  .number { display: inline-flex; align-items: center; gap: 6px; }
  .number .input { width: 136px; text-align: right; }
  .number select.input { width: auto; text-align: left; }
  .number .watts { width: 88px; }
  .row select.input { width: auto; max-width: 100%; margin-left: auto; }
  .unit { color: var(--m-muted); font-size: 12px; min-width: 14px; }
  .readonly .value { font-size: 12.5px; }
  .help { margin: 0 0 4px; font-size: 11.5px; color: var(--m-muted-2); }
  .foot { display: flex; align-items: center; gap: var(--s3); margin-top: auto; }
  .feedback { font-size: 12.5px; color: var(--m-muted); }
</style>
