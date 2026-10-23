<script>
  import { t, catalogText, optionText } from "../i18n/i18n.svelte.js";
  import { RISKS, choices, defaultHint, previewValue, shownValue } from "../lib/settings.js";

  /**
   * lang "catalog": the label, help and unit are the settings catalogue's, in
   * the reader's language where this build has them. null: they are the
   * console's own words. "en": the server's English, as it is.
   * descriptionLang, when given, is the help text's own language (null: the
   * page's), for a field whose label and help come from different places.
   */
  let { field, value, live = null, hideRisk = false, defaultValue = undefined, lang = "catalog", descriptionLang = undefined, placeholder = "", type = "", disabled = false, onchange } = $props();

  const uid = $props.id();
  const id = `setting-${uid}`;
  const shown = $derived(shownValue(field, value));
  const hint = $derived(defaultValue === undefined ? defaultHint(field, value) : defaultValue !== null && (value === null || value === undefined || value === "") ? String(defaultValue) : "");
  const risk = $derived(!hideRisk && RISKS.includes(field.risk) ? field.risk : null);
  const numeric = $derived(field.type === "number" || field.type === "integer");
  const words = (text) => (lang === "catalog" ? catalogText(text) : { text, lang: lang || undefined });
  const label = $derived(words(field.label || field.path));
  const description = $derived(!field.description ? null : descriptionLang !== undefined ? { text: field.description, lang: descriptionLang || undefined } : words(field.description));
  const unit = $derived(field.unit ? catalogText(field.unit) : null);
  const hintText = $derived((field.options || []).map(String).includes(hint) ? optionText(hint) : hint);
</script>

<div class="setting" data-path={field.path}>
  {#if field.type === "boolean"}
    <label class="check head" for={id}>
      <input {id} type="checkbox" checked={shown} {disabled} onchange={(event) => onchange(event.currentTarget.checked)} />
      <span class="label" lang={label.lang}>{label.text}</span>
    </label>
  {:else}
    <label class="head" for={id}><span class="label" lang={label.lang}>{label.text}</span>{#if unit}<span class="unit" lang={unit.lang}>{unit.text}</span>{/if}</label>
    {#if field.options?.length}
      <select {id} class="input" value={shown} {disabled} onchange={(event) => onchange(event.currentTarget.value)}>
        {#if !shown}<option value="">{hint ? t("settings.edit.default", { value: hintText }) : "—"}</option>{/if}
        {#each choices(field, value) as option (option)}<option value={option}>{optionText(option)}</option>{/each}
      </select>
    {:else}
      <input
        {id}
        class="input num"
        type={type || (numeric ? "number" : "text")}
        {disabled}
        autocomplete={type === "password" ? "new-password" : undefined}
        inputmode={numeric ? "decimal" : undefined}
        step={numeric ? "any" : undefined}
        min={numeric ? field.min : undefined}
        max={numeric ? field.max : undefined}
        value={shown}
        placeholder={placeholder || (hint ? t("settings.edit.default", { value: hint }) : "")}
        oninput={(event) => onchange(event.currentTarget.value)}
      />
    {/if}
  {/if}
  {#if description}<p class="desc" lang={description.lang}>{description.text}</p>{/if}
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
