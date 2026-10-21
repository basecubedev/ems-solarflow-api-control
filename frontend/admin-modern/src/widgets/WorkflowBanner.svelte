<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { system, ensure } from "../state/system.svelte.js";
  import { activeWorkflow, ownerKey, stateKey } from "../lib/workflow.js";
  import { classicPage } from "../state/prefs.svelte.js";
  import { href, router } from "../app/router.svelte.js";

  /**
   * A Guided Setup or Upgrade holds the console. It is said on every page:
   * a Guided Upgrade with the way to this console's Updates page, which
   * follows it, and a Guided Setup with the hand-over to Classic, which
   * resumes it from the server's durable record. The page that already shows
   * the workflow needs no banner for it.
   */
  ensure("lifecycle");
  const workflow = $derived(activeWorkflow(system.lifecycle.data));
</script>

{#if workflow && !(workflow.native && router.page === workflow.native)}
  <div class="banner span-12" role="status" data-testid="workflow-banner">
    <span class="icon-tile sm c-warn"><Icon name="route" /></span>
    <div class="text">
      <strong>{workflow.owner === "conflict" ? t("workflow.bannerConflict") : t("workflow.banner", { owner: t(ownerKey(workflow.owner)) })}</strong>
      <span class="muted">{t(stateKey(workflow.state))}</span>
    </div>
    <div class="actions">
      {#if workflow.recoverable}<a class="btn secondary small" href="#/system">{t("workflow.openRecovery")}</a>{/if}
      {#if workflow.native}
        <a class="btn small" href={href(workflow.native)} data-testid="workflow-continue"><Icon name="circle-arrow-up" size="sm" />{t("workflow.showUpgrade")}</a>
      {:else}
        <a class="btn small" href={classicPage(workflow.classic)} data-testid="workflow-continue"><Icon name="arrow-up-right" size="sm" />{t("workflow.continue")}</a>
      {/if}
    </div>
  </div>
{/if}

<style>
  .banner {
    display: flex; align-items: center; gap: var(--s3); flex-wrap: wrap; padding: var(--s3) var(--s4);
    border-radius: var(--m-radius-card); border: 1px solid color-mix(in srgb, var(--m-warn) 40%, transparent);
    background: color-mix(in srgb, var(--m-warn) 7%, var(--m-surface-1));
  }
  .text { display: flex; flex-direction: column; gap: 2px; flex: 1 1 260px; min-width: 0; }
  .actions { display: flex; gap: var(--s2); flex-wrap: wrap; }
</style>
