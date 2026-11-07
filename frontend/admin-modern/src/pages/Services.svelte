<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import Empty from "../../../shared/ui/Empty.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import ConfirmDialog from "../../../shared/ui/ConfirmDialog.svelte";
  import StaleNote from "../widgets/StaleNote.svelte";
  import ServiceOutcome from "../widgets/ServiceOutcome.svelte";
  import PlanRefusal from "../widgets/PlanRefusal.svelte";
  import { t, fmt, say } from "../i18n/i18n.svelte.js";
  import { system, ensure, keepWithOverview, reload } from "../state/system.svelte.js";
  import { serviceAction, runServiceAction } from "../state/services.svelte.js";
  import { serviceView, plannedChanges, canSync, serviceOutcome, SERVICES } from "../lib/services.js";
  import { errorView } from "../lib/errors.js";
  import { href } from "../app/router.svelte.js";

  ensure("overview", "plan");
  keepWithOverview("plan");

  const ICONS = { ems: "cpu", influxdb: "database" };
  const DESIRED = ["running", "stopped", "unavailable"];

  const f = $derived(fmt());
  const plan = $derived(system.plan.data);
  const overview = $derived(system.overview.data);
  const changes = $derived(plannedChanges(plan));
  const readable = $derived(Boolean(plan) && plan.ok !== false);
  const stale = $derived(Boolean(system.plan.error));

  let confirming = $state("");
  let asking = $state(false);
  const busy = $derived(serviceAction.busy);
  const outcome = $derived(serviceOutcome(serviceAction.outcome, t));

  function desiredText(service) {
    const desired = plan?.desired?.[service]?.desired;
    return DESIRED.includes(desired) ? t(`services.desired.${desired}`) : "—";
  }

  function version(service) {
    return overview?.containers?.[service]?.tag || null;
  }

  /**
   * The confirmation names what the server will do, so it is asked against a
   * plan read just now, not the one that has sat on screen since the page
   * opened: a plan that cannot be read again is not confirmed at all.
   */
  async function ask(kind) {
    asking = true;
    await reload("plan");
    asking = false;
    const current = system.plan.data;
    if (system.plan.error || !current || current.ok === false) return;
    if (kind === "sync" && !canSync(current)) return;
    if (kind === "repair" && !(current.available && current.analytics_enabled)) return;
    confirming = kind;
  }

  function run(kind) {
    confirming = "";
    runServiceAction(kind);
  }
</script>

