<script>
  import { tick, untrack } from "svelte";
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import Empty from "../../../shared/ui/Empty.svelte";
  import ConfirmDialog from "../../../shared/ui/ConfirmDialog.svelte";
  import StepList from "./StepList.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { adminSend } from "../state/auth.svelte.js";
  import { backupJobs, contactLost, dismissRestore, followAgain, reloadIfGone, restoreEndedSession, runRestore, startUnanswered } from "../state/backupJobs.svelte.js";
  import { fileActionKey, stepView } from "../lib/backups.js";
  import { errorKey, errorView } from "../lib/errors.js";
  import { href } from "../app/router.svelte.js";

  /**
   * One restore, from preview to result. Nothing is written until the
   * operator confirms a preview the server made for exactly these options;
   * changing an option drops the preview and asks for a new one, so the
   * plan that runs is always the plan on screen. The run itself is followed
   * by state/backupJobs, so it outlives this panel.
   */
  let { target, onclose } = $props();

  const OUTCOME_ICONS = { ok: "circle-check", info: "info", lost: "wifi-off", unknown: "circle-help", warn: "circle-x", fault: "circle-x" };

  let password = $state("");
  let unlocked = $state(false);
  let rollback = $state(true);
  let autoRollback = $state(true);
  let plan = $state.raw(null);
  let previewing = $state(false);
  let error = $state.raw(null);
  let refused = $state.raw(null);
  let confirming = $state(false);
  let region = $state();
  let previewToken = 0;
  const f = $derived(fmt());
  const job = backupJobs.restore;
  const mine = $derived(backupJobs.restoreTarget?.id === target.id);
  const running = $derived(mine && job.running);
  const ran = $derived(mine && (job.running || job.result !== null || job.error !== null));
  const needsPassword = $derived(target.locked && !unlocked && !ran);
  const involvesAnalytics = $derived((plan?.targets || []).some((entry) => entry.backup_type === "influxdb"));
  const steps = $derived(mine ? job.steps.map((step) => stepView(step, backupJobs.restorePlan?.targets || [])) : []);
  const outcome = $derived.by(() => {
    if (!mine || job.running) return null;
    if (job.result) {
      if (job.result.ok) return { tone: "ok", text: t("restore.done") };
      const tone = job.result.status === "rollback_failed" ? "fault" : "warn";
      return job.result.message ? { tone, text: job.result.message, lang: "en" } : { tone, text: t("restore.incomplete") };
    }
    if (job.error) {
      if (restoreEndedSession(job)) return { tone: "info", text: t("restore.sessionEnded") };
      if (contactLost(job)) return { tone: "lost", text: t("jobs.contactLost") };
      if (startUnanswered(job)) return { tone: "unknown", text: t("error.noAnswer") };
      const key = errorKey(job.error);
      return { tone: "fault", text: key && key !== "jobs.unreadable" ? t(key) : t("restore.unreadable") };
    }
    return null;
  });

  $effect(() => {
    target;
    untrack(() => {
      tick().then(() => {
        region?.scrollIntoView({ block: "start" });
        region?.focus({ preventScroll: true });
      });
      refused = null;
      if ((!target.locked || unlocked) && !ran) preview();
    });
  });

  async function preview() {
    const token = ++previewToken;
    plan = null;
    error = null;
    previewing = true;
    try {
      const data = await adminSend("/api/admin/maintenance/backups/restore/preview", {
        body: {
          id: target.id,
          scope: target.type || "config",
          password: target.locked ? password || null : null,
          conflict_policy: "replace",
          rollback,
          auto_rollback: autoRollback,
        },
      });
      if (token === previewToken) plan = data;
    } catch (err) {
      if (token === previewToken) error = errorView(err, t, t("restore.previewFailed"));
      reloadIfGone(err);
    } finally {
      if (token === previewToken) previewing = false;
    }
  }

  function optionsChanged() {
    refused = null;
    if (!needsPassword) preview();
  }

  /** The password is proven against the archive first, as Classic does, so a wrong one is said as such. */
  async function unlock(event) {
    event.preventDefault();
    error = null;
    previewing = true;
    try {
      const details = await adminSend("/api/admin/maintenance/backups/inspect", { body: { id: target.id, password } });
      if (details.locked) {
        error = { text: t("backups.wrongPassword") };
        return;
      }
      unlocked = true;
    } catch (err) {
      error = errorView(err, t, t("backups.inspectFailed"));
      reloadIfGone(err);
      return;
    } finally {
      previewing = false;
    }
    preview();
  }

  /**
   * A start the server refused -- the preview expired or was used, or another
   * job holds the backups -- changed nothing, so the panel goes back to its
   * options with a fresh preview and says why, instead of ending there.
   */
  async function execute() {
    confirming = false;
    refused = null;
    const confirmed = plan;
    plan = null;
    const started = runRestore(target, confirmed);
    await tick();
    region?.focus({ preventScroll: true });
    await started;
    if (backupJobs.restoreTarget?.id !== target.id) {
      plan = confirmed;
      return;
    }
    if (!job.jobId && job.error && !startUnanswered(job)) {
      refused = errorView(job.error, t);
      reloadIfGone(job.error);
      dismissRestore();
      preview();
    }
  }
