<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import Empty from "../../../shared/ui/Empty.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import SearchField from "../../../shared/ui/SearchField.svelte";
  import DeviceCard from "./DeviceCard.svelte";
  import { t, tn } from "../i18n/i18n.svelte.js";
  import { LIVE_LABELS, groupKey, groupValues, likelyValue } from "../lib/mqtt.js";

  /**
   * The live topics, one folded device card per group, each saying whether a
   * mapping reads it. onmap(group) offers an unmapped group for mapping and
   * onedit(id) the mapping of a mapped one whose id is in editable, saying
   * when one of its ids is in waiting, the EMS changes a restart has yet to read;
   * onuse(topic, path) offers each value of a mappable group to the editor,
   * under useLabel, and marks the ones in used ("topic|path") with usedLabel.
   * opened names the groups that start unfolded and stay first; whereText says
   * a broker of also_on in the reader's words.
   */
  let { groups = [], busy = false, onmap = null, onedit = null, editable = new Set(), waiting = new Set(), onuse = null, useLabel = "", usedLabel = "", used = new Set(), opened = [], whereText = (where) => where } = $props();

  const uid = $props.id();
  const pinned = initiallyOpen();
  let filter = $state("");
  let open = $state.raw(new Set(pinned));

  const ordered = $derived(pinned.size ? [...groups.filter((group) => pinned.has(groupKey(group))), ...groups.filter((group) => !pinned.has(groupKey(group)))] : groups);
  const shown = $derived.by(() => {
    const words = filter.trim().toLowerCase();
    return words ? ordered.filter((group) => matches(group, words)) : ordered;
  });
  const allOpen = $derived(shown.length > 0 && shown.every((group) => open.has(groupKey(group))));

  function initiallyOpen() {
    return new Set(opened);
  }

  function labelOf(group) {
    return LIVE_LABELS.includes(group.label) ? t(`mqtt.label.${group.label}`) : group.label;
  }

  function toneOf(group) {
    if (group.label === "unknown") return "c-idle";
    return group.mappable ? "c-inv" : "c-pv";
  }

  function matches(group, words) {
    const texts = [group.key, labelOf(group), ...group.topics.flatMap((topic) => [topic.topic, ...topic.values.map((value) => value.path || "")])];
    return texts.some((text) => String(text).toLowerCase().includes(words));
  }

  function setOpen(group, value) {
    const next = new Set(open);
    if (value) next.add(groupKey(group));
    else next.delete(groupKey(group));
    open = next;
  }

  function toggleAll() {
    const next = new Set(open);
    for (const group of shown) {
      if (allOpen) next.delete(groupKey(group));
      else next.add(groupKey(group));
    }
    open = next;
  }

  function mappedIds(group) {
    return [...new Set(group.topics.flatMap((topic) => topic.values.flatMap((value) => value.mapped_to || [])))];
  }

  /** Which mappings read a group, and which of them the running EMS has yet to read, named when not all of them. */
  function mappedText(ids) {
    const mapped = t("mqtt.topics.mapped", { ids: ids.join(", ") });
    const waits = ids.filter((id) => waiting.has(id));
    if (!waits.length) return mapped;
    return `${mapped} · ${t("mqtt.mapping.waitsRestart")}${waits.length < ids.length ? `: ${waits.join(", ")}` : ""}`;
  }

  function summary(group) {
    const value = likelyValue(groupValues(group), "power");
    const count = tn("mqtt.topics.count", group.topics.length);
    return value ? `${count} · ${value.path || t("mqtt.topics.whole")} ${value.value}` : count;
  }
</script>

