<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { system, reload } from "../state/system.svelte.js";
  import { upgrade } from "../state/upgrade.svelte.js";
  import { adminSend } from "../state/auth.svelte.js";
  import { alignmentView } from "../lib/workflow.js";
  import { errorView } from "../lib/errors.js";

  /**
   * A system alignment holds the console, as going back to the version that
   * runs leaves one when the Admin console already ran it. Continue sends the
   * server's resume, as Classic's Resume does; the server decides the step.
   */
  const view = $derived(upgrade.stage === "returned" && !upgrade.returnAligns ? null : alignmentView(system.alignment.data?.transition));
  let busy = $state(false);
  let failure = $state.raw(null);
  let restarting = $state(false);

  async function proceed() {
    if (busy || !view?.canContinue) return;
    busy = true;
    failure = null;
    try {
      const data = await adminSend("/api/admin/system-alignment/resume", { body: { operation_id: view.operationId, tag: view.tag } });
      restarting = data?.reconnect === true || data?.status === "admin_alignment_started";
    } catch (error) {
      failure = errorView(error, t);
    } finally {
      busy = false;
      reload("alignment", "lifecycle", "overview");
    }
  }
</script>

{#if view}
  <Card title={t("alignment.title")} subtitle={t("alignment.sub")} span="span-12">
    <div class="alignment" data-testid="alignment">
      <p class="state"><Icon name={view.stage === "failed_recoverable" ? "circle-x" : "route"} />{t(`alignment.stage.${view.stage}`)}</p>
      {#if view.error}<p class="note warn"><Icon name="triangle-alert" /><span lang="en">{view.error}</span></p>{/if}
      {#if restarting}<p class="note"><Icon name="info" />{t("alignment.restarting")}</p>{/if}
      {#if failure}<p class="form-error" lang={failure.lang}>{failure.text}</p>{/if}
      <div class="row-wrap actions">
        {#if view.canContinue}<button class="btn" type="button" onclick={proceed} disabled={busy} data-testid="alignment-continue"><Icon name="play" size="sm" />{busy ? t("alignment.continuing") : t("alignment.continue")}</button>{/if}
        {#if view.release}<a class="btn" href="#/system" data-testid="alignment-release"><Icon name="arrow-right" size="sm" />{t("alignment.release")}</a>{:else if system.lifecycle.data?.recoverable}<a class="btn secondary small" href="#/system">{t("workflow.openRecovery")}</a>{/if}
      </div>
    </div>
  </Card>
{/if}

<style>
  .alignment { display: flex; flex-direction: column; gap: var(--s3); }
  .state { display: flex; align-items: center; gap: var(--s2); margin: 0; font-weight: 560; }
  .actions { gap: var(--s2); }
</style>