</script>

{#snippet panelActions()}
  <button class="icon-btn" type="button" onclick={onclose} disabled={running} aria-label={t("common.close")} title={t("common.close")}><Icon name="x" /></button>
{/snippet}

<div class="span-12 restore" tabindex="-1" bind:this={region}>
<Card title={t("restore.title", { name: target.name })} subtitle={t("restore.sub")} actions={panelActions}>
  {#if needsPassword}
    <form class="unlock" onsubmit={unlock} autocomplete="off">
      <p class="muted"><Icon name="lock" size="sm" />{t("restore.lockedLead")}</p>
      <label class="field" for="restore-password"><span>{t("backups.password")}</span>
        <input class="input" id="restore-password" type="password" autocomplete="off" bind:value={password} required />
      </label>
      <button class="btn" type="submit" disabled={previewing || !password}>{t("restore.unlock")}</button>
    </form>
  {/if}

  {#if !needsPassword && !ran}
    <fieldset class="options">
      <legend class="sr-only">{t("restore.options")}</legend>
      <label class="check"><input type="checkbox" bind:checked={rollback} onchange={optionsChanged} data-testid="restore-rollback" /><span><strong>{t("restore.rollback")}</strong><span class="muted">{t("restore.rollbackHint")}</span></span></label>
      <label class="check"><input type="checkbox" bind:checked={autoRollback} onchange={optionsChanged} /><span><strong>{t("restore.autoRollback")}</strong><span class="muted">{t("restore.autoRollbackHint")}</span></span></label>
    </fieldset>
  {/if}

  {#if refused && !ran}<p class="note warn" role="alert" data-testid="restore-refused"><Icon name="triangle-alert" /><span>{t("restore.refused")} <span lang={refused.lang}>{refused.text}</span> {t("restore.refusedNext")}</span></p>{/if}
  {#if previewing && !needsPassword}<Empty text={t("restore.previewing")} icon="loader" />{/if}
  {#if error}<p class="form-error" role="alert" lang={error.lang}>{error.text}</p>{/if}

  {#if plan && !ran}
    <div class="facts" data-testid="restore-summary">
      <div class="fact"><div class="fact-label">{t("restore.willRestore")}</div><div class="fact-value num">{f.number(plan.summary?.would_restore || 0)}</div></div>
      <div class="fact"><div class="fact-label">{t("restore.willReplace")}</div><div class="fact-value num">{f.number(plan.summary?.would_replace || 0)}</div></div>
      <div class="fact"><div class="fact-label">{t("restore.willSkip")}</div><div class="fact-value num">{f.number(plan.summary?.would_skip || 0)}</div></div>
    </div>
    {#if plan.files?.length}
      <ul class="files">
        {#each plan.files as file, index (`${file.archive}-${file.path}-${index}`)}
          <li><span class="path">{file.path}</span><span class="muted">{fileActionKey(file.action) ? t(fileActionKey(file.action)) : file.action}</span></li>
        {/each}
      </ul>
    {/if}
    {#if plan.blocked}{@const blocked = errorView({ error: plan.block_reason }, t, t("restore.blocked"))}<p class="note warn"><Icon name="ban" /><span lang={blocked.lang}>{blocked.text}</span></p>{/if}
    {#if !rollback}<p class="note warn"><Icon name="triangle-alert" />{t("restore.noRollback")}</p>{/if}
    {#each plan.warnings || [] as warning, index (index)}<p class="note warn"><Icon name="triangle-alert" /><span lang="en">{warning}</span></p>{/each}
    <div class="row-wrap actions">
      <button class="btn danger" type="button" disabled={plan.blocked || backupJobs.busy} onclick={() => (confirming = true)} data-testid="restore-execute">
        <Icon name="history" size="sm" />{t("restore.execute")}
      </button>
      <button class="btn secondary" type="button" onclick={onclose}>{t("auth.cancel")}</button>
    </div>
  {/if}

  {#if steps.length}<StepList {steps} label={t("restore.progress")} />{/if}
  <div role="status" aria-live="polite">
    {#if mine && job.lost}<p class="note warn"><Icon name="wifi-off" />{t("jobs.lost")}</p>{/if}
    {#if outcome}<p class="outcome {outcome.tone}" data-testid="restore-outcome"><Icon name={OUTCOME_ICONS[outcome.tone] || "circle-x"} /><span lang={outcome.lang}>{outcome.text}</span></p>{/if}
  </div>
  {#if outcome}
    {#if outcome.tone === "lost"}<button class="btn secondary small" type="button" onclick={() => followAgain(job)}><Icon name="refresh-cw" size="sm" />{t("jobs.followAgain")}</button>{/if}
    {#if outcome.tone === "ok"}<a class="btn" href={href("services")}><Icon name="rotate-cw" size="sm" />{t("restore.restartNow")}</a>{/if}
  {/if}
</Card>
</div>

<ConfirmDialog
  open={confirming}
  title={t("restore.confirmTitle")}
  lines={[involvesAnalytics ? t("restore.confirmAnalytics") : t("restore.confirm"), rollback ? t("restore.confirmRollback") : t("restore.noRollback")]}
  confirmLabel={t("restore.execute")}
  danger
  onconfirm={execute}
  oncancel={() => (confirming = false)}
/>

<style>
  .restore { scroll-margin-top: 96px; border-radius: var(--m-radius-card); }
  .unlock { display: flex; flex-direction: column; gap: var(--s3); max-width: 420px; }
  .unlock p { display: flex; gap: 6px; align-items: center; margin: 0; }
  .options { border: 0; padding: 0; margin: 0 0 var(--s3); display: flex; flex-direction: column; gap: var(--s2); }
  .options .check { align-items: flex-start; }
  .options .check > span { display: flex; flex-direction: column; gap: 2px; }
  .options .check input { margin-top: 2px; }
  .options .muted { font-size: 12.5px; }
  .files { list-style: none; margin: var(--s3) 0 0; padding: 0; max-height: 260px; overflow: auto; }
  .files li { display: flex; justify-content: space-between; gap: var(--s3); padding: 6px 0; border-bottom: 1px solid var(--m-line); font-size: 13px; flex-wrap: wrap; }
  .path { font-family: var(--m-mono); font-size: 12.5px; overflow-wrap: anywhere; }
  .actions { margin-top: var(--s4); gap: var(--s2); }
  .outcome { display: flex; gap: 8px; align-items: flex-start; margin: var(--s3) 0; font-weight: 560; }
  .outcome.ok :global(svg.i) { color: var(--m-ok); }
  .outcome.fault :global(svg.i) { color: var(--m-fault); }
  .outcome.warn :global(svg.i), .outcome.lost :global(svg.i), .outcome.unknown :global(svg.i) { color: var(--m-warn); }
  .outcome.info :global(svg.i) { color: var(--m-accent); }
</style>
