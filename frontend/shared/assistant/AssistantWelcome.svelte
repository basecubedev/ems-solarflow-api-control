<script>
  import Icon from "../ui/Icon.svelte";
  import { t } from "../i18n/i18n.svelte.js";

  /** The assistant's greeting: what the installation is, in one line, and the way to the overview's findings when there are any. */
  let { lead, kind = "", attention = "", attentionHref = "" } = $props();
</script>

<section class="card welcome span-12" aria-labelledby="assistant-welcome" data-kind={kind} data-testid="assistant-welcome">
  <div class="welcome-head">
    <span class="icon-tile c-out"><Icon name="compass" /></span>
    <div class="welcome-words">
      <h2 class="welcome-title" id="assistant-welcome">{t("assistant.welcome")}</h2>
      <p class="muted">{lead}</p>
    </div>
  </div>
  {#if attention}
    <a class="attention inner c-warn" href={attentionHref} data-testid="assistant-attention">
      <Icon name="triangle-alert" />
      <strong>{attention}</strong>
      <span class="muted">{t("assistant.toOverview")}</span>
      <Icon name="chevron-right" size="sm" />
    </a>
  {/if}
</section>

<style>
  .welcome { display: flex; flex-direction: column; gap: var(--s4); border-color: color-mix(in srgb, var(--output) 30%, var(--m-line)); }
  .welcome-head { display: flex; align-items: center; gap: var(--s4); }
  .welcome-head > .icon-tile { width: 48px; height: 48px; border-radius: 14px; }
  .welcome-head > .icon-tile :global(svg.i) { width: 24px; height: 24px; }
  .welcome-words { min-width: 0; }
  .attention { display: flex; align-items: center; gap: var(--s2); flex-wrap: wrap; text-decoration: none; border: 1px solid color-mix(in srgb, var(--c) 40%, var(--m-line)); }
  .attention:hover { background: var(--m-surface-3); }
  .attention > :global(svg.i:first-child) { color: var(--c); }
  .attention > :global(svg.i:last-child) { color: var(--m-muted-2); margin-left: auto; }
  .welcome-title { margin: 0; font-size: 19px; font-weight: 650; letter-spacing: -0.01em; }
  .welcome-words p { margin: 3px 0 0; }
  @container content (max-width: 560px) {
    .welcome-head { gap: var(--s3); }
    .welcome-head > .icon-tile { width: 40px; height: 40px; border-radius: 12px; }
  }
</style>
