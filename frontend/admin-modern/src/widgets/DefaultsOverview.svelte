<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import Empty from "../../../shared/ui/Empty.svelte";
  import CatalogText from "./CatalogText.svelte";
  import { t, catalogText, optionText } from "../i18n/i18n.svelte.js";
  import { FEATURE_IDS, FEATURE_LOOKS, SECTION_LOOK, spelledSwitch } from "../lib/settings.js";

  /**
   * The settings that depart from this release's defaults (lib/settings.js,
   * departures), one group per section or device. Information only: ``onshow``
   * takes the reader to the field, where a value is changed as anywhere else.
   */
  let { groups, searching = false, onshow } = $props();

  function words(field, value) {
    if (spelledSwitch(field, value)) return JSON.stringify(value);
    if (field.type === "boolean") return t(value ? "common.on" : "common.off");
    if (Array.isArray(value)) return value.join(", ");
    const text = String(value);
    if ((field.options || []).map(String).includes(text)) return field.optionLabels?.[text] ?? optionText(text);
    return text;
  }

  function look(group) {
    if (group.device) return { icon: "zap", tone: "c-inv" };
    return FEATURE_LOOKS[group.section.id] || SECTION_LOOK;
  }
</script>

{#if !groups.length}
  <Empty text={t(searching ? "settings.edit.noMatch" : "settings.defaults.none")} icon={searching ? "search" : "circle-check"} />
{:else}
  <div class="groups" data-testid="settings-defaults">
    {#each groups as group (group.id)}
      {@const mark = look(group)}
      <section class="group" data-group={group.id}>
        <h3 class="title">
          <span class="icon-tile sm {mark.tone}" aria-hidden="true"><Icon name={mark.icon} /></span>
          {#if group.device}
            <span>{group.device.name || t("settings.unnamed")}</span>
          {:else if FEATURE_IDS.includes(group.section.id)}
            <span>{t(`feature.${group.section.id}`)}</span>
          {:else}
            <span><CatalogText text={group.section.title || group.section.id} /></span>
          {/if}
          <span class="muted num">({group.rows.length})</span>
        </h3>
        <ul class="rows">
          {#each group.rows as row (row.field.path)}
            {@const label = catalogText(row.field.label || row.field.path)}
            {@const unit = row.field.unit ? catalogText(row.field.unit) : null}
            <li class="row" data-path={row.field.path}>
              <span class="words">
                <span class="head"><span class="label" lang={label.lang}>{label.text}</span>{#if unit}<span class="unit" lang={unit.lang}>{unit.text}</span>{/if}</span>
                <code class="path">{row.field.path}</code>
              </span>
              <span class="badges">
                {#if row.value !== undefined}<span class="badge saved num">{t("settings.defaults.saved", { value: words(row.field, row.value) })}</span>{/if}
                <span class="badge default num">{t("common.defaultIs", { value: words(row.field, row.standard) })}</span>
              </span>
              <button class="btn secondary small" type="button" aria-label={t("settings.defaults.showField", { name: label.text })} onclick={() => onshow(row)}><Icon name="eye" size="sm" />{t("settings.defaults.show")}</button>
            </li>
          {/each}
        </ul>
      </section>
    {/each}
  </div>
{/if}

<style>
  .groups { display: flex; flex-direction: column; gap: var(--s4); }
  .group + .group { border-top: 1px solid var(--m-line); padding-top: var(--s4); }
  .title { display: flex; align-items: center; gap: var(--s2); font-size: 13.5px; font-weight: 620; margin: 0 0 var(--s2); }
  .rows { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; }
  .row { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; align-items: center; gap: var(--s2) var(--s4); padding: var(--s2) 0; }
  .row + .row { border-top: 1px dashed var(--m-line); }
  .words { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .head { display: flex; align-items: baseline; flex-wrap: wrap; gap: var(--s2); }
  .label { font-size: 13.5px; font-weight: 560; overflow-wrap: anywhere; }
  .unit { color: var(--m-muted); font-size: 12px; font-weight: 450; }
  .path { color: var(--m-muted); font-size: 11.5px; overflow-wrap: anywhere; }
  .badges { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: var(--s2); }
  .badge.default { color: var(--m-muted); background: transparent; box-shadow: inset 0 0 0 1px var(--m-line-strong); }
  @container content (max-width: 720px) {
    .row { grid-template-columns: minmax(0, 1fr) auto; }
    .badges { grid-column: 1 / -1; grid-row: 2; justify-content: flex-start; }
  }
</style>
