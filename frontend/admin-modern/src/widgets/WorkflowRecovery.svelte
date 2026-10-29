<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import Empty from "../../../shared/ui/Empty.svelte";
  import ConfirmDialog from "../../../shared/ui/ConfirmDialog.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { adminSend } from "../state/auth.svelte.js";
  import { reload } from "../state/system.svelte.js";
  import { ownerKey, stateKey, recoveryReason, recoveryWanted, shortReference, staleReasonKey, workflowReference, workflowSince } from "../lib/workflow.js";
  import { errorView, noAnswer } from "../lib/errors.js";

  /**
   * A Guided Setup or Upgrade that can no longer be finished or switched
   * away from. The server previews what a reset would do and binds it to a
   * fingerprint of the state it saw; the reset sends that fingerprint back,
   * so it never acts on a state the operator did not look at.
   */
  let plan = $state.raw(null);
  let error = $state.raw(null);
  let message = $state.raw(null);
  let busy = $state(false);
  let confirming = $state("");
  let loading = $state(true);
  const f = $derived(fmt());
  const lifecycle = $derived(plan?.lifecycle || {});
  const safe = $derived(Boolean(plan?.safe?.available));
  const advanced = $derived(Boolean(plan?.advanced?.available));
  const reference = $derived(workflowReference(lifecycle));
  const since = $derived(workflowSince(lifecycle));

  async function inspect() {
    loading = true;
    error = null;
    try {
      plan = await adminSend("/api/admin/workflow-lifecycle/recovery/preview", { body: {} });
      return plan;
    } catch (err) {
      error = errorView(err, t, t("recovery.unreadable"));
      return null;
    } finally {
      loading = false;
    }
  }

  inspect();

  let confirmedPlan = null;

  /**
   * The state is read again before the question is asked, so the operator
   * confirms what is on screen now -- and exactly that state's fingerprint is
   * sent. If the server's state moved after that, it refuses the reset rather
   * than acting on something nobody looked at.
   */
  async function ask(mode) {
    if (busy) return;
    message = null;
    const current = await inspect();
    if (!current) return;
    const available = mode === "safe" ? current.safe?.available : current.advanced?.available;
    if (!available) {
      message = { tone: "fault", text: t("recovery.notOffered") };
      return;
    }
    confirmedPlan = current;
    confirming = mode;
  }

  async function run(mode) {
    confirming = "";
    const confirmed = confirmedPlan;
    confirmedPlan = null;
    if (busy || !confirmed) return;
    busy = true;
    try {
      await adminSend("/api/admin/workflow-lifecycle/recovery", {
        body: { mode, confirm: true, fingerprint: confirmed.fingerprint, reason: recoveryReason(confirmed) },
      });
      message = { tone: "ok", text: mode === "release_stale_state" ? t("recovery.released") : t("recovery.reset") };
    } catch (err) {
      message = noAnswer(err) ? { tone: "unknown", text: t("error.noAnswer") } : { tone: "fault", ...errorView(err, t, t("recovery.failed")) };
    } finally {
      busy = false;
      await inspect();
      reload("lifecycle", "alignment");
    }
  }

  function summary() {
    if (!plan) return "";
    if (plan.operation_running) return t("recovery.summary.running");
    if (lifecycle.blocking_reason === "workflow_owner_conflict") return t("recovery.summary.conflict");
    if (plan.blocking) return t("recovery.summary.blocked", { owner: t(ownerKey(lifecycle.owner)), state: t(stateKey(lifecycle.state)) });
    if (safe || advanced) return t("recovery.summary.resettable");
    return t("recovery.summary.none");
  }
</script>

