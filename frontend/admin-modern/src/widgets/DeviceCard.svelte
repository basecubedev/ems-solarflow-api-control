<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import { t } from "../i18n/i18n.svelte.js";

  let { id, title, model = "", modelLang = undefined, meta = "", enabled = true, status = "", connection = "", onremove = null, open = false, icon = "", tone = "", children } = $props();

  let expanded = $state(open);
  const bodyId = $derived(`device-${String(id).replace(/[^a-z0-9]/gi, "-")}`);
</script>

<section class="device inner" class:off={!enabled} data-device={id}>
  <div class="head">
    <button class="summary" type="button" aria-expanded={expanded ? "true" : "false"} aria-controls={bodyId} onclick={() => (expanded = !expanded)}>
      {#if icon}<span class="icon-tile sm {tone}"><Icon name={icon} /></span>{/if}
      <span class="words">
        <span class="name">{title}</span>
        {#if model}<span class="muted small" lang={modelLang}>{model}</span>{/if}
        {#if meta}<span class="muted small num">{meta}</span>{/if}
      </span>
      <Icon name={expanded ? "chevron-up" : "chevron-down"} size="sm" />
    </button>
    <div class="side">
      {#if connection}<span class="badge">{connection}</span>{/if}
      <Status tone={enabled ? "c-ok" : "c-idle"} text={status || (enabled ? t("devices.on") : t("devices.off"))} />
      {#if onremove}<button class="btn secondary small" type="button" onclick={onremove} data-testid="device-remove"><Icon name="trash-2" size="sm" />{t("devices.remove")}</button>{/if}
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
  .name { font-weight: 600; }
  .small { font-size: 12.5px; overflow-wrap: anywhere; }
  .side { display: flex; align-items: center; gap: var(--s2); flex-wrap: wrap; }
  .body { display: flex; flex-direction: column; gap: var(--s4); }
  .body[hidden] { display: none; }
</style>
