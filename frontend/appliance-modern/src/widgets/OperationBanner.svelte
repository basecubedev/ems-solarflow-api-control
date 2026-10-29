<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import OperationDetails from "./OperationDetails.svelte";
  import { t, say, langOf, fmt } from "../i18n/i18n.svelte.js";
  import { host } from "../state/host.svelte.js";
  import { actions, acknowledgeOperation, cancelOperation } from "../state/actions.svelte.js";
  import { acknowledgeable, archiveHref, cancellable, operationStage, operationTitleKey, recentProgress, stateOf } from "../lib/operations.js";

  /**
   * What an owner watches while their appliance is being changed, on every
   * page: what runs, how far it got, how it ended, and the one thing to do
   * about it. It stays until the owner acknowledges the outcome.
   */
  const operation = $derived(host.operation);
  const titleKey = $derived(operationTitleKey(operation));
  const title = $derived(titleKey ? t(titleKey) : String(operation?.type || "").replace(/[._]/g, " ") || t("op.current"));
  const titleLang = $derived(!titleKey && operation?.type ? "en" : undefined);
  const stage = $derived(operationStage(operation));
  const state = $derived(stateOf(operation));
  const steps = $derived(recentProgress(operation));
  const archive = $derived(archiveHref(operation));
  const settled = $derived(Boolean(operation?.settled));
  const icon = $derived(!settled ? "loader" : state.tone === "c-ok" ? "circle-check" : state.tone === "c-idle" ? "circle-minus" : state.tone === "c-warn" ? "triangle-alert" : "circle-x");
</script>

{#if operation}
  <section class="card span-12 operation" data-testid="operation-banner" data-state={operation.state} aria-labelledby="operation-title">
    <div class="head">
      <span class="icon-tile sm {state.tone}"><Icon name={icon} /></span>
      <div class="titles">
        <h2 class="card-title" id="operation-title" lang={titleLang}>{title}</h2>
        {#if stage}<div class="card-sub" lang="en" data-testid="operation-stage">{stage}</div>{/if}
      </div>
      <span data-testid="operation-outcome"><Status tone={state.tone} text={say(state)} lang={langOf(state)} /></span>
    </div>
    {#if operation.error?.message}<p class="error" lang="en" data-testid="operation-error">{operation.error.message}</p>{/if}
    <OperationDetails {operation} />
    {#if steps.length}
      <ol class="steps" aria-label={t("op.progress")}>
        {#each steps as step, index (index)}
          <li class:last={index === steps.length - 1}>
            <span class="mark"></span>
            <span class="what" lang="en">{step.stage}</span>
            {#if step.detail}<span class="detail" lang="en">{step.detail}</span>{/if}
            {#if step.at}<span class="when num">{fmt().time(Number(step.at) * 1000, { seconds: true })}</span>{/if}
          </li>
        {/each}
      </ol>
    {/if}
    <div class="actions">
      {#if archive}<a class="btn small" href={archive} download data-testid="download-support-archive"><Icon name="download" />{t("op.downloadArchive")}</a>{/if}
      {#if acknowledgeable(operation)}
        <button class="btn small" type="button" onclick={acknowledgeOperation} disabled={Boolean(actions.settling)} aria-busy={actions.settling === "acknowledge" ? "true" : undefined} data-testid="acknowledge-operation">
          <Icon name={actions.settling === "acknowledge" ? "loader" : "check"} />{actions.settling === "acknowledge" ? t("op.acknowledging") : t("op.acknowledge")}
        </button>
      {/if}
      {#if cancellable(operation)}
        <button class="btn secondary small" type="button" onclick={cancelOperation} disabled={Boolean(actions.settling)} aria-busy={actions.settling === "cancel" ? "true" : undefined} data-testid="cancel-operation">
          {actions.settling === "cancel" ? t("op.cancelling") : t("op.cancel")}
        </button>
      {:else if !settled}
        <p class="note" data-testid="operation-uninterruptible"><Icon name="info" />{t("op.uninterruptible")}</p>
      {/if}
    </div>
    <p class="sr-only" role="status" aria-live="polite"><span lang={titleLang}>{title}</span>: <span lang={langOf(state)}>{say(state)}</span></p>
  </section>
{/if}

<style>
  .operation { border-color: color-mix(in srgb, var(--m-accent) 30%, var(--m-line)); }
  .head { display: flex; align-items: center; gap: var(--s3); flex-wrap: wrap; }
  .titles { flex: 1 1 200px; min-width: 0; }
  .error { margin: var(--s3) 0 0; color: var(--m-fault); font-size: 13px; }
  .steps { list-style: none; margin: var(--s4) 0 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
  .steps li { display: grid; grid-template-columns: 14px minmax(0, 1fr) auto; column-gap: var(--s2); align-items: baseline; font-size: 13px; color: var(--m-muted); }
  .steps .mark { width: 8px; height: 8px; border-radius: 50%; background: var(--m-muted-2); align-self: center; }
  .steps li.last { color: var(--m-text); }
  .steps li.last .mark { background: var(--m-accent); }
  .steps .detail { grid-column: 2; font-size: 12px; }
  .steps .when { grid-column: 3; grid-row: 1; font-size: 12px; color: var(--m-muted-2); }
  .actions { display: flex; flex-wrap: wrap; gap: var(--s2); align-items: center; margin-top: var(--s4); }
  .actions:empty { display: none; }
  .actions .note { margin: 0; }
</style>
