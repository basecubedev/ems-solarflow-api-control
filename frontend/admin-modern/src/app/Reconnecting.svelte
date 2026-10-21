<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import { t } from "../i18n/i18n.svelte.js";
</script>

<div class="reconnecting" role="status" aria-live="polite" data-testid="reconnecting">
  <Icon name="loader" size="sm" />{t("app.reconnecting")}
</div>

<style>
  .reconnecting {
    position: fixed; top: 12px; left: 50%; transform: translateX(-50%); z-index: 60;
    display: inline-flex; align-items: center; gap: 8px; padding: 8px 14px; border-radius: var(--m-radius-pill);
    background: var(--m-surface-3); border: 1px solid color-mix(in srgb, var(--m-warn) 45%, transparent);
    color: var(--m-text); font-size: 12.5px; box-shadow: var(--m-shadow-pop);
  }
</style>
