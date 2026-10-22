<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import SettingField from "./SettingField.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { AUTOMATIC_FEATURES, FEATURE_IDS, fieldMatches, levelled, liveOverride, sharedRisk, switchPath, switchedOn } from "../lib/settings.js";
  import { settings, setValue } from "../state/settings.svelte.js";

  /** values and onset: another draft's feature values and its setter, as Guided Setup has them. */
  let { section, query = "", values: given = null, onset = null, startOpen = false } = $props();

  let toggled = $state(null);
  const open = $derived(toggled ?? startOpen);
  const values = $derived(given || settings.draft?.features || {});
  const overrides = $derived(given ? null : settings.saved?.overrides);
  const set = (field, value) => (onset ? onset(field, value) : setValue(field, value));
  const own = $derived(switchPath(section));
  const on = $derived(switchedOn(section, values));
  const switchField = $derived(own ? (section.fields || []).find((field) => field.path === own) || { path: own, type: "boolean" } : null);
  const named = $derived(FEATURE_IDS.includes(section.id));
  const levels = $derived(levelled(section));
  const shown = $derived(Object.fromEntries(Object.entries(levels).map(([level, fields]) => [level, fields.filter((field) => fieldMatches(field, query))])));
  const searching = $derived(Boolean(String(query).trim()));
  const expanded = $derived(open || searching);
  const bodyId = $derived(`section-${String(section.id).replace(/[^a-z0-9]/gi, "-")}`);
</script>

{#snippet fields(list)}
  {@const risk = sharedRisk(list)}
  {#if risk}<p class="note warn"><Icon name="triangle-alert" />{t(`settings.risk.${risk}.shared`)}</p>{/if}
  <div class="fields">
    {#each list as field (field.path)}
      <SettingField {field} value={values[field.path]} live={liveOverride(overrides, field.path)} hideRisk={Boolean(risk)} type={field.secret ? "password" : ""} onchange={(value) => set(field, value)} />
    {/each}
  </div>
{/snippet}

<section class="section inner" class:off={on === false} data-section={section.id}>
  <div class="head">
    {#if switchField}
      <input
        class="switch"
        type="checkbox"
        checked={on}
        onchange={(event) => set(switchField, event.currentTarget.checked)}
        aria-label={t("settings.edit.switch", { name: named ? t(`feature.${section.id}`) : section.title || section.id })}
      />
    {/if}
    <button class="summary" type="button" aria-expanded={expanded ? "true" : "false"} aria-controls={bodyId} onclick={() => (toggled = !open)}>
      <span class="words">
        <span class="name">{#if named}{t(`feature.${section.id}`)}{:else}<span lang="en">{section.title || section.id}</span>{/if}</span>
        <span class="muted small">{#if named}{t(`feature.${section.id}.sub`)}{:else}<span lang="en">{section.summary || section.description || ""}</span>{/if}</span>
      </span>
      {#if on !== null}
        <Status tone={on ? "c-ok" : "c-idle"} text={on ? t("common.on") : t("common.off")} />
      {:else if AUTOMATIC_FEATURES.includes(section.id)}
        <span title={t("settings.featureAutoHint")}><Status tone="c-acc" text={t("settings.featureAuto")} /></span>
      {/if}
      {#if own && liveOverride(overrides, own)}<span class="badge live">{t("settings.overridden")}</span>{/if}
      <Icon name={expanded ? "chevron-up" : "chevron-down"} size="sm" />
    </button>
  </div>
  <div class="body" id={bodyId} hidden={!expanded}>
      {#if shown.normal.length}{@render fields(shown.normal)}{/if}
      {#each ["advanced", "expert"] as level (level)}
        {#if shown[level].length}
          <details class="level" open={searching}>
            <summary>{t(`settings.edit.level.${level}`)} <span class="muted num">({shown[level].length})</span></summary>
            {@render fields(shown[level])}
          </details>
        {/if}
      {/each}
      {#if !shown.normal.length && !shown.advanced.length && !shown.expert.length}<p class="muted small">{t("settings.edit.onlySwitch")}</p>{/if}
  </div>
</section>

<style>
  .section { display: flex; flex-direction: column; gap: var(--s3); }
  .section.off .summary .name { color: var(--m-muted); }
  .head { display: flex; align-items: center; gap: var(--s3); }
  .switch { width: 18px; height: 18px; accent-color: var(--m-accent); flex: none; }
  .summary { all: unset; cursor: pointer; display: flex; align-items: center; gap: var(--s3); flex: 1; min-width: 0; }
  .summary:focus-visible { outline: 2px solid var(--m-accent); outline-offset: 4px; border-radius: 6px; }
  .words { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
  .name { font-weight: 600; }
  .small { font-size: 12.5px; }
  .body { display: flex; flex-direction: column; gap: var(--s3); }
  .body[hidden] { display: none; }
  .fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--s4) var(--s5); }
  .level summary { cursor: pointer; font-size: 13px; font-weight: 600; margin-bottom: var(--s3); }
  .live { color: var(--m-accent); }
  @container content (max-width: 720px) { .fields { grid-template-columns: minmax(0, 1fr); } }
</style>
