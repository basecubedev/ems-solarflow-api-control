<script>
  import { tick } from "svelte";
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import ConfirmDialog from "../../../shared/ui/ConfirmDialog.svelte";
  import { t, tn, fmt } from "../i18n/i18n.svelte.js";
  import { system } from "../state/system.svelte.js";
  import { upgrade, verify, startUpgrade, closeUpgrade, recover, previewSetupDiscard, discardSetup } from "../state/upgrade.svelte.js";
  import { PHASES, PHASE_TONES, STAGE_TONES, WORKING_STAGES, phaseStates, phaseProgress, failedStep, backupArchive, rateLimited, replacesAdmin, migrationView, isDevelopment, clock, nothingChanged, phaseOfStage } from "../lib/upgrade.js";
  import { noteView } from "../lib/updates.js";
  import { errorView } from "../lib/errors.js";
  import { dashboardHref } from "../lib/links.js";
  import Spinner from "./Spinner.svelte";

  const f = $derived(fmt());
  const tag = $derived(upgrade.target);
  const verification = $derived(upgrade.verification);
  const migration = $derived(migrationView(upgrade.migration?.review));
  const replacing = $derived(upgrade.adminRequired ?? replacesAdmin(verification));
  const catalogued = $derived((system.releases.data?.releases || []).find((release) => release.tag === tag) || null);
  const releaseNote = $derived(noteView(catalogued?.upgrade_warning));
  const progress = $derived(["starting", "replacing", "running"].includes(upgrade.stage));
  const working = $derived(WORKING_STAGES.includes(upgrade.stage));
  const tone = $derived(STAGE_TONES[upgrade.stage] || "c-out");
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
  const step = $derived(phaseProgress(phases));
  const stopped = $derived(failedStep({ steps: upgrade.steps, result: upgrade.result }));
  const stoppedPhase = $derived(stopped ? PHASES.find((phase) => phase.steps.includes(stopped.key))?.id || null : phaseOfStage(own?.failed_stage || own?.resume_stage));
  const unchanged = $derived(nothingChanged({ started: Boolean(upgrade.operationId || upgrade.jobId || upgrade.workDone), steps: upgrade.steps, result: upgrade.result }));
  const archive = $derived(backupArchive(upgrade.result));
  const warnings = $derived((Array.isArray(upgrade.result?.warnings) ? upgrade.result.warnings : []).map(String));
  const dashboard = $derived(dashboardHref(system.overview.data?.links?.dashboard_url));
  const failure = $derived(upgrade.error ? errorView(upgrade.error, t) : null);
  const reason = $derived(upgrade.result?.message || stopped?.detail || own?.error_message || "");
  const closable = $derived(!upgrade.busy && !upgrade.holding && upgrade.stage !== "succeeded");

  const uid = $props.id();
  let now = $state(Date.now());
  const since = $derived(upgrade.stage === "verifying" ? upgrade.verifyingSince : upgrade.since);
  $effect(() => {
    if (!working) return;
    now = Date.now();
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
    const first = screen === "";
    screen = next;
    tick().then(() => {
      showTop(first);
      region?.focus({ preventScroll: true });
    });
  });

  /**
   * The panel's top, below the sticky top bar, when it lies above the window
   * or, after its first screen, below the window's fold. The first screen
   * leaves a panel that already shows where it is, so opening the page during
   * a run keeps What runs now in view.
   */
  function showTop(first) {
    const card = region?.closest(".card");
    if (!card) return;
    const bar = globalThis.document?.querySelector(".topbar")?.getBoundingClientRect().bottom || 0;
    const top = card.getBoundingClientRect().top;
    if (top >= bar && (first || top <= globalThis.innerHeight - FOLD)) return;
    globalThis.scrollTo?.(0, globalThis.scrollY + top - bar - 12);
  }

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

  const FOLD = 160;
  const PHASE_ICONS = { done: "circle-check", pending: "circle", failed: "circle-x" };
  const RECOVERY_LINES = {
    retry: ["upgrade.recover.retryConfirm"],
    return: ["upgrade.recover.returnConfirm"],
    cancel: ["upgrade.recover.cancelConfirm"],
  };
</script>

