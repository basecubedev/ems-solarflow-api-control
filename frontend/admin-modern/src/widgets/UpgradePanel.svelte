<script>
  import { tick } from "svelte";
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import ConfirmDialog from "../../../shared/ui/ConfirmDialog.svelte";
  import { t, tn, fmt } from "../i18n/i18n.svelte.js";
  import { system } from "../state/system.svelte.js";
  import { upgrade, verify, startUpgrade, closeUpgrade, recover, previewSetupDiscard, discardSetup } from "../state/upgrade.svelte.js";
  import { PHASES, phaseStates, failedStep, backupArchive, rateLimited, replacesAdmin, migrationView, isDevelopment, clock, nothingChanged, phaseOfStage } from "../lib/upgrade.js";
  import { noteView } from "../lib/updates.js";
  import { errorView } from "../lib/errors.js";
  import { dashboardHref } from "../lib/links.js";

  const f = $derived(fmt());
  const tag = $derived(upgrade.target);
  const verification = $derived(upgrade.verification);
  const migration = $derived(migrationView(upgrade.migration?.review));
  const replacing = $derived(upgrade.adminRequired ?? replacesAdmin(verification));
  const catalogued = $derived((system.releases.data?.releases || []).find((release) => release.tag === tag) || null);
  const releaseNote = $derived(noteView(catalogued?.upgrade_warning));
  const progress = $derived(["starting", "replacing", "running"].includes(upgrade.stage));
  const own = $derived(upgrade.transition && upgrade.transition.mode === "guided_upgrade" && upgrade.transition.operation_id === upgrade.operationId ? upgrade.transition : null);
  const phases = $derived(
    phaseStates({
      steps: upgrade.steps,
      stage: own?.stage || (upgrade.stage === "replacing" ? "admin_reconnect_pending" : null),
      adminRequired: replacing,
      backup: upgrade.backupPlanned,
      starting: upgrade.stage === "starting",
      finished: upgrade.stage === "succeeded" ? "succeeded" : null,
    }).filter((phase) => phase.state !== "skipped"),
  );
  const stopped = $derived(failedStep({ steps: upgrade.steps, result: upgrade.result }));
  const stoppedPhase = $derived(stopped ? PHASES.find((phase) => phase.steps.includes(stopped.key))?.id || null : phaseOfStage(own?.failed_stage || own?.resume_stage));
  const unchanged = $derived(nothingChanged({ started: Boolean(upgrade.operationId || upgrade.jobId || upgrade.workDone), steps: upgrade.steps, result: upgrade.result }));
  const archive = $derived(backupArchive(upgrade.result));
  const warnings = $derived((Array.isArray(upgrade.result?.warnings) ? upgrade.result.warnings : []).map(String));
  const dashboard = $derived(dashboardHref(system.overview.data?.links?.dashboard_url));
  const failure = $derived(upgrade.error ? errorView(upgrade.error, t) : null);
  const reason = $derived(upgrade.result?.message || stopped?.detail || own?.error_message || "");
  const closable = $derived(!upgrade.busy && !upgrade.holding && upgrade.stage !== "succeeded");

  let now = $state(Date.now());
  $effect(() => {
    if (!progress) return;
    const timer = setInterval(() => (now = Date.now()), 1000);
    return () => clearInterval(timer);
  });

  /**
   * Each new screen of the flow is shown from its top and takes the focus, so
   * the view and a screen reader both land on it: the button that led here sat
   * further down, or was removed with the screen it belonged to. Moving between
   * the running screens keeps the reader where they are.
   */
  let region = $state();
  let screen = "";
  $effect(() => {
    const next = progress ? "progress" : upgrade.stage;
    if (next === screen) return;
    screen = next;
    globalThis.scrollTo?.(0, 0);
    tick().then(() => region?.focus({ preventScroll: true }));
  });

  let confirming = $state(false);
  let confirmingRecovery = $state("");
  let discardPlan = $state.raw(null);
  let discardError = $state.raw(null);

  async function askDiscard() {
    discardError = null;
    try {
      const plan = await previewSetupDiscard();
      if (plan?.blocked || !plan?.fingerprint) {
        discardError = plan;
        return;
      }
      discardPlan = plan;
    } catch (error) {
      discardError = error;
    }
  }

  async function discard() {
    const plan = discardPlan;
    discardPlan = null;
    if (await discardSetup(plan)) await askDiscard();
  }

  function start() {
    confirming = false;
    startUpgrade();
  }

  function runRecovery(kind) {
    confirmingRecovery = "";
    recover(kind);
  }

  const PHASE_ICONS = { done: "circle-check", running: "loader", pending: "circle", failed: "circle-x" };
  const RECOVERY_LINES = {
    retry: ["upgrade.recover.retryConfirm"],
    return: ["upgrade.recover.returnConfirm"],
    cancel: ["upgrade.recover.cancelConfirm"],
  };
