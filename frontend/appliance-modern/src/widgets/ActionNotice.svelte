<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import { t, say, langOf } from "../i18n/i18n.svelte.js";
  import { actions, dismissNotice } from "../state/actions.svelte.js";
</script>

{#if actions.notice}
  <div class="span-12 notice-bar {actions.notice.tone}" role="alert" data-testid="action-notice">
    <Icon name={actions.notice.tone === "c-fault" ? "circle-x" : "triangle-alert"} />
    <p lang={langOf(actions.notice)}>{say(actions.notice)}</p>
    <button class="icon-btn" type="button" onclick={dismissNotice} aria-label={t("common.close")} title={t("common.close")}><Icon name="x" /></button>
  </div>
{/if}

<style>
  .notice-bar {
    display: flex; align-items: center; gap: var(--s3); padding: var(--s3) var(--s4); border-radius: var(--m-radius-card);
    border: 1px solid color-mix(in srgb, var(--c) 45%, transparent); background: color-mix(in srgb, var(--c) 9%, var(--m-surface-1));
  }
  .notice-bar > :global(svg.i) { color: var(--c); }
  p { margin: 0; flex: 1; }
</style>
