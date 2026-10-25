<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import Empty from "../../../shared/ui/Empty.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import ConfirmDialog from "../../../shared/ui/ConfirmDialog.svelte";
  import { untrack } from "svelte";
  import { t, tn, fmt } from "../i18n/i18n.svelte.js";
  import { system, ensure, reload } from "../state/system.svelte.js";
  import SetupDevices from "../widgets/setup/SetupDevices.svelte";
  import SetupConfig from "../widgets/setup/SetupConfig.svelte";
  import SetupDeployment from "../widgets/setup/SetupDeployment.svelte";
  import SetupStart from "../widgets/setup/SetupStart.svelte";
  import { setup, goTo, locked, restoreWorkflow, startSetup, switchPlan, loadReleases, choose, leftAction, rightAction, resumeFromTransition, restart, resumeTransition, discardSetup } from "../state/setup.svelte.js";
  import { setupDiscovery, clearDiscovery, loadBrokers, watchMdns } from "../state/setupDiscovery.svelte.js";
  import { setupDraft, discoveryChanged, loadCatalog, rejoinCurrent, resetDraft, schedulePreview, syncFromDiscovery, writeConfig } from "../state/setupDraft.svelte.js";
  import { setupDeploy, configWritten, loadConfigStatus, loadPlan, resetDeploy } from "../state/setupDeploy.svelte.js";
  import { SETUP_STEPS, SETUP_STEP_LOOKS, PIPELINE, buildActions, buildMessage, channelGroup, groupReleases, isDevelopmentTag, pipelineStates, setupRecovery, setupTransitionActive } from "../lib/setup.js";
  import { BUSY_OPERATIONS } from "../lib/setupDraft.js";
  import { errorView } from "../lib/errors.js";
  import { WORKFLOW_BLOCKED_CODES } from "../lib/workflow.js";

  ensure("lifecycle", "alignment");

  const SWITCH_OWNERS = ["guided_upgrade", "align_existing_install", "unknown"];

  let confirmRestart = $state(false);
  let confirmDiscard = $state(false);
  let switching = $state.raw(null);
  let opened = false;

  const f = $derived(fmt());
  const lifecycle = $derived(system.lifecycle.data);
  const transition = $derived(system.alignment.data?.transition || null);
  const grouped = $derived(groupReleases(setup.releases));
  const selected = $derived(setup.releases.find((release) => release.tag === setup.build.tag) || null);
  const choices = $derived(buildActions(setup.build, setup.releases, setup.intentNeeded));
  const message = $derived(buildMessage(setup.build));
  const pipeline = $derived(pipelineStates(setup.build.result || system.alignment.data));
  const CHECKS = ["admin_image_available", "ems_image_available", "revision_matches", "build_id_matches", "channel_matches", "embedded_resources_match"];
  const checks = $derived(setup.build.result?.checks || null);
  const stepIndex = $derived(SETUP_STEPS.indexOf(setup.step));
  const active = $derived(Boolean(setup.workflowId) || setupTransitionActive(transition));
  const recovery = $derived(setupRecovery(transition, system.alignment.data));
  const next = $derived(SETUP_STEPS[stepIndex + 1] || null);
  const canAdvance = $derived(setup.step === "config" ? Boolean(setupDraft.preview?.ready) && !setupDraft.busy : setup.step === "deployment" ? setupDeploy.ready : Boolean(next) && !locked(next));
  const verdict = $derived(t("setup.verdict", { n: String(stepIndex + 1).padStart(2, "0"), total: String(SETUP_STEPS.length).padStart(2, "0"), title: t(`setup.title.${setup.step}`) }) + (stepStatus(setup.step) ? ` · ${stepStatus(setup.step)}` : ""));

  $effect(() => {
    if (opened) return;
    opened = true;
    open();
  });

  $effect(() => {
    if (!active) return undefined;
    untrack(loadBrokers);
    return untrack(watchMdns);
  });

  $effect(() => {
    void setupDiscovery.devices;
    void setupDiscovery.ignored;
    void setupDiscovery.proposals;
    if (setup.confirmed && setupDraft.catalog) untrack(discoveryChanged);
  });

  /** Opens the setup as the server has it: resumed from its transition, restored from its workflow, or at the start. */
  async function open() {
    loadCatalog();
    await restoreWorkflow();
    await Promise.all([loadConfigStatus(), loadPlan()]);
    const status = await system.alignment.load();
    if (await resumeFromTransition(status?.transition)) return;
    if (setup.workflowId) await loadReleases();
  }

  /** What a step's chip says; a locked step says nothing. */
  function stepStatus(step) {
    if (locked(step)) return "";
    if (step === "release") return t(`setup.chip.release.${setup.confirmed ? "ready" : ["failed", "invalid"].includes(setup.build.state) ? "failed" : ["validating", "confirming", "updating", "reconnecting"].includes(setup.build.state) ? "working" : setup.releasesLoading ? "loading" : "notStarted"}`);
    if (step === "devices") return setupDiscovery.running || setupDiscovery.scanning ? t("setup.chip.devices.discovering") : setupDraft.candidates.length ? tn("setup.chip.devices.count", setupDraft.candidates.length) : t("setup.chip.devices.none");
    if (step === "config") return t(`setup.chip.config.${setupDraft.status}`);
    if (step === "deployment") return setup.generatedReady ? t("setup.chip.deployment.ready") : t("setup.chip.deployment.pending");
    const run = setupDeploy.start;
    return t(`setup.chip.start.${run.status === "running" ? "starting" : run.status === "failed" ? "failed" : run.running ? "running" : "ready"}`);
  }

  /** Opens a step and runs what it needs first: the plan for the settings. */
  function openStep(step) {
    goTo(step);
    if (setup.step === "config") syncFromDiscovery();
  }

  async function advance() {
    if (setup.step !== "config") return openStep(next);
    const data = await writeConfig();
    if (!data) return;
    configWritten(data);
    openStep("deployment");
  }

  function cleared() {
    resetDraft();
    clearDiscovery();
    resetDeploy();
  }

  async function begin() {
    const owner = lifecycle?.owner;
    if (SWITCH_OWNERS.includes(owner)) {
      const plan = await switchPlan();
      if (plan?.ok !== true || plan.blocked) {
        const blocked = plan?.blocked === true || WORKFLOW_BLOCKED_CODES.includes(plan?.error);
        setup.startError = blocked ? { ...plan, blocked } : plan?.failure || plan;
        return;
      }
      if (plan.confirmation_required) {
        switching = plan;
        return;
      }
      if ((await startSetup({ lifecycle: plan })) === "setup") afterStart();
      return;
    }
    if ((await startSetup()) === "setup") afterStart();
  }

  async function afterStart() {
    reload("lifecycle", "alignment");
    goTo("release");
    await loadReleases();
  }

  function releaseLabel(release) {
    const when = release.published_at || release.created_at;
    const parts = [release.selection_label || (release.channel === "development" ? `${release.display_name || release.name || release.tag}${release.revision_short ? ` · ${release.revision_short}` : ""}` : release.channel === "latest" ? t("setup.release.latestBuild") : release.name || release.tag)];
    if (release.prerelease) parts.push(t("setup.release.notStable"));
    if (release.prepared) parts.push(t("setup.release.prepared"));
    if (release.active) parts.push(t("setup.release.active"));
    if (when && !Number.isNaN(new Date(when).getTime())) parts.push(f.dateTime(when));
    if (release.selectable === false && release.reason) parts.push(release.reason);
    return parts.join(" · ");
  }