</script>

<Card title={t("upgrade.title", { tag: tag || "" })} subtitle={t(`upgrade.sub.${upgrade.stage}`)} span="span-12">
  {#snippet actions()}
    {#if closable}
      <button class="icon-btn" type="button" onclick={closeUpgrade} aria-label={t("upgrade.close")} title={t("upgrade.close")} data-testid="upgrade-close"><Icon name="x" /></button>
    {/if}
  {/snippet}
  <div class="upgrade" data-testid="upgrade" data-stage={upgrade.stage} tabindex="-1" bind:this={region} aria-label={t("upgrade.title", { tag: tag || "" })}>
    <p class="sr-only" role="status" aria-live="polite">{t(`upgrade.sub.${upgrade.stage}`)}</p>
    {#if upgrade.stage === "verifying"}
      <p class="lead"><Icon name="loader" />{t("upgrade.verifying")}</p>
    {:else if upgrade.stage === "verifyFailed"}
      <p class="note warn" role="alert"><Icon name="triangle-alert" /><span lang={failure?.lang}>{failure?.text || t("upgrade.verifyFailed")}</span></p>
      <div class="row-wrap actions">
        <button class="btn" type="button" onclick={verify} data-testid="upgrade-verify-again"><Icon name="rotate-cw" size="sm" />{t("upgrade.verifyAgain")}</button>
        <button class="btn secondary" type="button" onclick={closeUpgrade}>{t("upgrade.chooseOther")}</button>
      </div>
    {:else if upgrade.stage === "blocked"}
      {#if upgrade.conflict?.error === "setup_cleanup_required"}
        <p class="note warn" role="alert"><Icon name="triangle-alert" />{t("upgrade.blocked.cleanup")}</p>
        <div class="row-wrap actions"><a class="btn" href="#/system">{t("upgrade.blocked.openRecovery")}</a></div>
      {:else}
        <p class="note warn" role="alert"><Icon name="triangle-alert" />{t("upgrade.blocked.setup")}</p>
        {#if discardError || upgrade.error}
          {@const shown = discardError?.blocked ? null : errorView(discardError || upgrade.error, t)}
          <p class="note fault" role="alert"><Icon name="circle-x" /><span lang={shown?.lang}>{shown ? shown.text : t("upgrade.blocked.cannotDiscard")}</span></p>
        {/if}
        <div class="row-wrap actions">
          <button class="btn" type="button" onclick={askDiscard} disabled={upgrade.busy} data-testid="upgrade-discard-setup"><Icon name={upgrade.recovering === "discard" ? "loader" : "trash-2"} size="sm" />{t("upgrade.blocked.discard")}</button>
          <a class="btn secondary" href="#/system">{t("upgrade.blocked.openRecovery")}</a>
        </div>
      {/if}
    {:else if upgrade.stage === "ready"}
      <div class="facts two">
        <div class="fact"><div class="fact-label">{t("upgrade.now")}</div><div class="fact-value wrap num">{system.overview.data?.containers?.ems?.tag || verification?.current_admin?.system_tag || t("version.unknown")}</div></div>
        <div class="fact"><div class="fact-label">{t("upgrade.after")}</div><div class="fact-value wrap num">{tag}</div></div>
      </div>
      {#if isDevelopment(upgrade.release)}<p class="note warn"><Icon name="triangle-alert" />{t("upgrade.development")}</p>{/if}
      {#if releaseNote}<p class="note"><Icon name="info" />{#if releaseNote.key}{t(releaseNote.key)}{:else}<span lang="en">{releaseNote.text}</span>{/if}</p>{/if}
      {#if migration.required}<p class="note warn" data-testid="upgrade-migration"><Icon name="triangle-alert" /><span>{tn("upgrade.migration", migration.devices)}{#if migration.losing}{" "}{tn("upgrade.migrationLoses", migration.losing)}{/if}</span></p>{/if}
      <fieldset class="choices">
        <legend class="sub">{t("upgrade.choices")}</legend>
        {#each ["backup", "settings", "diagnostics"] as choice (choice)}
          <label class="choice inner">
            <input type="checkbox" bind:checked={upgrade.choices[choice]} data-testid={`upgrade-choice-${choice}`} />
            <span><span class="choice-title">{t(`upgrade.choice.${choice}`)}</span><span class="muted small">{t(`upgrade.choice.${choice}.desc`)}</span></span>
          </label>
        {/each}
      </fieldset>
      <h3 class="sub">{t("upgrade.whatHappens")}</h3>
      <ol class="plan">
        <li>{t("upgrade.plan.check")}</li>
        {#if upgrade.choices.backup}<li>{t("upgrade.plan.backup")}</li>{/if}
        {#if migration.required}<li>{t("upgrade.plan.migration")}</li>{/if}
        {#if replacing}<li>{t("upgrade.plan.admin")}</li>{/if}
        <li>{t("upgrade.plan.install", { tag })}</li>
        {#if upgrade.choices.diagnostics}<li>{t("upgrade.plan.verify")}</li>{/if}
      </ol>
      {#if !upgrade.choices.backup}<p class="note warn"><Icon name="triangle-alert" />{t("upgrade.noBackup")}</p>{/if}
      <div class="row-wrap actions">
        <button class="btn" type="button" onclick={() => (confirming = true)} data-testid="upgrade-start"><Icon name="circle-arrow-up" size="sm" />{t("upgrade.start", { tag })}</button>
        <button class="btn secondary" type="button" onclick={closeUpgrade}>{t("upgrade.chooseOther")}</button>
      </div>
    {:else if upgrade.stage === "checking"}
      <p class="lead"><Icon name="loader" />{t("upgrade.checking")}</p>
    {:else if progress}
      <ol class="phases" aria-label={t("upgrade.progress")} data-testid="upgrade-phases">
        {#each phases as phase (phase.id)}
          <li class="phase" data-phase={phase.id} data-state={phase.state}>
            <Icon name={PHASE_ICONS[phase.state] || "circle"} />
            <span class="what">
              <span class="phase-title"><span class="sr-only">{t(`step.state.${phase.state}`)} </span>{t(`upgrade.phase.${phase.id}`, { tag })}</span>
              {#if phase.state === "running"}<span class="muted small">{t(`upgrade.phase.${phase.id}.doing`)}</span>{/if}
            </span>
          </li>
        {/each}
      </ol>
      <p class="muted small num clock" data-testid="upgrade-clock">{t("upgrade.runningFor", { time: clock(now - upgrade.since) })}</p>
      {#if upgrade.lost}<p class="note warn"><Icon name="wifi-off" />{t("upgrade.lost")}</p>{/if}
      {#if upgrade.stage === "replacing" && upgrade.slow}
        <p class="note warn" data-testid="upgrade-slow"><Icon name="clock" />{t("upgrade.slow")}</p>
        <div class="row-wrap actions"><button class="btn secondary" type="button" onclick={() => globalThis.location.reload()}><Icon name="refresh-cw" size="sm" />{t("upgrade.reload")}</button></div>
      {/if}
      <p class="note"><Icon name="info" />{t("upgrade.keepOn")}</p>
    {:else if upgrade.stage === "succeeded"}
      <div class="outcome ok" data-testid="upgrade-done">
        <span class="icon-tile c-ok"><Icon name="circle-check" /></span>
        <div>
          <div class="outcome-title">{t("upgrade.done")}</div>
          <div class="muted">{replacing ? t("upgrade.doneBoth", { tag }) : t("upgrade.doneEms", { tag })}</div>
        </div>
      </div>
      <div class="facts two">
        {#if upgrade.before?.ems}<div class="fact"><div class="fact-label">{t("upgrade.before")}</div><div class="fact-value wrap num">{upgrade.before.ems}</div></div>{/if}
        <div class="fact"><div class="fact-label">{t("upgrade.nowRuns")}</div><div class="fact-value wrap num">{tag}</div></div>
        {#if upgrade.finishedAt}<div class="fact"><div class="fact-label">{t("upgrade.finishedAt")}</div><div class="fact-value num">{f.when(upgrade.finishedAt)}</div></div>{/if}
        {#if upgrade.since && upgrade.finishedAt}<div class="fact"><div class="fact-label">{t("upgrade.took")}</div><div class="fact-value num">{clock(upgrade.finishedAt - upgrade.since)}</div></div>{/if}
        {#if archive}<div class="fact"><div class="fact-label">{t("upgrade.backupMade")}</div><div class="fact-value wrap small-value">{archive}</div></div>{/if}
      </div>
      {#each warnings as warning, index (index)}<p class="note warn"><Icon name="triangle-alert" /><span lang="en">{warning}</span></p>{/each}
      <div class="row-wrap actions">
        {#if dashboard}<a class="btn" href={dashboard} data-testid="upgrade-open-dashboard"><Icon name="layout-dashboard" size="sm" />{t("upgrade.openDashboard")}</a>{/if}
        <button class="btn secondary" type="button" onclick={closeUpgrade} data-testid="upgrade-finish">{t("upgrade.finish")}</button>
      </div>
    {:else if upgrade.stage === "returned"}
      <div class="outcome" data-testid="upgrade-returned">
        <span class="icon-tile c-acc"><Icon name="history" /></span>
        <div>
          <div class="outcome-title">{t("upgrade.returned")}</div>
          <div class="muted">{upgrade.returnAligns ? t("upgrade.returnedAlign") : t("upgrade.returnedRestart")}</div>
        </div>
      </div>
      {#if upgrade.slow}<p class="note warn"><Icon name="clock" />{t("upgrade.slow")}</p>{/if}
      <div class="row-wrap actions">
        <button class="btn secondary" type="button" onclick={closeUpgrade}>{t("upgrade.finish")}</button>
      </div>
    {:else}
      <div class="outcome fault" data-testid="upgrade-failed">
        <span class="icon-tile c-fault"><Icon name={upgrade.stage === "cancelled" ? "ban" : "circle-x"} /></span>
        <div>
          <div class="outcome-title">{t(`upgrade.end.${upgrade.stage}`)}</div>
          {#if upgrade.stage === "failed" && stoppedPhase}<div class="muted">{t("upgrade.stoppedAt", { phase: t(`upgrade.phase.${stoppedPhase}`, { tag }) })}</div>{/if}
        </div>
      </div>
      {#if upgrade.stage === "failed" && upgrade.unknownStart}
        <p class="note warn"><Icon name="circle-help" />{t("upgrade.unknownStart")}</p>
      {:else if upgrade.stage !== "cancelled"}
        {#if rateLimited(upgrade.result)}<p class="note warn"><Icon name="clock" />{t("upgrade.rateLimited")}</p>{/if}
        {#if failure}
          <p class="note fault"><Icon name="circle-x" /><span lang={failure.lang}>{failure.text}</span></p>
        {:else if reason}
          <p class="note fault"><Icon name="circle-x" /><span lang="en">{reason}</span></p>
        {/if}
        <p class="muted">{upgrade.stage === "refused" || unchanged ? t("upgrade.nothingChanged") : t("upgrade.mayHaveChanged")}</p>
        {#if own?.worker_active === true}<p class="note"><Icon name="loader" />{t("upgrade.stillWorking")}</p>{/if}
      {/if}
      <div class="row-wrap actions">
        {#if upgrade.stage === "failed" && own?.resume_available}<button class="btn" type="button" disabled={upgrade.busy} onclick={() => (confirmingRecovery = "retry")} data-testid="upgrade-retry"><Icon name={upgrade.recovering === "retry" ? "loader" : "rotate-cw"} size="sm" />{t("upgrade.recover.retry")}</button>{/if}
        {#if upgrade.stage === "failed" && own?.return_available}<button class="btn secondary" type="button" disabled={upgrade.busy} onclick={() => (confirmingRecovery = "return")} data-testid="upgrade-return"><Icon name={upgrade.recovering === "return" ? "loader" : "history"} size="sm" />{t("upgrade.recover.return")}</button>{/if}
        {#if upgrade.stage === "failed" && own?.cancel_available}<button class="btn secondary" type="button" disabled={upgrade.busy} onclick={() => (confirmingRecovery = "cancel")} data-testid="upgrade-cancel"><Icon name={upgrade.recovering === "cancel" ? "loader" : "ban"} size="sm" />{t("upgrade.recover.cancel")}</button>{/if}
        {#if !upgrade.holding}<button class="btn secondary" type="button" disabled={upgrade.busy} onclick={closeUpgrade} data-testid="upgrade-choose-again">{t("upgrade.chooseAgain")}</button>{/if}
        <a class="btn secondary" href="#/backups"><Icon name="archive" size="sm" />{t("upgrade.openBackups")}</a>
      </div>
    {/if}
  </div>
</Card>

<ConfirmDialog
  open={confirming}
  title={t("upgrade.confirmTitle", { tag: tag || "" })}
  lines={[t("upgrade.confirmPause"), ...(replacing ? [t("upgrade.confirmReconnect")] : []), t("upgrade.confirmKeepOn"), ...(upgrade.choices.backup ? [] : [t("upgrade.noBackup")])]}
  confirmLabel={t("upgrade.start", { tag: tag || "" })}
  onconfirm={start}
  oncancel={() => (confirming = false)}
/>
<ConfirmDialog
  open={Boolean(confirmingRecovery)}
  title={confirmingRecovery ? t(`upgrade.recover.${confirmingRecovery}`) : ""}
  lines={(RECOVERY_LINES[confirmingRecovery] || []).map((key) => t(key))}
  confirmLabel={confirmingRecovery ? t(`upgrade.recover.${confirmingRecovery}`) : ""}
  danger={confirmingRecovery === "cancel"}
  onconfirm={() => runRecovery(confirmingRecovery)}
  oncancel={() => (confirmingRecovery = "")}
/>
<ConfirmDialog
  open={upgrade.acknowledgeReturn}
  title={t("upgrade.recover.return")}
  lines={[t("upgrade.recover.returnDevelopment")]}
  confirmLabel={t("upgrade.recover.return")}
  onconfirm={() => recover("return")}
  oncancel={() => (upgrade.acknowledgeReturn = false)}
/>
<ConfirmDialog
  open={Boolean(discardPlan)}
  title={t("upgrade.blocked.discardTitle")}
  lines={[t("upgrade.blocked.discardReset"), ...(discardPlan?.will_reset || []).map((text) => ({ text: String(text), lang: "en" })), t("upgrade.blocked.discardKeep"), ...(discardPlan?.will_preserve || []).map((text) => ({ text: String(text), lang: "en" }))]}
  confirmLabel={t("upgrade.blocked.discard")}
  danger
  onconfirm={discard}
  oncancel={() => (discardPlan = null)}
/>

<style>
  .upgrade { display: flex; flex-direction: column; gap: var(--s3); }
  .upgrade:focus { outline: none; }
  .lead { display: flex; gap: 8px; align-items: flex-start; margin: 0; }
  .lead :global(svg.i) { margin-top: 2px; flex: none; }
  .actions { gap: var(--s2); }
  .upgrade .btn { white-space: normal; height: auto; min-height: 36px; overflow-wrap: anywhere; text-align: left; }
  .choices { border: 0; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--s2); }
  .choice { display: flex; gap: var(--s3); align-items: flex-start; cursor: pointer; }
  .choice input { margin-top: 3px; flex: none; }
  .choice > span { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .choice-title { font-weight: 600; }
  .sub { font-size: 13px; font-weight: 620; margin: var(--s2) 0 var(--s1); padding: 0; }
  .small { font-size: 12.5px; }
  .plan { margin: 0; padding-left: 20px; display: flex; flex-direction: column; gap: 4px; font-size: 13.5px; }
  .phases { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--s2); }
  .phase { display: grid; grid-template-columns: 20px minmax(0, 1fr); gap: 10px; align-items: start; }
  .phase :global(svg.i) { margin-top: 2px; color: var(--m-muted-2); }
  .phase[data-state="done"] :global(svg.i) { color: var(--m-ok); }
  .phase[data-state="running"] :global(svg.i) { color: var(--m-accent); }
  .phase[data-state="failed"] :global(svg.i) { color: var(--m-fault); }
  .phase[data-state="pending"] .phase-title { color: var(--m-muted); }
  .what { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .phase-title { font-weight: 600; }
  .clock { margin: 0; }
  .outcome { display: flex; gap: var(--s3); align-items: center; }
  .outcome-title { font-size: 17px; font-weight: 650; }
  .small-value { font-size: 13px; }
</style>
