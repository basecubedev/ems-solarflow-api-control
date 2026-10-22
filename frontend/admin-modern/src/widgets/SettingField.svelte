<script>
  import { t } from "../i18n/i18n.svelte.js";
  import { RISKS, choices, defaultHint, previewValue, shownValue } from "../lib/settings.js";

  let { field, value, live = null, hideRisk = false, onchange } = $props();

  const uid = $props.id();
  const id = `setting-${uid}`;
  const shown = $derived(shownValue(field, value));
  const hint = $derived(defaultHint(field, value));
  const risk = $derived(!hideRisk && RISKS.includes(field.risk) ? field.risk : null);
  const numeric = $derived(field.type === "number" || field.type === "integer");
</script>

<div class="setting" data-path={field.path}>
  {#if field.type === "boolean"}
    <label class="check head" for={id}>
      <input {id} type="checkbox" checked={shown} onchange={(event) => onchange(event.currentTarget.checked)} />
      <span class="label" lang="en">{field.label || field.path}</span>
    </label>
  {:else}
    <label class="head" for={id}><span class="label" lang="en">{field.label || field.path}</span>{#if field.unit}<span class="unit">{field.unit}</span>{/if}</label>
    {#if field.options?.length}
      <select {id} class="input" value={shown} onchange={(event) => onchange(event.currentTarget.value)}>
        {#if !shown}<option value="">{hint ? t("settings.edit.default", { value: hint }) : "—"}</option>{/if}
        {#each choices(field, value) as option (option)}<option value={option}>{option}</option>{/each}
      </select>
    {:else}
      <input
        {id}
        class="input num"
        type={numeric ? "number" : "text"}
        inputmode={numeric ? "decimal" : undefined}
        step={numeric ? "any" : undefined}
        min={numeric ? field.min : undefined}
        max={numeric ? field.max : undefined}
        value={shown}
        placeholder={hint ? t("settings.edit.default", { value: hint }) : ""}
        oninput={(event) => onchange(event.currentTarget.value)}
      />
    {/if}
  {/if}
  {#if field.description}<p class="desc" lang="en">{field.description}</p>{/if}
  {#if risk || live}
    <p class="badges">
      {#if risk}<span class="badge risk" title={t(`settings.risk.${risk}.hint`)}>{t(`settings.risk.${risk}`)}</span>{/if}
      {#if live}<span class="badge live" title={t("settings.edit.liveHint", { live: previewValue(live.effective_value), saved: previewValue(live.config_value) })}>{t("settings.edit.live", { value: previewValue(live.effective_value) })}</span>{/if}
    </p>
  {/if}
</div>

<style>
  .setting { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
  .head { display: flex; align-items: baseline; gap: var(--s2); font-size: 13.5px; font-weight: 560; }
  .check.head { align-items: center; }
  .unit { color: var(--m-muted); font-size: 12px; font-weight: 450; }
  .desc { margin: 0; color: var(--m-muted); font-size: 12.5px; overflow-wrap: anywhere; }
  .badges { display: flex; flex-wrap: wrap; gap: var(--s2); margin: 0; }
  .risk { color: var(--m-warn); }
  .live { color: var(--m-accent); }
</style>
