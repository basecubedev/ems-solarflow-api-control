<script>
  import Icon from "./Icon.svelte";
  import { t } from "../i18n/i18n.svelte.js";

  /**
   * A native modal <dialog> with a title, its content and a row of actions:
   * focus stays inside, Esc and the close button call onclose. For a plain
   * yes/no about a write, ConfirmDialog says what will change.
   */
  let { open = false, title = "", wide = false, onclose, children, actions } = $props();
  let dialog = $state();
  const uid = $props.id();
  const titleId = `dialog-${uid}`;

  $effect(() => {
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  });
</script>

<dialog class="modal" class:wide bind:this={dialog} onclose={() => open && onclose?.()} aria-labelledby={titleId}>
  <div class="dialog-head">
    <h2 class="card-title" id={titleId}>{title}</h2>
    <button class="icon-btn" type="button" onclick={() => onclose?.()} aria-label={t("common.close")} title={t("common.close")}><Icon name="x" /></button>
  </div>
  <div class="dialog-body">{@render children?.()}</div>
  {#if actions}<div class="modal-actions">{@render actions()}</div>{/if}
</dialog>

<style>
  dialog.wide { width: min(640px, calc(100vw - 32px)); }
  .dialog-head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--s3); }
  .dialog-head .card-title { padding-top: 6px; overflow-wrap: anywhere; }
  .dialog-body { margin-top: var(--s3); max-height: min(60vh, 560px); overflow-y: auto; overscroll-behavior: contain; }
</style>