<Card icon="server" tone="c-inv" title={t("services.title")} subtitle={t("services.sub")} span="span-12">
  {#snippet actions()}
    <button class="icon-btn" type="button" onclick={() => reload("overview", "plan")} aria-label={t("common.refresh")} title={t("common.refresh")} disabled={system.plan.loading}><Icon name="refresh-cw" /></button>
  {/snippet}
  <StaleNote resource={system.plan} />
  {#if plan && !readable}
    <Empty error text={t("services.planUnreadable")}>{#if plan.message}<span class="muted" lang="en">{" "}{plan.message}</span>{/if}</Empty>
  {:else if plan}
    <div class="services">
      {#each SERVICES as service (service)}
        {@const view = serviceView(plan, service)}
        <div class="service inner" data-service={service} data-state={view.state}>
          <span class="icon-tile sm {view.tone}"><Icon name={ICONS[service]} /></span>
          <div class="words">
            <div class="name">{t(`service.name.${service}`)}</div>
            <div class="muted small num">
              {[view.name, version(service), view.startedAt ? t("overview.since", { when: f.relative(view.startedAt) }) : ""].filter(Boolean).join(" · ") || t(`service.detail.${view.state}`)}
            </div>
          </div>
          <Status tone={view.tone} text={view.label || t(`service.state.${view.state}`)} />
        </div>
      {/each}
    </div>
  {:else if system.plan.error}
    <Empty error {...errorView(system.plan.error, t)} />
  {:else}
    <Empty text={t("common.loading")} icon="loader" />
  {/if}
</Card>

{#if readable || outcome}
  <Card icon="rotate-cw" tone="c-out" title={t("services.syncTitle")} subtitle={t("services.syncSub")} span="span-8">
    {#if readable}
    <div class="facts">
      <div class="fact"><div class="fact-label wrap">{t("services.emsShould")}</div><div class="fact-value wrap">{desiredText("ems")}</div></div>
      <div class="fact"><div class="fact-label wrap">{t("services.analyticsShould")}</div><div class="fact-value wrap">{desiredText("influxdb")}</div></div>
      <div class="fact"><div class="fact-label wrap">{t("services.settingsState")}</div><div class="fact-value wrap">{t(`services.config.${["pending", "current"].includes(plan.config_state?.state) ? plan.config_state.state : "unknown"}`)}</div></div>
    </div>
    <h3 class="sub">{t("services.whatHappens")}</h3>
    {#if !plan.available}
      <PlanRefusal {plan} />
    {:else if changes.length}
      <ul class="changes">
        {#each changes as change, index (index)}<li>{say(change)}</li>{/each}
        <li class="muted">{t("services.nothingRemoved")}</li>
      </ul>
    {:else}
      <p class="muted">{t("services.noChanges")}</p>
    {/if}
    <div class="row-wrap actions">
      <button class="btn" type="button" disabled={!canSync(plan) || stale || asking || Boolean(busy)} onclick={() => ask("sync")} data-testid="sync">
        <Icon name={busy === "sync" ? "loader" : "rotate-cw"} size="sm" />{busy === "sync" ? t("services.syncing") : t("services.sync")}
      </button>
      {#if plan.available && plan.analytics_enabled}
        <button class="btn secondary" type="button" disabled={stale || asking || Boolean(busy)} onclick={() => ask("repair")} data-testid="repair">
          <Icon name={busy === "repair" ? "loader" : "wrench"} size="sm" />{busy === "repair" ? t("services.repairing") : t("services.repair")}
        </button>
      {/if}
      <a class="btn secondary" href={href("diagnostics")}><Icon name="stethoscope" size="sm" />{t("services.runChecks")}</a>
    </div>
    {/if}
    <div role="status" aria-live="polite">
      {#if outcome}<div class="said"><ServiceOutcome {outcome} /></div>{/if}
    </div>
  </Card>
{/if}

{#if overview}
  <Card icon="boxes" tone="c-inv" title={t("services.dockerTitle")} subtitle={t("services.dockerSub")} span={readable || outcome ? "span-4" : "span-12"}>
    <div class="facts two">
      <div class="fact"><div class="fact-label">{t("services.dockerAccess")}</div><div class="fact-value">{overview?.docker?.available ? t("services.dockerOk") : t("services.dockerUnavailable")}</div></div>
      <div class="fact"><div class="fact-label">{t("version.docker")}</div><div class="fact-value num" class:na={!overview?.docker?.server_version}>{overview?.docker?.server_version || "—"}</div></div>
    </div>
    {#if overview && !overview.docker?.available && overview.docker?.error}<p class="note warn"><Icon name="triangle-alert" /><span lang="en">{overview.docker.error}</span></p>{/if}
  </Card>
{/if}

<ConfirmDialog
  open={confirming === "sync"}
  title={t("services.confirmSyncTitle")}
  lines={[t("services.confirmSync")]}
  confirmLabel={t("services.sync")}
  onconfirm={() => run("sync")}
  oncancel={() => (confirming = "")}
/>
<ConfirmDialog
  open={confirming === "repair"}
  title={t("services.confirmRepairTitle")}
  lines={[t("services.confirmRepair")]}
  confirmLabel={t("services.repair")}
  onconfirm={() => run("repair")}
  oncancel={() => (confirming = "")}
/>

<style>
  .services { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--s3); }
  .service { display: flex; align-items: center; gap: var(--s3); min-width: 0; }
  .words { flex: 1; min-width: 0; }
  .name { font-weight: 620; }
  .small { font-size: 12.5px; overflow-wrap: anywhere; }
  .sub { font-size: 13px; font-weight: 620; margin: var(--s4) 0 var(--s2); }
  .changes { margin: 0; padding-left: 18px; display: flex; flex-direction: column; gap: 4px; font-size: 13.5px; }
  .actions { margin-top: var(--s4); gap: var(--s2); }
  .said { margin-top: var(--s4); }
  @container content (max-width: 640px) { .services { grid-template-columns: minmax(0, 1fr); } }
</style>
