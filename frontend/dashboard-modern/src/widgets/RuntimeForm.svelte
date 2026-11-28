<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import { untrack } from "svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import ConfirmDialog from "../../../shared/ui/ConfirmDialog.svelte";
  import Segmented from "../../../shared/ui/Segmented.svelte";
  import Stepper from "../../../shared/ui/Stepper.svelte";
  import Switch from "../../../shared/ui/Switch.svelte";
  import { barFor, stepFor } from "../../../shared/lib/controls.js";
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
   * The controls are the shared ones the Admin console's settings use: a
   * switch before its label, a stepper with the catalogue's bar, the default
   * ticked on it and named in its tip, and buttons for two or three choices.
   * A number the browser cannot read keeps the value it had, as in the Admin
   * console, so the stepper steps from that and the save refuses the typo.
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
      {@const id = `${uid}-${field.name}`}
      {#if field.type === "toggle"}
        <div class="row toggle">
          <Switch {id} name={field.name} checked={values[field.name] === true} onchange={(checked) => (values[field.name] = checked)} />
          <label for={id}>{field.label}</label>
        </div>
      {:else if field.type === "number"}
        {@const standard = Number.isFinite(field.default) ? field.default : null}
        <div class="row stack">
          <label for={id}>{field.label}</label>
          <Stepper
            {id}
            name={field.name}
            value={values[field.name] ?? ""}
            step={stepFor(field)}
            min={field.min}
            max={field.max}
            start={standard}
            unit={field.unit ? { text: field.unit } : null}
            placeholder={standard !== null ? String(standard) : field.placeholder || ""}
            note={standard !== null ? t("common.defaultNote") : ""}
            bar={barFor(field)}
            fill={field.emptyIsMaximum ? (barFor(field)?.[1] ?? null) : null}
            mark={standard}
            markText={standard !== null ? t("common.defaultIs", { value: field.unit ? `${fmt().number(standard)} ${field.unit}` : fmt().number(standard) }) : ""}
            oninput={(event) => {
              if (!event.currentTarget.validity?.badInput) values[field.name] = event.currentTarget.value;
            }}
            onstep={(text) => (values[field.name] = text)}
          />
        </div>
      {:else if field.type === "gridTarget"}
        {@const parts = gridTargetParts(values[field.name], directions[field.name] || gridTargetParts(field.default, field.direction).direction)}
        {@const meaning = gridTargetMeaning(field.phase, values[field.name], field.default)}
        {@const standard = Number.isFinite(field.default) && gridTargetParts(field.default, field.direction).direction === parts.direction ? Math.abs(field.default) : null}
        {@const reach = Array.isArray(field.scale) ? Math.max(...field.scale.map((end) => Math.abs(Number(end)))) : null}
        <div class="row stack" role="group" aria-labelledby="{id}-label">
          <span class="label" id="{id}-label">{field.label}</span>
          <div class="grid-target">
            <Segmented
              choice
              label={t("gridTarget.direction")}
              value={parts.direction}
              options={[{ value: "import", label: t("gridTarget.import") }, { value: "export", label: t("gridTarget.export") }]}
              onchange={(direction) => {
                directions[field.name] = direction;
                values[field.name] = gridTargetValue(direction, parts.watts);
              }}
            />
            <Stepper
              {id}
              name={field.name}
              label={t("gridTarget.watts")}
              value={parts.watts ?? ""}
              step={10}
              min={0}
              max={field.max}
              start={standard}
              unit={{ text: "W" }}
              placeholder={standard !== null ? String(standard) : ""}
              note={standard !== null ? t("common.defaultNote") : ""}
              bar={Number.isFinite(reach) && reach > 0 ? [0, reach] : null}
              mark={standard}
              markText={standard !== null ? t("common.defaultIs", { value: `${fmt().number(standard)} W` }) : ""}
              oninput={(event) => {
                if (!event.currentTarget.validity?.badInput) values[field.name] = gridTargetValue(parts.direction, event.currentTarget.value);
              }}
              onstep={(text) => (values[field.name] = gridTargetValue(parts.direction, text))}
            />
          </div>
        </div>
        <p class="help meaning" data-testid="grid-target-meaning" data-field={field.name}>{t(meaning.key, { w: fmt().power(meaning.watts) })}</p>
      {:else if field.type === "select" && field.options.length >= 2 && field.options.length <= 3}
        <div class="row stack">
          <span class="label" aria-hidden="true">{field.label}</span>
          <Segmented choice label={field.label} value={values[field.name]} options={field.options} onchange={(value) => (values[field.name] = value)} />
        </div>
      {:else if field.type === "select"}
        <div class="row stack">
          <label for={id}>{field.label}</label>
          <select class="input" {id} name={field.name} bind:value={values[field.name]}>
            {#each field.options as option (option.value)}<option value={option.value}>{option.label}</option>{/each}
          </select>
        </div>
      {:else}
        <div class="row readonly"><span class="label">{field.label}</span><span class="value">{field.value}</span></div>
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
  .row { display: flex; flex-direction: column; gap: 8px; background: var(--m-surface-2); border-radius: var(--m-radius-inner); padding: 10px 12px; font-size: 13px; }
  .row > label, .row > .label { color: var(--m-muted); }
  .row.toggle { flex-direction: row; align-items: center; gap: 10px; min-height: 44px; padding-block: 8px; }
  .row.toggle > label { color: var(--m-text); cursor: pointer; }
  .row.readonly { flex-direction: row; align-items: center; justify-content: space-between; gap: var(--s3); min-height: 40px; padding-block: 6px; }
  .row select.input { max-width: 20rem; }
  .grid-target { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
  .grid-target :global(.stepper-wrap) { flex: 1 1 12rem; }
  .readonly .value { font-size: 12.5px; }
  .help { margin: 0 0 4px; font-size: 11.5px; color: var(--m-muted-2); }
  .foot { display: flex; align-items: center; gap: var(--s3); margin-top: auto; }
  .feedback { font-size: 12.5px; color: var(--m-muted); }
</style>
