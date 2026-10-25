<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import Empty from "../../../shared/ui/Empty.svelte";
  import { t, tn, fmt } from "../i18n/i18n.svelte.js";
  import { system, ensure } from "../state/system.svelte.js";
  import { welcome, setWelcome } from "../state/prefs.svelte.js";
  import { assistantSteps, assistantTasks, attentionCount, dashboardStep, installKind, installed, managesExisting, stepProgress, STATE_LOOKS } from "../lib/assistant.js";
  import { healthVerdict } from "../lib/health.js";
  import { dashboardHref } from "../lib/links.js";
  import { href } from "../app/router.svelte.js";
  import { errorView } from "../lib/errors.js";

  ensure("overview", "config", "backups");

  const f = $derived(fmt());
  const overview = $derived(system.overview.data);
  const kind = $derived(installKind(overview));
  const steps = $derived(assistantSteps({ overview, config: system.config.data, backups: system.backups.data }));
  const progress = $derived(stepProgress(steps));
  const dashboard = $derived(dashboardHref(overview?.links?.dashboard_url));
  const finish = $derived(dashboardStep(overview, dashboard, system.config.data));
  const tasks = $derived(assistantTasks(overview));
  const attention = $derived(attentionCount(healthVerdict(overview, false)));

  const RESOURCES = { install: "overview", devices: "config", backup: "backups", dashboard: "overview" };
  const INSTALL_TEXTS = { none: "assistant.step.install.todo", partial: "assistant.step.install.partial", legacy: "assistant.step.install.legacy", missing: "assistant.step.install.noContainer" };

  function lead() {
    if (kind !== "unknown") return t(`assistant.lead.${kind}`);
    return system.overview.error ? t("overview.unreadable") : t("common.loading");
  }

  /** Why a step cannot be told yet: the installation it waits for, or its own answer, still loading or unreadable. */
  function unknownText(step) {
    const source = installed(overview) === null ? "overview" : RESOURCES[step.id];
    const config = system.config.data;
    if (source === "config" && config && config.status !== "ok") return t(`settings.config.${config.status === "missing" ? "missing" : "invalid"}`);
    return system[source].error ? t("assistant.step.unreadable") : t("common.loading");
  }

  function stepText(step) {
    if (step.state === "later") return t("assistant.step.later");
    if (step.state === "unknown") return unknownText(step);
    if (step.id === "install") return t(step.state === "done" ? "assistant.step.install.done" : INSTALL_TEXTS[step.kind]);
    if (step.id === "devices") {
      if (step.state === "done") return tn("assistant.step.devices.done", step.count);
      return step.placeholders.length ? t("assistant.step.devices.placeholder", { fields: step.placeholders.join(", ") }) : t("assistant.step.devices.todo");
    }
    return step.state === "done" ? t("assistant.step.backup.done", { when: f.when(step.at) }) : t("assistant.step.backup.todo");
  }

  /** The count, once every step is known; a step that cannot be told yet is never counted either way. */
  function progressText() {
    if (steps.some((step) => step.state === "unknown")) return "";
    return progress.done === progress.total ? t("assistant.next.allDone") : t("assistant.next.progress", progress);
  }

  function keepShowing(event) {
    setWelcome(event.currentTarget.checked);
  }
</script>