{#snippet banner()}
  <div class="working {tone}" data-testid="upgrade-working">
    <span class="icon-tile sm"><Spinner /></span>
    <div class="working-text">
      {#if upgrade.stage === "verifying"}
        <div class="working-title" id={`${uid}-working`}>{t("upgrade.busy.verifying", { tag })}</div>
        <div class="muted small">{t("upgrade.verifying")}</div>
      {:else if upgrade.stage === "checking"}
        <div class="working-title" id={`${uid}-working`}>{t("upgrade.sub.checking")}</div>
        <div class="muted small">{t("upgrade.checking")}</div>
      {:else}
        <div class="working-title" id={`${uid}-working`}>{t(`upgrade.sub.${upgrade.stage}`)}</div>
        {#if step.current}<div class="muted small">{t("upgrade.phaseOf", { step: step.step, total: step.total })}{" "}<span aria-hidden="true">·</span>{" "}{t(`upgrade.phase.${step.current}`, { tag })}</div>{/if}
      {/if}
    </div>
    {#if since}<span class="muted small num clock" data-testid="upgrade-clock">{t("upgrade.runningFor", { time: clock(now - since) })}</span>{/if}
    {#if progress}
      <div class="track" role="progressbar" aria-labelledby={`${uid}-working`} aria-valuemin="0" aria-valuemax={step.total} aria-valuenow={Math.floor(step.share * step.total)} aria-valuetext={step.current ? t("upgrade.phaseOf", { step: step.step, total: step.total }) : undefined}><span style:transform={`scaleX(${Math.max(0.04, step.share)})`}></span></div>
    {:else}
      <div class="track sweep" aria-hidden="true"><span></span></div>
    {/if}
  </div>
{/snippet}

<Card icon="circle-arrow-up" {tone} title={t("upgrade.title", { tag: tag || "" })} subtitle={working ? "" : t(`upgrade.sub.${upgrade.stage}`)} span="span-12">
  {#snippet actions()}
    {#if closable}
      <button class="icon-btn" type="button" onclick={closeUpgrade} aria-label={t("upgrade.close")} title={t("upgrade.close")} data-testid="upgrade-close"><Icon name="x" /></button>
    {/if}
  {/snippet}
  <div class="upgrade" data-testid="upgrade" data-stage={upgrade.stage} tabindex="-1" bind:this={region} aria-label={t("upgrade.title", { tag: tag || "" })}>
    <p class="sr-only" role="status" aria-live="polite">{t(`upgrade.sub.${upgrade.stage}`)}</p>
    {#if upgrade.stage === "verifying" || upgrade.stage === "checking"}
      {@render banner()}
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
          <button class="btn" type="button" onclick={askDiscard} disabled={upgrade.busy} data-testid="upgrade-discard-setup">{#if upgrade.recovering === "discard"}<Spinner size="sm" />{:else}<Icon name="trash-2" size="sm" />{/if}{t("upgrade.blocked.discard")}</button>
          <a class="btn secondary" href="#/system">{t("upgrade.blocked.openRecovery")}</a>
        </div>
      {/if}
    {:else if upgrade.stage === "ready"}
      <div class="ready-banner c-ok" data-testid="upgrade-ready">
        <span class="icon-tile sm c-ok"><Icon name="circle-check" /></span>
        <div class="working-text">
          <div class="working-title">{t("upgrade.readyTitle", { tag })}</div>
          <div class="muted small">{t("upgrade.readyText")}</div>
        </div>
      </div>
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
        <button class="btn go" type="button" onclick={() => (confirming = true)} data-testid="upgrade-start"><Icon name="circle-arrow-up" />{t("upgrade.start", { tag })}</button>
        <button class="btn secondary" type="button" onclick={closeUpgrade}>{t("upgrade.chooseOther")}</button>
      </div>
    {:else if progress}
      {@render banner()}
      <ol class="phases" aria-label={t("upgrade.progress")} data-testid="upgrade-phases">
        {#each phases as phase (phase.id)}
          <li class="phase {PHASE_TONES[phase.state] || 'c-idle'}" data-phase={phase.id} data-state={phase.state}>
            {#if phase.state === "running"}<Spinner />{:else}<Icon name={PHASE_ICONS[phase.state] || "circle"} />{/if}
            <span class="what">
              <span class="phase-title"><span class="sr-only">{t(`step.state.${phase.state}`)}{" "}</span>{t(`upgrade.phase.${phase.id}`, { tag })}</span>
              {#if phase.state === "running"}<span class="muted small">{t(`upgrade.phase.${phase.id}.doing`)}</span>{/if}
            </span>
          </li>
        {/each}
      </ol>
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
        <span class="icon-tile c-soc"><Icon name="history" /></span>
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
        {#if own?.worker_active === true}<p class="note"><Spinner />{t("upgrade.stillWorking")}</p>{/if}
      {/if}
      <div class="row-wrap actions">
        {#if upgrade.stage === "failed" && own?.resume_available}<button class="btn" type="button" disabled={upgrade.busy} onclick={() => (confirmingRecovery = "retry")} data-testid="upgrade-retry">{#if upgrade.recovering === "retry"}<Spinner size="sm" />{:else}<Icon name="rotate-cw" size="sm" />{/if}{t("upgrade.recover.retry")}</button>{/if}
        {#if upgrade.stage === "failed" && own?.return_available}<button class="btn secondary" type="button" disabled={upgrade.busy} onclick={() => (confirmingRecovery = "return")} data-testid="upgrade-return">{#if upgrade.recovering === "return"}<Spinner size="sm" />{:else}<Icon name="history" size="sm" />{/if}{t("upgrade.recover.return")}</button>{/if}
        {#if upgrade.stage === "failed" && own?.cancel_available}<button class="btn secondary" type="button" disabled={upgrade.busy} onclick={() => (confirmingRecovery = "cancel")} data-testid="upgrade-cancel">{#if upgrade.recovering === "cancel"}<Spinner size="sm" />{:else}<Icon name="ban" size="sm" />{/if}{t("upgrade.recover.cancel")}</button>{/if}
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
  .working, .ready-banner {
    position: relative; overflow: hidden; display: flex; gap: var(--s3); align-items: flex-start; flex-wrap: wrap;
    padding: var(--s3) var(--s4); border-radius: var(--m-radius-inner);
    background: color-mix(in srgb, var(--c) 9%, var(--m-surface-2));
    border: 1px solid color-mix(in srgb, var(--c) 35%, transparent);
  }
  .working { padding-bottom: calc(var(--s3) + 6px); }
  .ready-banner { align-items: center; }
  .working-text { flex: 1 1 160px; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
  .working-title { font-size: 14.5px; font-weight: 640; }
  .working .clock { margin: 0; align-self: center; }
  .track { position: absolute; left: 0; right: 0; bottom: 0; height: 4px; background: color-mix(in srgb, var(--c) 14%, transparent); }
  .track > span { display: block; height: 100%; transform-origin: left center; background: linear-gradient(90deg, color-mix(in srgb, var(--c) 55%, transparent), var(--c)); transition: transform 0.6s ease; }
  .track.sweep > span { width: 35%; animation: sweep 1.8s ease-in-out infinite; }
  @keyframes sweep { from { transform: translateX(-100%); } to { transform: translateX(290%); } }
  :global(:root[data-motion="off"]) .track > span { transition: none; }
  :global(:root[data-motion="off"]) .track.sweep > span { animation: none; width: 100%; opacity: 0.5; }
  .upgrade .btn.go {
    height: auto; min-height: 44px; padding: 10px 22px; font-size: 14.5px; background: var(--m-ok); color: var(--m-black);
    box-shadow: 0 6px 22px color-mix(in srgb, var(--m-ok) 32%, transparent);
    animation: beckon 2.4s ease-out 3;
  }
  @keyframes beckon {
    0% { box-shadow: 0 6px 22px color-mix(in srgb, var(--m-ok) 32%, transparent), 0 0 0 0 color-mix(in srgb, var(--m-ok) 45%, transparent); }
    70%, 100% { box-shadow: 0 6px 22px color-mix(in srgb, var(--m-ok) 32%, transparent), 0 0 0 12px transparent; }
  }
  :global(:root[data-motion="off"]) .upgrade .btn.go { animation: none; }
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
  .phases { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
  .phase { display: grid; grid-template-columns: 20px minmax(0, 1fr); gap: 10px; align-items: start; padding: 8px 10px; border-radius: var(--m-radius-inner); border: 1px solid transparent; }
  .phase :global(svg.i) { margin-top: 2px; color: var(--c); }
  .phase:is([data-state="running"], [data-state="failed"]) { background: color-mix(in srgb, var(--c) 9%, var(--m-surface-2)); border-color: color-mix(in srgb, var(--c) 35%, transparent); }
  .phase[data-state="pending"] .phase-title { color: var(--m-muted); }
  .what { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .phase-title { font-weight: 600; }
  .outcome { display: flex; gap: var(--s3); align-items: center; }
  .outcome-title { font-size: 17px; font-weight: 650; }
  .small-value { font-size: 13px; }
</style>