{#if loading && !plan}
  <Card icon="wrench" tone="c-pv" title={t("recovery.title")} subtitle={t("recovery.sub")} span="span-12"><Empty text={t("common.loading")} icon="loader" /></Card>
{:else if error || message || recoveryWanted(plan)}
  <Card icon="wrench" tone="c-pv" title={t("recovery.title")} subtitle={t("recovery.sub")} span="span-12" className="recovery">
    {#if error}
      <Empty error {...error}>
        {#snippet action()}<button class="btn secondary small" type="button" onclick={inspect} disabled={loading}><Icon name="refresh-cw" size="sm" />{t("common.retry")}</button>{/snippet}
      </Empty>
    {:else}
      <p class="summary" data-testid="recovery-summary"><Icon name={plan.blocking ? "triangle-alert" : "info"} />{summary()}</p>
      <div class="facts">
        <div class="fact"><div class="fact-label">{t("recovery.what")}</div><div class="fact-value">{t(ownerKey(lifecycle.owner))}</div></div>
        <div class="fact"><div class="fact-label">{t("recovery.state")}</div><div class="fact-value">{t(stateKey(lifecycle.state))}</div></div>
        <div class="fact"><div class="fact-label">{t("recovery.since")}</div><div class="fact-value num" class:na={!since}>{since ? f.when(since) : "—"}</div></div>
      </div>
      <p class="note"><Icon name="shield-check" />{t("recovery.preserves")}</p>
      {#if safe || advanced}
        <details class="more">
          <summary><Icon name="chevron-right" />{t("recovery.details")}</summary>
          <div class="body">
            <div class="facts two">
              <div class="fact"><div class="fact-label">{t("recovery.reference")}</div><div class="fact-value num">{reference ? t(`recovery.reference.${reference.kind}`, { id: reference.id }) : "—"}</div></div>
              <div class="fact"><div class="fact-label">{t("recovery.fingerprint")}</div><div class="fact-value num">{shortReference(plan.fingerprint)}</div></div>
            </div>
            {#if plan.advanced?.files?.length}
              <h3 class="sub">{t("recovery.affected")}</h3>
              <ul class="files">{#each plan.advanced.files as file, index (index)}<li><span class="path">{file.name}</span><span class="muted">{staleReasonKey(file.reason) ? t(staleReasonKey(file.reason)) : file.reason || ""}</span></li>{/each}</ul>
            {/if}
            <h3 class="sub">{t("recovery.alwaysKept")}</h3>
            <p class="muted small path">{(plan.will_preserve || []).join(", ")}</p>
          </div>
        </details>
        <div class="row-wrap actions">
          {#if safe}<button class="btn" type="button" disabled={busy || loading} onclick={() => ask("safe")} data-testid="recovery-safe"><Icon name="rotate-ccw" size="sm" />{t("recovery.safe")}</button>{/if}
          {#if advanced}<button class="btn secondary danger-text" type="button" disabled={busy || loading} onclick={() => ask("release_stale_state")}><Icon name="eraser" size="sm" />{t("recovery.advanced")}</button>{/if}
        </div>
      {/if}
    {/if}
    <div role="status" aria-live="polite">
      {#if message}<p class="outcome {message.tone}"><Icon name={message.tone === "ok" ? "circle-check" : message.tone === "unknown" ? "circle-help" : "circle-x"} /><span lang={message.lang}>{message.text}</span></p>{/if}
    </div>
  </Card>
{/if}

<ConfirmDialog
  open={confirming === "safe"}
  title={t("recovery.safeTitle")}
  lines={[t("recovery.safeConfirm")]}
  confirmLabel={t("recovery.safe")}
  onconfirm={() => run("safe")}
  oncancel={() => {
    confirming = "";
    confirmedPlan = null;
  }}
/>
<ConfirmDialog
  open={confirming === "release_stale_state"}
  title={t("recovery.advancedTitle")}
  lines={[t("recovery.advancedConfirm"), t("recovery.advancedConfirm2")]}
  confirmLabel={t("recovery.advanced")}
  danger
  onconfirm={() => (confirming = "release_stale_state_last")}
  oncancel={() => {
    confirming = "";
    confirmedPlan = null;
  }}
/>
<ConfirmDialog
  open={confirming === "release_stale_state_last"}
  title={t("recovery.advancedLastTitle")}
  lines={[t("recovery.advancedLast")]}
  confirmLabel={t("recovery.advanced")}
  danger
  onconfirm={() => run("release_stale_state")}
  oncancel={() => {
    confirming = "";
    confirmedPlan = null;
  }}
/>

<style>
  .summary { display: flex; gap: 8px; align-items: flex-start; margin: 0 0 var(--s3); font-weight: 560; }
  .sub { font-size: 13px; font-weight: 620; margin: var(--s3) 0 var(--s2); }
  .files { list-style: none; margin: 0; padding: 0; }
  .files li { display: flex; justify-content: space-between; gap: var(--s3); padding: 6px 0; border-bottom: 1px solid var(--m-line); font-size: 13px; flex-wrap: wrap; }
  .path { font-family: var(--m-mono); font-size: 12.5px; overflow-wrap: anywhere; }
  .small { font-size: 12.5px; margin: 0; }
  .actions { margin-top: var(--s4); gap: var(--s2); }
  .outcome { display: flex; gap: 8px; align-items: flex-start; margin: var(--s3) 0 0; font-weight: 560; }
  .outcome :global(svg.i) { color: var(--m-ok); }
  .outcome.fault :global(svg.i) { color: var(--m-fault); }
  .outcome.unknown :global(svg.i) { color: var(--m-warn); }
  .danger-text:not(:disabled):hover { color: var(--m-fault); border-color: color-mix(in srgb, var(--m-fault) 50%, transparent); }
</style>
