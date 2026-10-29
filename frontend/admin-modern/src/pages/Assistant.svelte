<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import Empty from "../../../shared/ui/Empty.svelte";
  import AssistantWelcome from "../../../shared/assistant/AssistantWelcome.svelte";
  import AssistantStep from "../../../shared/assistant/AssistantStep.svelte";
  import TaskTile from "../../../shared/assistant/TaskTile.svelte";
  import StepList from "../../../shared/assistant/StepList.svelte";
  import TaskGrid from "../../../shared/assistant/TaskGrid.svelte";
  import KeepChoice from "../../../shared/assistant/KeepChoice.svelte";
  import { t, tn, fmt } from "../i18n/i18n.svelte.js";
  import { system, ensure } from "../state/system.svelte.js";
  import { assistantSteps, assistantTasks, attentionCount, dashboardStep, installKind, installed, managesExisting, stepProgress } from "../lib/assistant.js";
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

  function finishText() {
    if (finish.state === "ready") return t("assistant.step.dashboard.ready");
    return finish.reason ? t(`assistant.step.dashboard.${finish.reason}`) : unknownText(finish);
  }

  /** The count, once every step is known; a step that cannot be told yet is never counted either way. */
  function progressText() {
    if (steps.some((step) => step.state === "unknown")) return "";
    return progress.done === progress.total ? t("assistant.next.allDone") : t("assistant.next.progress", progress);
  }
</script>

{#snippet stepActions(step)}
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
{/snippet}

{#snippet dashboardActions()}
  {#if finish.reason === "off"}
    <a class="btn secondary" href={href("settings")} data-testid="assistant-dashboard-settings">{t("assistant.task.settings.title")}</a>
  {:else}
    <a class="btn secondary" href={dashboard} data-testid="assistant-dashboard"><Icon name="arrow-up-right" size="sm" />{t("nav.dashboard")}</a>
  {/if}
{/snippet}

{#if system.overview.error}
  <div class="span-12" data-testid="assistant-unreadable">
    <Empty error {...errorView(system.overview.error, t)} />
    {#if overview && system.overview.loadedAt}
      <p class="note" data-testid="assistant-stale"><Icon name="clock" />{t("overview.stale", { when: f.when(system.overview.loadedAt) })}</p>
    {/if}
  </div>
{/if}

<AssistantWelcome lead={lead()} {kind} attention={attention ? tn("overview.attention", attention) : ""} attentionHref={href("overview")} />

<Card icon="list-checks" tone="c-out" title={t("assistant.next.title")} subtitle={progressText()} span="span-12">
  <StepList>
    {#each steps as step (step.id)}
      <AssistantStep {step} title={t(`assistant.step.${step.id}.title`)} text={stepText(step)} actions={step.state === "todo" ? stepActions : undefined} />
    {/each}
    <AssistantStep
      step={finish}
      title={t("assistant.step.dashboard.title")}
      text={finishText()}
      actions={finish.state === "ready" || finish.reason === "docker" || finish.reason === "off" ? dashboardActions : undefined}
    />
  </StepList>
</Card>

{#if tasks.length}
  <Card icon="route" tone="c-out" title={t("assistant.tasks.title")} span="span-12">
    <TaskGrid>
      {#each tasks as task (task.id)}
        <TaskTile
          id={task.id}
          href={task.tab ? `${href(task.page)}/${task.tab}` : href(task.page)}
          icon={task.icon}
          tone={task.tone}
          title={t(`assistant.task.${task.id}.title`)}
          text={t(`assistant.task.${task.id}.text`)}
        />
      {/each}
    </TaskGrid>
  </Card>
{/if}

<KeepChoice hint={t("assistant.keepHint")} overviewHref={href("overview")} />