</script>

{#snippet failure(error)}
  {@const view = errorView(error, t)}
  <p class="form-error" lang={view.lang}>{view.text}</p>
{/snippet}

{#if !active}
  <Card icon="package-plus" tone="c-out" title={t("setup.start.title")} subtitle={t("setup.start.sub")} span="span-12">
    <div class="start" data-testid="setup-start">
      <p>{t("setup.start.lead")}</p>
      <ol class="steps-preview">
        {#each SETUP_STEPS as step (step)}<li><span class="icon-tile sm {SETUP_STEP_LOOKS[step].tone}"><Icon name={SETUP_STEP_LOOKS[step].icon} /></span>{t(`setup.step.${step}`)}</li>{/each}
      </ol>
      {#if setup.startError}
        {#if setup.startError.blocked}<p class="note warn"><Icon name="triangle-alert" />{t("setup.start.blocked")} <a href="#/system">{t("workflow.openRecovery")}</a></p>{:else}{@render failure(setup.startError)}{/if}
      {/if}
      <button class="btn" type="button" onclick={begin} disabled={setup.starting} data-testid="setup-begin"><Icon name="play" size="sm" />{setup.starting ? t("setup.start.starting") : t("setup.start.begin")}</button>
    </div>
  </Card>
{:else}
  <div class="stepper span-12" role="tablist" aria-label={t("setup.steps")}>
    {#each SETUP_STEPS as step, index (step)}
      <button type="button" role="tab" class="step-tab" class:current={setup.step === step} aria-selected={setup.step === step ? "true" : "false"} disabled={locked(step)} onclick={() => openStep(step)} data-testid={`setup-step-${step}`}>
        <span class="n num">{String(index + 1).padStart(2, "0")}</span>
        <span class="words"><span class="label">{t(`setup.step.${step}`)}</span>{#if stepStatus(step)}<span class="chip muted">{stepStatus(step)}</span>{/if}</span>
      </button>
    {/each}
  </div>
  <p class="verdict span-12" role="status" data-testid="setup-verdict">{verdict}.</p>

  {#if setup.stale}
    <div class="note warn span-12" role="alert" data-testid="setup-stale">
      <Icon name="triangle-alert" /><span>{t("setup.stale")}</span>
      <button class="btn secondary small" type="button" onclick={rejoinCurrent}>{t("setup.rejoin")}</button>
    </div>
  {/if}
  {#if setup.cleanup}
    <div class="note warn span-12" role="alert" data-testid="setup-cleanup">
      <Icon name="triangle-alert" /><span>{t("setup.cleanup")}</span><a class="btn secondary small" href="#/system">{t("workflow.openRecovery")}</a>
    </div>
  {/if}
  {#if setup.intentNeeded}
    <div class="note warn span-12" role="alert" data-testid="setup-intent">
      <Icon name="triangle-alert" /><span>{t("setup.intentNeeded")}</span>
      <button class="btn secondary small" type="button" onclick={begin} disabled={setup.starting}>{t("setup.confirmAgain")}</button>
    </div>
  {/if}

  {#if setup.step === "release"}
    <Card icon="circle-arrow-up" tone="c-out" title={t("setup.release.title")} subtitle={t("setup.release.sub")} span="span-8">
      {#snippet actions()}
        <button class="icon-btn" type="button" onclick={() => loadReleases()} disabled={setup.releasesLoading} aria-label={t("common.refresh")} title={t("common.refresh")}><Icon name="refresh-cw" /></button>
      {/snippet}
      <div class="release" data-testid="setup-release">
        {#if setup.releasesLoading && !setup.releases.length}
          <Empty text={t("common.loading")} icon="loader" />
        {:else if setup.releasesError && !setup.releases.length}
          <Empty error text={t("setup.release.unavailable")} />
        {:else}
          <label class="field">
            <span>{t("setup.release.choose")}</span>
            <select class="input" value={setup.build.tag || ""} onchange={(event) => choose(event.currentTarget.value)} disabled={setup.build.locked || ["validating", "confirming", "updating", "reconnecting"].includes(setup.build.state)} data-testid="setup-release-select">
              {#each grouped as group (group.id)}
                <optgroup label={t(`setup.channel.${group.id}`)}>
                  {#each group.releases as release (release.tag)}<option value={release.tag} disabled={release.selectable === false} lang="en">{releaseLabel(release)}</option>{/each}
                </optgroup>
              {/each}
            </select>
          </label>
          {#if selected}
            <p class="badges">
              <span class="badge">{t(`setup.channel.${channelGroup(selected.channel) || "stable"}`)}</span>
              {#if isDevelopmentTag(selected.tag)}<span class="badge">{t("setup.release.development")}</span>{/if}
              {#if selected.docker_supported === false}<span class="badge warn">{t("setup.release.unsupported")}</span>{/if}
            </p>
          {/if}
          {#each setup.warnings as warning, index (index)}<p class="note warn"><Icon name="triangle-alert" /><span lang="en">{warning}</span></p>{/each}
          {#if message}<p class="message" data-testid="setup-build-message">{t(`setup.build.${message}`)}</p>{/if}
          {#if setup.build.error}{@render failure(setup.build.error)}{/if}
          {#if checks}
            <ul class="checks" data-testid="setup-checks">
              {#each CHECKS as key (key)}
                {@const archive = key === "embedded_resources_match" && setup.build.result?.embedded_resources_applicable === false}
                <li data-passed={archive ? "n/a" : checks[key] === true ? "yes" : "no"}><Icon name={archive ? "circle-minus" : checks[key] === true ? "circle-check" : "circle-x"} size="sm" /><span>{archive ? t("setup.check.releaseArchive") : t(`setup.check.${key}`)}</span></li>
              {/each}
            </ul>
          {/if}
          <div class="row-wrap buttons">
            {#if choices.left.label !== "update" || choices.left.enabled}
              <button class="btn secondary" type="button" onclick={leftAction} disabled={!choices.left.enabled} data-testid="setup-left">{t(`setup.action.${choices.left.label}`)}</button>
            {/if}
            <button class="btn" type="button" onclick={rightAction} disabled={!choices.right.enabled} data-testid="setup-right">{t(`setup.action.${choices.right.label}`)}</button>
          </div>
        {/if}
      </div>
    </Card>
    <Card icon="route" tone="c-out" title={t("setup.pipeline.title")} subtitle={t("setup.pipeline.sub")} span="span-4">
      <ol class="pipeline" data-testid="setup-pipeline">
        {#each PIPELINE as stage, index (stage)}
          <li data-state={pipeline.states[index]}><Status tone={pipeline.states[index] === "done" ? "c-ok" : pipeline.states[index] === "failed" ? "c-fault" : pipeline.states[index] === "active" ? "c-acc" : "c-idle"} text={t(`setup.pipeline.${stage}`)} /></li>
        {/each}
      </ol>
      {#if recovery}
        <div class="recovery" role="alert" data-testid="setup-recovery">
          <p class="note warn"><Icon name="triangle-alert" />{#if recovery.reason}{t(`setup.recovery.${recovery.reason}`)}{:else}<span lang="en">{recovery.error}</span>{/if}</p>
          <div class="row-wrap buttons">
            <button class="btn small" type="button" onclick={() => resumeTransition(transition)} disabled={!recovery.canResume || Boolean(setup.recovering)} data-testid="setup-recovery-resume"><Icon name={setup.recovering === "resume" ? "loader" : "rotate-cw"} size="sm" />{setup.recovering === "resume" ? t("setup.recovery.resuming") : t("setup.recovery.resume")}</button>
            <button class="btn secondary small" type="button" onclick={() => (confirmDiscard = true)} disabled={!recovery.canDiscard || Boolean(setup.recovering)} data-testid="setup-recovery-discard"><Icon name={setup.recovering === "discard" ? "loader" : "trash-2"} size="sm" />{setup.recovering === "discard" ? t("setup.recovery.discarding") : t("setup.recovery.discard")}</button>
          </div>
          {#if setup.recoveryError}{@render failure(setup.recoveryError)}{/if}
        </div>
      {/if}
      {#if setup.template}
        <div class="facts two">
          <div class="fact"><div class="fact-label">{t("setup.release.template")}</div><div class="fact-value num" lang="en">{setup.template.tag}</div></div>
          <div class="fact"><div class="fact-label">{t("setup.release.image")}</div><div class="fact-value num small-value" lang="en">{setup.template.dockerImage || "—"}</div></div>
        </div>
      {/if}
    </Card>
  {:else if setup.step === "devices"}
    <SetupDevices />
  {:else if setup.step === "config"}
    <SetupConfig onrestart={() => (confirmRestart = true)} />
  {:else if setup.step === "deployment"}
    <SetupDeployment />
  {:else}
    <SetupStart />
  {/if}

  <div class="row-wrap step-nav span-12">
    {#if stepIndex > 0}<button class="btn secondary small" type="button" onclick={() => openStep(SETUP_STEPS[stepIndex - 1])}>{t("setup.back")}</button>{/if}
    {#if setup.step !== "release" && next}
      <button class="btn small" type="button" onclick={advance} disabled={!canAdvance} data-testid="setup-next">{setup.step === "config" ? (setupDraft.busy === "write" ? t("setup.saving") : t("setup.continueDeployment")) : t("setup.next")}</button>
    {/if}
    {#if setupDraft.navError && setup.step === "config"}
      <p class="form-error" role="alert">{#if setupDraft.navError.raw}<span lang="en">{setupDraft.navError.raw}</span>{:else if setupDraft.navError.key === "busy"}{t("setup.config.busy", { what: BUSY_OPERATIONS.includes(setupDraft.navError.operation) ? t(`setup.operation.${setupDraft.navError.operation}`) : t("setup.operation.other") })}{:else}{t(`setup.config.status.${setupDraft.navError.key}`)}{/if}</p>
    {/if}
    <button class="btn secondary small danger-text" type="button" onclick={() => (confirmRestart = true)} disabled={setup.restarting} data-testid="setup-restart"><Icon name="rotate-ccw" size="sm" />{t("setup.restart")}</button>
    {#if setup.restartError}{@render failure(setup.restartError)}{/if}
  </div>
{/if}

<ConfirmDialog
  open={setup.confirmReplace}
  title={t("setup.replaceTitle")}
  lines={[t("setup.replaceLine")]}
  confirmLabel={t("setup.start.begin")}
  danger
  onconfirm={async () => { setup.confirmReplace = false; if ((await startSetup({ confirm: true })) === "setup") afterStart(); }}
  oncancel={() => (setup.confirmReplace = false)}
/>
<ConfirmDialog
  open={Boolean(switching)}
  title={t("setup.switchTitle")}
  lines={[t("setup.switchLine")]}
  confirmLabel={t("setup.start.begin")}
  danger
  onconfirm={async () => { const plan = switching; switching = null; if ((await startSetup({ lifecycle: plan })) === "setup") afterStart(); }}
  oncancel={() => (switching = null)}
/>
<ConfirmDialog
  open={confirmDiscard}
  title={t("setup.recovery.discardTitle")}
  lines={[t("setup.recovery.discardLine"), t("setup.restartSafe")]}
  confirmLabel={t("setup.recovery.discard")}
  danger
  onconfirm={async () => { confirmDiscard = false; if (await discardSetup()) cleared(); }}
  oncancel={() => (confirmDiscard = false)}
/>
<ConfirmDialog
  open={confirmRestart}
  title={t("setup.restartTitle")}
  lines={[t("setup.restartLine"), t("setup.restartSafe")]}
  confirmLabel={t("setup.restart")}
  danger
  onconfirm={async () => { confirmRestart = false; if (await restart(cleared)) await loadReleases(); else if (setup.step === "config") schedulePreview(); }}
  oncancel={() => (confirmRestart = false)}
/>

<style>
  .start { display: flex; flex-direction: column; gap: var(--s3); align-items: flex-start; }
  .start p { margin: 0; }
  .steps-preview, .checks, .pipeline { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--s2); }
  .steps-preview li { display: flex; gap: var(--s2); align-items: center; }
  .n { display: inline-grid; place-items: center; min-width: 26px; height: 26px; border-radius: 8px; background: var(--m-surface-3); font-size: 12px; font-weight: 600; }
  .stepper { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: var(--s2); }
  .step-tab { display: flex; align-items: center; gap: var(--s2); padding: var(--s2) var(--s3); border-radius: var(--m-radius-control); border: 1px solid var(--m-line); background: var(--m-surface-1); color: var(--m-text); cursor: pointer; min-width: 0; text-align: left; }
  .step-tab.current { border-color: var(--m-accent); }
  .step-tab:disabled { opacity: 0.5; cursor: default; }
  .step-tab .words { display: flex; flex-direction: column; min-width: 0; }
  .step-tab .label, .step-tab .chip { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
  .step-tab .chip { font-size: 11.5px; }
  .verdict { margin: 0; font-size: 13px; color: var(--m-muted); }
  .release { display: flex; flex-direction: column; gap: var(--s3); }
  .badges { display: flex; gap: var(--s2); flex-wrap: wrap; margin: 0; }
  .badge.warn { color: var(--m-warn); }
  .message { margin: 0; font-weight: 560; }
  .checks li { display: flex; gap: var(--s2); align-items: center; font-size: 13px; }
  .buttons { gap: var(--s2); }
  .pipeline { margin-bottom: var(--s3); }
  .recovery { display: flex; flex-direction: column; gap: var(--s2); margin-bottom: var(--s3); }
  .recovery p { margin: 0; }
  .step-nav { gap: var(--s2); align-items: center; }
  @container content (max-width: 720px) { .stepper { grid-template-columns: repeat(5, minmax(0, 1fr)); } .step-tab .words { display: none; } }
</style>
