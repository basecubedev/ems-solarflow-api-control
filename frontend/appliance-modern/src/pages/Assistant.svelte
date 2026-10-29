<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import AssistantWelcome from "../../../shared/assistant/AssistantWelcome.svelte";
  import AssistantStep from "../../../shared/assistant/AssistantStep.svelte";
  import TaskTile from "../../../shared/assistant/TaskTile.svelte";
  import StepList from "../../../shared/assistant/StepList.svelte";
  import TaskGrid from "../../../shared/assistant/TaskGrid.svelte";
  import KeepChoice from "../../../shared/assistant/KeepChoice.svelte";
  import { t, tn } from "../i18n/i18n.svelte.js";
  import { href } from "../app/router.svelte.js";
  import { host, ensure, held } from "../state/host.svelte.js";
  import { session } from "../state/session.svelte.js";
  import { adminLink } from "../lib/links.js";
  import { rankedFindings, sessionFindings } from "../lib/findings.js";
  import { applianceKind, assistantSteps, assistantTasks, readStatus, stepProgress } from "../lib/assistant.js";

  $effect(() => {
    if (host.backup === undefined) ensure("backup");
  });

  const status = $derived(host.status);
  const backup = $derived(held("backup"));
  const kind = $derived(applianceKind(status));
  const steps = $derived(assistantSteps({ status, backup }));
  const progress = $derived(stepProgress(steps));
  const admin = $derived(host.settings && !host.settings.error ? adminLink(globalThis.location, host.settings, readStatus(status)) : "");
  const tasks = $derived(assistantTasks(admin));
  const attention = $derived(readStatus(status) ? rankedFindings(status, sessionFindings(session.audit, session.confirmed)).length : 0);

  const ADMIN_TEXTS = {
    healthy: "assistant.step.admin.done",
    bootstrap: "assistant.step.admin.bootstrap",
    missing: "assistant.step.admin.missing",
    unhealthy: "assistant.step.admin.unhealthy",
    stopped: "assistant.step.admin.stopped",
    starting: "assistant.step.admin.starting",
    docker: "assistant.step.admin.docker",
    live: "assistant.step.admin.live",
  };

  function lead() {
    if (kind !== "unknown") return t(`assistant.lead.${kind}`);
    return readStatus(status) || status?.error ? t("assistant.lead.unreadable") : t("common.loading");
  }

  /** Why a step cannot be told yet: its answer is still being read, or could not be read. */
  function unknownText(step) {
    if (step.kind === "live" || step.kind === "starting") return t(ADMIN_TEXTS[step.kind]);
    if (step.id === "backup") return backup?.error ? t("assistant.step.unreadable") : t("common.loading");
    return readStatus(status) || status?.error ? t("assistant.step.unreadable") : t("common.loading");
  }

  function stepText(step) {
    if (step.state === "later") return t("assistant.step.later");
    if (step.state === "unknown") return unknownText(step);
    if (step.id === "network") return step.state === "done" ? t("assistant.step.network.done", { connection: step.connection || "—" }) : t("assistant.step.network.todo");
    if (step.id === "admin") return t(ADMIN_TEXTS[step.kind]);
    if (step.id === "ems") return step.state === "done" ? t("assistant.step.ems.done") : t(`assistant.step.ems.${step.kind}`);
    if (step.id === "updates") {
      if (step.state === "done") return t("assistant.step.updates.done");
      return step.kind === "doubt" ? t("assistant.step.updates.doubt") : tn("assistant.step.updates.security", step.count);
    }
    return step.state === "done" ? t("assistant.step.backup.done") : t("assistant.step.backup.todo");
  }

  /** The count, once every step is known; a step that cannot be told yet is never counted either way. */
  function progressText() {
    if (steps.some((step) => step.state === "unknown")) return "";
    return progress.done === progress.total ? t("assistant.next.allDone") : t("assistant.next.progress", progress);
  }
</script>

{#snippet stepActions(step)}
  {#if step.id === "network"}
    <a class="btn" class:secondary={!step.next} href={href("network")} data-testid="assistant-network">{t("assistant.step.network.action")}</a>
  {:else if step.id === "admin" && step.kind === "docker"}
    <a class="btn" class:secondary={!step.next} href={href("diagnostics")} data-testid="assistant-diagnostics">{t("assistant.step.admin.diagnostics")}</a>
  {:else if step.id === "admin"}
    <a class="btn" class:secondary={!step.next} href={href("admin")} data-testid="assistant-admin">{step.kind === "bootstrap" ? t("assistant.step.admin.install") : t("assistant.step.admin.repair")}</a>
  {:else if step.id === "ems" && admin}
    <a class="btn" class:secondary={!step.next} href={admin} target="_blank" rel="noopener" data-testid="assistant-open-admin"><Icon name="external-link" size="sm" />{t("nav.openAdmin")}</a>
  {:else if step.id === "updates"}
    <a class="btn" class:secondary={!step.next} href={href("updates")} data-testid="assistant-updates">{t("overview.openUpdates")}</a>
  {:else if step.id === "backup"}
    <a class="btn" class:secondary={!step.next} href={href("access")} data-testid="assistant-access">{t("assistant.step.backup.action")}</a>
  {/if}
{/snippet}

<AssistantWelcome lead={lead()} {kind} attention={attention ? tn("overview.attention", attention) : ""} attentionHref={href("overview")} />

<Card icon="list-checks" tone="c-out" title={t("assistant.next.title")} subtitle={progressText()} span="span-12">
  <StepList>
    {#each steps as step (step.id)}
      <AssistantStep
        {step}
        title={t(`assistant.step.${step.id}.title`)}
        text={stepText(step)}
        actions={step.state === "todo" && (step.id !== "ems" || admin) ? stepActions : undefined}
      />
    {/each}
  </StepList>
</Card>

<Card icon="route" tone="c-out" title={t("assistant.tasks.title")} span="span-12">
  <TaskGrid>
    {#each tasks as task (task.id)}
      <TaskTile
        id={task.id}
        href={task.external ? admin : href(task.page)}
        icon={task.icon}
        tone={task.tone}
        external={Boolean(task.external)}
        title={t(`assistant.task.${task.id}.title`)}
        text={t(`assistant.task.${task.id}.text`)}
      />
    {/each}
  </TaskGrid>
</Card>

<KeepChoice hint={t("assistant.keepHint")} overviewHref={href("overview")} />
