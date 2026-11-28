<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import { t, catalogText, optionText } from "../i18n/i18n.svelte.js";
  import { RISKS, choices, controlFor, defaultHint, barFor, departedDefault, nameableDefault, shownValue, stepFor } from "../lib/settings.js";
  import { monthsOf, seasonText } from "../../../shared/lib/controls.js";
  import { localeFor } from "../../../shared/lib/format.js";
  import { prefs } from "../../../shared/state/prefs.svelte.js";
  import MonthPicker from "../../../shared/ui/MonthPicker.svelte";
  import Segmented from "../../../shared/ui/Segmented.svelte";
  import Stepper from "../../../shared/ui/Stepper.svelte";
  import Switch from "../../../shared/ui/Switch.svelte";

  /**
   * lang "catalog": the label, help and unit are the settings catalogue's, in
   * the reader's language where this build has them. null: they are the
   * console's own words. "en": the server's English, as it is.
   * descriptionLang, when given, is the help text's own language (null: the
   * page's), for a field whose label and help come from different places.
   * A number the browser cannot read changes nothing, so a typo is never taken
   * for an emptied field, which would take the default.
   * The control follows the field (controlFor): a switch, two or three choices
   * side by side, month chips, a stepper with the unit in it for a number with
   * a unit or a bar, a list, or a plain input.
   * A switch, the choices and the months show the default while unset, since
   * that is what the EMS applies. A stepper with a bar ticks its default on
   * the bar and names it in a tip, instead of the badge below.
   * operating, when given, is the line naming the operating value the EMS runs
   * below this limit, or beside this activation: { text, hint }.
   */
  let { field, value, operating = null, hideRisk = false, defaultValue = undefined, lang = "catalog", descriptionLang = undefined, placeholder = "", type = "", disabled = false, onchange } = $props();

  let unreadable = $state(false);
  const uid = $props.id();

  $effect(() => {
    value;
    unreadable = false;
  });
  const id = `setting-${uid}`;
  const labelId = `${id}-label`;
  const control = $derived(controlFor(field, value, type));
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
  const segments = $derived(control === "segmented" ? choices(field, value).map((option) => ({ value: option, label: optionWords(option) })) : []);
  const chosen = $derived(shown !== "" ? shown : standard !== undefined ? String(standard) : "");
  const months = $derived(monthsOf(value).length ? value : fallback);
  const start = $derived(hint !== "" && Number.isFinite(Number(hint)) ? Number(hint) : null);
  const marked = $derived(control === "stepper" && barFor(field) !== null && standard !== undefined && Number.isFinite(Number(standard)));
  const markText = $derived(marked ? t("common.defaultIs", { value: unit ? `${standard} ${unit.text}` : String(standard) }) : "");
  const typed = (event) => {
    unreadable = event.currentTarget.validity?.badInput === true;
    if (!unreadable) onchange(event.currentTarget.value);
  };
  const departedText = $derived(
    departed === undefined ? ""
      : field.type === "boolean" ? t(departed ? "common.on" : "common.off")
      : field.type === "month_list" ? seasonText(departed, localeFor(prefs.lang), t("months.allYear"))
      : Array.isArray(departed) ? departed.join(", ") : valueWords(departed),
  );
</script>

<div class="setting" data-path={field.path}>
  {#if control === "switch"}
    <div class="switch-row">
      <Switch {id} checked={shown} {disabled} onchange={(checked) => onchange(checked)} />
      <label class="head" for={id}><span class="label" lang={label.lang}>{label.text}</span></label>
    </div>
  {:else if control === "segmented" || control === "months"}
    <span class="head" id={labelId}><span class="label" lang={label.lang}>{label.text}</span>{#if unit}<span class="unit" lang={unit.lang}>{unit.text}</span>{/if}</span>
    {#if control === "segmented"}
      <Segmented options={segments} value={chosen} label={label.text} choice {disabled} onchange={(option) => onchange(option)} />
    {:else}
      <MonthPicker value={months} {disabled} labelledby={labelId} onchange={(list) => onchange(list)} />
    {/if}
  {:else}
    <label class="head" for={id}><span class="label" lang={label.lang}>{label.text}</span>{#if unit && control !== "stepper"}<span class="unit" lang={unit.lang}>{unit.text}</span>{/if}</label>
    {#if control === "select"}
      <select {id} class="input" value={shown} {disabled} onchange={(event) => onchange(event.currentTarget.value)}>
        {#if !shown}<option value="">{hint ? t("common.defaultValue", { value: hintText }) : "—"}</option>{/if}
        {#each choices(field, value) as option (option)}<option value={option}>{optionWords(option)}{shown && standard !== undefined && option === String(standard) ? ` ${t("common.defaultNote")}` : ""}</option>{/each}
      </select>
    {:else if control === "stepper"}
      <Stepper
        {id}
        value={shown}
        step={stepFor(field)}
        min={field.min}
        max={field.max}
        {start}
        {unit}
        bar={barFor(field)}
        placeholder={placeholder || hint}
        note={!placeholder && hint ? t("common.defaultNote") : ""}
        mark={marked ? Number(standard) : null}
        {markText}
        {disabled}
        invalid={unreadable}
        oninput={typed}
        onstep={(text) => {
          unreadable = false;
          onchange(text);
        }}
      />
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
        oninput={typed}
      />
    {/if}
  {/if}
  {#if unreadable}<p class="form-error" role="alert">{t("common.unreadableNumber")}</p>{/if}
  {#if description}<p class="desc" lang={description.lang}>{description.text}</p>{/if}
  {#if risk || operating || (departedText && !marked)}
    <p class="badges">
      {#if risk}<span class="badge warn" title={t(`settings.risk.${risk}.hint`)}>{t(`settings.risk.${risk}`)}</span>{/if}
      {#if operating}<span class="badge operating num" title={operating.hint} data-testid="operating-value">{operating.text}</span>{/if}
      {#if departedText && !marked}<span class="badge default">{t("common.defaultIs", { value: departedText })}</span>{/if}
    </p>
  {/if}
</div>

<style>
  .setting { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
  .setting > .head, .setting > .switch-row { margin-bottom: 2px; }
  .head { display: flex; align-items: baseline; gap: var(--s2); font-size: 13.5px; font-weight: 560; }
  .switch-row { display: flex; align-items: center; gap: 10px; min-height: 34px; }
  .switch-row .head { min-width: 0; cursor: pointer; }
  .setting > :global(.seg) { align-self: flex-start; max-width: 100%; }
  .unit { color: var(--m-muted); font-size: 12px; font-weight: 450; }
  .desc { margin: 0; color: var(--m-muted); font-size: 12.5px; overflow-wrap: anywhere; }
  .badges { display: flex; flex-wrap: wrap; gap: var(--s2); margin: 0; }
  .badge.default { color: var(--m-muted); background: transparent; box-shadow: inset 0 0 0 1px var(--m-line-strong); }
  .live { color: var(--m-accent); }
</style>