{#if groups.length}
  <div class="row-wrap">
    <SearchField bind:value={filter} label={t("mqtt.topics.filter")} testid="mqtt-topics-filter" />
    <button class="btn secondary small" type="button" onclick={toggleAll} disabled={!shown.length} data-testid="mqtt-topics-toggle-all"><Icon name={allOpen ? "chevron-up" : "chevron-down"} size="sm" />{allOpen ? t("mqtt.topics.collapseAll") : t("mqtt.topics.expandAll")}</button>
  </div>
  {#if !shown.length}<div data-testid="mqtt-topics-no-match"><Empty text={t("mqtt.topics.noMatch")} icon="search" /></div>{/if}
{/if}
<ul class="list" data-testid="mqtt-topic-groups">
  {#each shown as group, index (groupKey(group))}
    <li class="group" data-label={group.label} data-key={group.key}>
      <DeviceCard
        id={`${uid}-${index}`}
        icon="radio-tower"
        tone={toneOf(group)}
        title={group.key}
        model={labelOf(group)}
        meta={summary(group)}
        bind:expanded={() => open.has(groupKey(group)), (value) => setOpen(group, value)}
      >
        {#snippet side()}
          {#if !group.mappable}
            <span class="muted small">{group.label === "ems_output" ? t("mqtt.topics.output") : t("mqtt.topics.native")}</span>
          {:else}
            {@const ids = mappedIds(group)}
            {@const own = ids.find((id) => editable.has(id))}
            <Status tone={!ids.length ? "c-idle" : ids.some((id) => waiting.has(id)) ? "c-warn" : "c-ok"} text={ids.length ? mappedText(ids) : t("mqtt.topics.unmapped")} />
            {#if own && onedit}
              <button class="btn secondary small" type="button" onclick={() => onedit(own)} disabled={busy} data-testid="mqtt-edit-group"><Icon name="wrench" size="sm" />{t("mqtt.mapping.edit")}</button>
            {:else if onmap}
              <button class="btn secondary small" type="button" onclick={() => onmap(group)} disabled={busy} data-testid="mqtt-map-group"><Icon name="plus" size="sm" />{t("mqtt.topics.mapGroup")}</button>
            {/if}
          {/if}
        {/snippet}
        {#each group.topics as topic (topic.topic)}
          <div class="topic-row">
            <code class="topic">{topic.topic}</code>
            <span class="muted small">{t("mqtt.topics.rate", { rate: topic.messages_per_minute })}{topic.retained ? ` · ${t("mqtt.topics.retained")}` : ""}</span>
            {#each topic.values as value (`${value.path}`)}
              {@const taken = used.has(`${topic.topic}|${value.path ?? ""}`)}
              <div class="value-row">
                <span class:likely={Boolean(value.likely)}>{value.path || t("mqtt.topics.whole")}</span>
                <strong>{value.value}</strong>
                {#if value.mapped_to.length}<span class="muted small">{t("mqtt.topics.mappedTo", { ids: value.mapped_to.join(", ") })}</span>{/if}
                {#if onuse && group.mappable}
                  <button class="btn secondary small" type="button" onclick={() => onuse(topic.topic, value.path)} disabled={taken} aria-pressed={taken ? "true" : "false"} data-testid="mqtt-use-value">{#if taken}<Icon name="circle-check" size="sm" />{usedLabel}{:else}{useLabel}{/if}</button>
                {/if}
              </div>
            {/each}
          </div>
        {/each}
      </DeviceCard>
      {#if group.also_on?.length}
        <p class="note warn small" data-testid="mqtt-topic-also-on"><Icon name="triangle-alert" size="sm" />{t("mqtt.topics.alsoOn", { brokers: group.also_on.map(whereText).join(", ") })}</p>
      {/if}
    </li>
  {/each}
</ul>

<style>
  .list { list-style: none; margin: 0; padding: 0; display: grid; gap: var(--s2); }
  .list > li { display: grid; gap: var(--s1); }
  .topic-row { display: grid; gap: var(--s1); }
  .value-row { display: flex; flex-wrap: wrap; gap: var(--s2); align-items: center; padding-left: var(--s3); }
  .topic { overflow-wrap: anywhere; }
  .likely { font-weight: 600; }
</style>
