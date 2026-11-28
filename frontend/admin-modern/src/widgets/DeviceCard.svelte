<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import { untrack } from "svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import Switch from "../../../shared/ui/Switch.svelte";
  import { t } from "../i18n/i18n.svelte.js";

  /**
   * side replaces the connection, state and remove button on the right; open is the first fold, expanded binds it to an owner; flag names something in the body to look at.
   * onenable puts the device's own switch at the right end of the head, as a feature card has, in place of the pill that only names its state.
   */
  let { id, title, model = "", modelLang = undefined, meta = "", enabled = true, status = "", connection = "", flag = "", onremove = null, onenable = null, open = false, expanded = $bindable(untrack(() => open)), icon = "", tone = "", side, children } = $props();

  const bodyId = $derived(`device-${String(id).replace(/[^a-z0-9]/gi, "-")}`);
</script>

<section class="device inner" class:off={!enabled} data-device={id}>
  <div class="head">
    <button class="summary" type="button" aria-expanded={expanded ? "true" : "false"} aria-controls={bodyId} onclick={() => (expanded = !expanded)}>
      <span class="fold" aria-hidden="true"><Icon name="chevron-right" size="sm" /></span>
      {#if icon}<span class="icon-tile sm {enabled ? tone : 'c-idle'}"><Icon name={icon} /></span>{/if}
      <span class="words">
        <span class="name">{title}</span>
        {#if model}<span class="muted small" lang={modelLang}>{model}</span>{/if}
        {#if meta}<span class="muted small num">{meta}</span>{/if}
      </span>
    </button>
    <div class="side">
      {#if side}
        {@render side()}
      {:else}
        {#if connection}<span class="badge">{connection}</span>{/if}
        {#if flag}<Status tone="c-warn" icon="triangle-alert" text={flag} />{/if}
        {#if !onenable}<Status tone={enabled ? "c-ok" : "c-idle"} text={status || (enabled ? t("devices.on") : t("devices.off"))} />{/if}
        {#if onremove}<button class="btn secondary small" type="button" onclick={onremove} data-testid="device-remove"><Icon name="trash-2" size="sm" />{t("devices.remove")}</button>{/if}
        {#if onenable}<Switch checked={enabled} label={t("settings.edit.switch", { name: title })} onchange={onenable} />{/if}
      {/if}
    </div>
  </div>
  <div class="body" id={bodyId} hidden={!expanded}>{@render children?.()}</div>
</section>

<style>
  .device { display: flex; flex-direction: column; gap: var(--s3); }
  .device.off .name { color: var(--m-muted); }
  .head { display: flex; align-items: center; gap: var(--s3); flex-wrap: wrap; }
  .summary { all: unset; cursor: pointer; display: flex; align-items: center; gap: var(--s3); flex: 1 1 240px; min-width: 0; }
  .summary:focus-visible { outline: 2px solid var(--m-accent); outline-offset: 4px; border-radius: 6px; }
  .words { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
  .fold { display: grid; place-items: center; flex: none; color: var(--m-muted); transition: transform 0.15s ease; }
  .summary[aria-expanded="true"] .fold { transform: rotate(90deg); }
  .summary:hover .fold { color: var(--m-text); }
  .name { font-weight: 600; }
  .small { font-size: 12.5px; overflow-wrap: anywhere; }
  .side { display: flex; align-items: center; gap: var(--s2); flex-wrap: wrap; }
  .body { display: flex; flex-direction: column; gap: var(--s4); }
  .body[hidden] { display: none; }
</style>
