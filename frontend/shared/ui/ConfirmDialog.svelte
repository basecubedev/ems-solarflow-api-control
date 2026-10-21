<script>
  import { t } from "../i18n/i18n.svelte.js";

  let { open = false, title = "", lines = [], confirmLabel = "", danger = false, onconfirm, oncancel } = $props();
  let dialog = $state();
  const uid = $props.id();
  const titleId = `confirm-${uid}`;

  $effect(() => {
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  });
</script>

<dialog class="modal" bind:this={dialog} onclose={() => open && oncancel?.()} aria-labelledby={titleId}>
  <h2 class="card-title" id={titleId}>{title}</h2>
  {#each lines as line, index (index)}<p class="line">{line}</p>{/each}
  <div class="modal-actions">
    <button class="btn secondary" type="button" onclick={() => oncancel?.()}>{t("auth.cancel")}</button>
    <button class="btn" class:danger type="button" onclick={() => onconfirm?.()} data-testid="confirm">{confirmLabel || t("common.save")}</button>
  </div>
</dialog>

<style>
  .line { color: var(--m-muted); font-size: 13.5px; margin: var(--s3) 0 0; }
</style>
