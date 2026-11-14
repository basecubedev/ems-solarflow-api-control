<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import { t, catalogText, optionText } from "../i18n/i18n.svelte.js";
  import { RISKS, choices, defaultHint, departedDefault, nameableDefault, previewValue, shownValue } from "../lib/settings.js";

  /**
   * lang "catalog": the label, help and unit are the settings catalogue's, in
   * the reader's language where this build has them. null: they are the
   * console's own words. "en": the server's English, as it is.
   * descriptionLang, when given, is the help text's own language (null: the
   * page's), for a field whose label and help come from different places.
   * A number the browser cannot read changes nothing, so a typo is never taken
   * for an emptied field, which would take the default.
   */
  let { field, value, live = null, hideRisk = false, defaultValue = undefined, lang = "catalog", descriptionLang = undefined, placeholder = "", type = "", disabled = false, onchange } = $props();

  let unreadable = $state(false);
  const uid = $props.id();

  $effect(() => {
    value;
    unreadable = false;
  });
  const id = `setting-${uid}`;
  const shown = $derived(shownValue(field, value));
  const fallback = $derived(defaultValue === undefined ? field.default : defaultValue);
  const hint = $derived(defaultHint(field, value, fallback));
  const risk = $derived(!hideRisk && RISKS.includes(field.risk) ? field.risk : null);
  const numeric = $derived(field.type === "number" || field.type === "integer");
  const words = (text) => (lang === "catalog" ? catalogText(text) : { text, lang: lang || undefined });
  const label = $derived(words(field.label || field.path));
  const description = $derived(!field.description ? null : descriptionLang !== undefined ? { text: field.description, lang: descriptionLang || undefined } : words(field.description));
  const unit = $derived(field.unit ? catalogText(field.unit) : null);
  const standard = $derived(nameableDefault(field, fallback));
  const departed = $derived(departedDefault(field, value, fallback));
  const optionWords = (option) => field.optionLabels?.[option] ?? optionText(option);
  const valueWords = (value) => ((field.options || []).map(String).includes(String(value)) ? optionWords(String(value)) : String(value));
  const hintText = $derived(hint ? valueWords(hint) : "");
  const departedText = $derived(
    departed === undefined ? ""
      : field.type === "boolean" ? t(departed ? "common.on" : "common.off")
      : Array.isArray(departed) ? departed.join(", ") : valueWords(departed),
  );
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
        {#if !shown}<option value="">{hint ? t("common.defaultValue", { value: hintText }) : "—"}</option>{/if}
        {#each choices(field, value) as option (option)}<option value={option}>{optionWords(option)}{shown && standard !== undefined && option === String(standard) ? ` ${t("settings.edit.defaultOption")}` : ""}</option>{/each}
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
        placeholder={placeholder || (hint ? t("common.defaultValue", { value: hint }) : "")}
        aria-invalid={unreadable || undefined}
        oninput={(event) => {
          unreadable = event.currentTarget.validity?.badInput === true;
          if (!unreadable) onchange(event.currentTarget.value);
        }}
      />
    {/if}
  {/if}
  {#if unreadable}<p class="form-error" role="alert">{t("common.unreadableNumber")}</p>{/if}
  {#if description}<p class="desc" lang={description.lang}>{description.text}</p>{/if}
  {#if risk || live || departedText}
    <p class="badges">
      {#if risk}<span class="badge risk" title={t(`settings.risk.${risk}.hint`)}>{t(`settings.risk.${risk}`)}</span>{/if}
      {#if live}<span class="badge live" title={t("settings.edit.liveHint", { live: previewValue(live.effective_value), saved: previewValue(live.config_value) })}>{t("settings.edit.live", { value: previewValue(live.effective_value) })}</span>{/if}
      {#if departedText}<span class="badge default">{t("settings.edit.defaultBadge", { value: departedText })}</span>{/if}
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
  .badge.default { color: var(--m-muted); background: transparent; box-shadow: inset 0 0 0 1px var(--m-line-strong); }
  .live { color: var(--m-accent); }
</style>