{#snippet mark(step)}
  <span class="icon-tile sm {['later', 'unknown', 'off'].includes(step.state) ? 'c-idle' : step.tone}"><Icon name={step.icon} /></span>
{/snippet}

{#snippet state(step)}
  <Status tone={STATE_LOOKS[step.state].tone} icon={STATE_LOOKS[step.state].icon} text={t(`assistant.state.${step.state}`)} />
{/snippet}

{#if system.overview.error}
  <div class="span-12" data-testid="assistant-unreadable">
    <Empty error {...errorView(system.overview.error, t)} />
    {#if overview && system.overview.loadedAt}
      <p class="note" data-testid="assistant-stale"><Icon name="clock" />{t("overview.stale", { when: f.when(system.overview.loadedAt) })}</p>
    {/if}
  </div>
{/if}

<section class="card welcome span-12" aria-labelledby="assistant-welcome" data-kind={kind} data-testid="assistant-welcome">
  <div class="welcome-head">
    <span class="icon-tile c-out"><Icon name="compass" /></span>
    <div class="welcome-words">
      <h2 class="welcome-title" id="assistant-welcome">{t("assistant.welcome")}</h2>
      <p class="muted">{lead()}</p>
    </div>
  </div>
  {#if attention}
    <a class="attention inner c-warn" href={href("overview")} data-testid="assistant-attention">
      <Icon name="triangle-alert" />
      <strong>{tn("overview.attention", attention)}</strong>
      <span class="muted">{t("assistant.toOverview")}</span>
      <Icon name="chevron-right" size="sm" />
    </a>
  {/if}
</section>


<Card icon="list-checks" tone="c-out" title={t("assistant.next.title")} subtitle={progressText()} span="span-12">
  <ol class="steps" data-testid="assistant-steps">
    {#each steps as step (step.id)}
      <li class="step inner {step.tone}" class:next={step.next} aria-current={step.next ? "step" : undefined} data-step={step.id} data-state={step.state}>
        {@render mark(step)}
        <div class="step-words">
          <strong>{t(`assistant.step.${step.id}.title`)}</strong>
          <span class="muted">{stepText(step)}</span>
        </div>
        {@render state(step)}
        {#if step.state === "todo"}
          <div class="step-actions">
            {#if step.id === "install" && step.kind === "missing"}
              <a class="btn" class:secondary={!step.next} href={href("updates")} data-testid="assistant-updates">{t("health.action.updates")}</a>
            {:else if step.id === "install" && step.kind === "legacy"}
              <a class="btn" class:secondary={!step.next} href={href("system")} data-testid="assistant-legacy">{t("system.legacyMove")}</a>
            {:else if step.id === "install" && step.kind === "partial" && managesExisting(overview)}
              <a class="btn" class:secondary={!step.next} href={href("overview")} data-testid="assistant-missing">{t("assistant.step.install.missing")}</a>
              <a class="btn secondary" href={href("setup")} data-testid="assistant-setup"><Icon name="play" size="sm" />{t("overview.startSetup")}</a>
            {:else if step.id === "install"}
              <a class="btn" class:secondary={!step.next} href={href("setup")} data-testid="assistant-setup"><Icon name="play" size="sm" />{t("overview.startSetup")}</a>
              {#if step.kind === "partial"}
                <a class="btn secondary" href={href("overview")} data-testid="assistant-missing">{t("assistant.step.install.missing")}</a>
              {:else}
                <a class="btn secondary" href={`${href("backups")}/restore`} data-testid="assistant-restore">{t("overview.restoreBackup")}</a>
              {/if}
            {:else if step.id === "devices"}
              <a class="btn" class:secondary={!step.next} href={`${href("settings")}/devices`} data-testid="assistant-devices">{t("assistant.step.devices.action")}</a>
            {:else}
              <a class="btn" class:secondary={!step.next} href={`${href("backups")}/create`} data-testid="assistant-backup">{t("backups.create")}</a>
            {/if}
          </div>
        {/if}
      </li>
    {/each}
    <li class="step inner {finish.tone}" data-step="dashboard" data-state={finish.state}>
      {@render mark(finish)}
      <div class="step-words">
        <strong>{t("assistant.step.dashboard.title")}</strong>
        <span class="muted">{finish.state === "ready" ? t("assistant.step.dashboard.ready") : finish.reason ? t(`assistant.step.dashboard.${finish.reason}`) : unknownText(finish)}</span>
      </div>
      {@render state(finish)}
      {#if finish.state === "ready" || finish.reason === "docker"}
        <div class="step-actions"><a class="btn secondary" href={dashboard} data-testid="assistant-dashboard"><Icon name="arrow-up-right" size="sm" />{t("nav.dashboard")}</a></div>
      {:else if finish.reason === "off"}
        <div class="step-actions"><a class="btn secondary" href={href("settings")} data-testid="assistant-dashboard-settings">{t("assistant.task.settings.title")}</a></div>
      {/if}
    </li>
  </ol>
</Card>

{#if tasks.length}
  <Card icon="route" tone="c-out" title={t("assistant.tasks.title")} span="span-12">
    <div class="tasks" data-testid="assistant-tasks">
      {#each tasks as task (task.id)}
        <a class="task inner {task.tone}" href={task.tab ? `${href(task.page)}/${task.tab}` : href(task.page)} data-task={task.id}>
          <span class="icon-tile sm"><Icon name={task.icon} /></span>
          <span class="task-words">
            <strong>{t(`assistant.task.${task.id}.title`)}</strong>
            <span class="muted">{t(`assistant.task.${task.id}.text`)}</span>
          </span>
          <Icon name="chevron-right" size="sm" />
        </a>
      {/each}
    </div>
  </Card>
{/if}

<div class="choice span-12">
  <div class="choice-words">
    <label class="check"><input type="checkbox" checked={welcome.choice !== "off"} onchange={keepShowing} data-testid="assistant-keep" />{t("assistant.keep")}</label>
    <p class="muted">{t("assistant.keepHint")}</p>
  </div>
  <a class="btn secondary" href={href("overview")} data-testid="assistant-overview">{t("assistant.toOverview")}<Icon name="arrow-right" size="sm" /></a>
</div>

<style>
  .welcome { display: flex; flex-direction: column; gap: var(--s4); border-color: color-mix(in srgb, var(--output) 30%, var(--m-line)); }
  .welcome-head { display: flex; align-items: center; gap: var(--s4); }
  .welcome-head > .icon-tile { width: 48px; height: 48px; border-radius: 14px; }
  .welcome-head > .icon-tile :global(svg.i) { width: 24px; height: 24px; }
  .attention { display: flex; align-items: center; gap: var(--s2); flex-wrap: wrap; text-decoration: none; border: 1px solid color-mix(in srgb, var(--c) 40%, var(--m-line)); }
  .attention:hover { background: var(--m-surface-3); }
  .attention > :global(svg.i:first-child) { color: var(--c); }
  .attention > :global(svg.i:last-child) { color: var(--m-muted-2); margin-left: auto; }
  .welcome-words { min-width: 0; }
  .welcome-title { margin: 0; font-size: 19px; font-weight: 650; letter-spacing: -0.01em; }
  .welcome-words p { margin: 3px 0 0; }
  .steps { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--s2); }
  .step { display: flex; align-items: center; gap: var(--s3); flex-wrap: wrap; border: 1px solid var(--m-line); }
  .step.next { border-color: color-mix(in srgb, var(--c) 45%, var(--m-line)); background: color-mix(in srgb, var(--c) 7%, var(--m-surface-2)); }
  .step-words { display: flex; flex-direction: column; gap: 2px; flex: 1 1 220px; min-width: 0; }
  .step-words .muted, .task-words .muted { font-size: 12.5px; }
  .step-actions { display: flex; flex-wrap: wrap; gap: var(--s2); }
  .tasks { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--s3); }
  .task { display: flex; align-items: flex-start; gap: var(--s3); text-decoration: none; min-width: 0; border: 1px solid var(--m-line); transition: border-color 0.15s; }
  .task:hover { border-color: color-mix(in srgb, var(--c) 45%, var(--m-line)); background: var(--m-surface-3); }
  .task > :global(svg.i:last-child) { color: var(--m-muted-2); margin-left: auto; align-self: center; flex: none; }
  .task-words { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .choice { display: flex; align-items: center; gap: var(--s4); flex-wrap: wrap; padding: var(--s2) var(--s1); }
  .choice-words { flex: 1 1 260px; min-width: 0; }
  .choice-words p { margin: 4px 0 0 24px; font-size: 12.5px; }
  @container content (max-width: 900px) { .tasks { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
  @container content (max-width: 560px) {
    .tasks { grid-template-columns: minmax(0, 1fr); }
    .welcome-head { gap: var(--s3); }
    .welcome-head > .icon-tile { width: 40px; height: 40px; border-radius: 12px; }
  }
</style>
