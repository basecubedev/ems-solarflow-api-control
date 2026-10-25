<script>
  import Card from "../../../../shared/ui/Card.svelte";
  import Icon from "../../../../shared/ui/Icon.svelte";
  import Status from "../../../../shared/ui/Status.svelte";
  import ConfirmDialog from "../../../../shared/ui/ConfirmDialog.svelte";
  import StepList from "../StepList.svelte";
  import { t } from "../../i18n/i18n.svelte.js";
  import { dashboardHref } from "../../lib/links.js";
  import { DOCKER_STATES, conflictService, jobSteps, serviceKey, startMessage, startTone } from "../../lib/setup.js";
  import { setup } from "../../state/setup.svelte.js";
  import { setupDeploy, loadPlan, loadStatus, repairPermissions, resolveConflict, startEms } from "../../state/setupDeploy.svelte.js";

  let replacing = $state(false);

  const start = $derived(setupDeploy.start);
  const docker = $derived(setupDeploy.docker);
  const message = $derived(startMessage(start, { prepared: setupDeploy.prepared, dockerReady: setupDeploy.dockerReady, docker }));
  const steps = $derived(jobSteps(start.steps));
  const conflict = $derived(start.conflict);
  const service = $derived(conflict ? t(`setup.service.${conflictService(conflict)}`) : "");
  const replace = $derived(conflict?.replace_available === true);
  const safe = $derived(conflict?.safe_fix_available === true);
  const services = $derived(start.services.length ? start.services : setupDeploy.images.map((image) => ({ service: image.service, image: image.image, state: null, status: "pending" })));
  const identity = $derived(setupDeploy.plan?.runtime_identity || null);
  const href = $derived(dashboardHref(start.dashboardUrl || "http://localhost:8080"));
  const blocked = $derived(start.status === "running" || !setupDeploy.prepared || !setupDeploy.dockerReady || Boolean(conflict) || start.errorCode === "workspace_permission_denied");

  $effect(() => {
    loadPlan().then(loadStatus);
  });

  function resolve() {
    if (replace) replacing = true;
    else resolveConflict();
  }
</script>

<Card icon="play" tone={startTone(start, message)} title={t("setup.run.title")} subtitle={t("setup.run.sub")} span="span-8">
  <div class="stack" data-testid="setup-start-step">
    {#if !setupDeploy.prepared}<p class="note warn"><Icon name="triangle-alert" />{t("setup.run.blocked")}</p>{/if}
    <div class="facts two">
      <div class="fact"><div class="fact-label">{t("setup.deploy.workspace")}</div><div class="fact-value num small-value" lang="en">{setupDeploy.workspace || "—"}</div></div>
      <div class="fact"><div class="fact-label">{t("setup.run.prepared")}</div><div class="fact-value">{setupDeploy.prepared ? t("setup.run.isPrepared") : t("setup.run.notPrepared")}</div></div>
      <div class="fact"><div class="fact-label">{t("setup.run.release")}</div><div class="fact-value num" lang="en">{setup.build.tag || "—"}</div></div>
      <div class="fact"><div class="fact-label">Docker</div><div class="fact-value">{docker ? (DOCKER_STATES.includes(docker.state) ? t(`setup.docker.${docker.state}`) : docker.state || "—") : t("setup.deploy.checking")}</div></div>
    </div>
    <p class="status-line" role="status" data-testid="setup-start-status">{#if message.raw}<span lang="en">{message.raw}</span>{:else}{t(`setup.run.status.${message.key}`)}{/if}</p>
    {#if start.error}<p class="form-error" lang="en">{start.error}</p>{/if}
    {#if start.errorDetail}<details><summary>{t("setup.config.technical")}</summary><pre class="code" lang="en">{start.errorDetail}</pre></details>{/if}
    {#if steps.length}<StepList {steps} label={t("setup.run.progress")} />{/if}

    {#if start.errorCode === "workspace_permission_denied"}
      <div class="note warn" role="alert" data-testid="setup-permission">
        <Icon name="triangle-alert" />
        <span>{t("setup.run.permission", { identity: identity ? `${identity.puid} / ${identity.pgid}` : "—" })}</span>
        <button class="btn small" type="button" onclick={repairPermissions} disabled={start.repairing}>{start.repairing ? t("setup.run.repairing") : t("setup.run.repair")}</button>
      </div>
    {/if}

    {#if conflict}
      <div class="inner conflict" role="alert" data-testid="setup-container-conflict">
        <strong>{replace ? t("setup.run.conflict.replaceTitle", { service }) : conflict.status === "running" ? t("setup.run.conflict.runningTitle", { service }) : t(`setup.run.conflict.existing.${conflictService(conflict)}`)}</strong>
        <p class="muted small">{replace ? t("setup.run.conflict.replace", { service }) : safe ? t("setup.run.conflict.stopped", { service }) : conflict.status === "running" ? t("setup.run.conflict.running", { service }) : t("setup.run.conflict.other")}</p>
        <div class="facts two">
          <div class="fact"><div class="fact-label">{t("setup.run.conflict.name")}</div><div class="fact-value num" lang="en">{conflict.container_name || "—"}</div></div>
          <div class="fact"><div class="fact-label">{t("setup.run.conflict.image")}</div><div class="fact-value num small-value" lang="en">{conflict.image || "—"}</div></div>
          <div class="fact"><div class="fact-label">{t("setup.run.conflict.selected")}</div><div class="fact-value num small-value" lang="en">{conflict.selected_image || "—"}</div></div>
        </div>
        {#if safe || replace}<button class="btn small" type="button" onclick={resolve} disabled={start.resolving} data-testid="setup-conflict-resolve">{start.resolving ? (replace ? t("setup.run.conflict.replacing", { service }) : t("setup.run.conflict.removing")) : replace ? t("setup.run.conflict.replaceAction", { service }) : t("setup.run.conflict.removeAction")}</button>{/if}
      </div>
    {/if}

    <div class="row-wrap">
      <button class="btn" type="button" onclick={startEms} disabled={blocked} data-testid="setup-start-ems"><Icon name={start.status === "running" ? "loader" : "play"} size="sm" />{start.status === "running" ? t("setup.run.starting") : start.running ? t("setup.run.restart") : t("setup.run.start")}</button>
      <button class="btn secondary small" type="button" onclick={loadStatus} disabled={start.status === "running"}>{t("setup.run.recheck")}</button>
      {#if start.running}<Status tone="c-ok" icon="circle-check" text={t("setup.run.running")} />{/if}
    </div>

    {#if start.running && !conflict}
      <div class="lead inner" data-testid="setup-start-success">
        <span class="icon-tile sm c-ok"><Icon name="circle-check" /></span>
        <p>{t("setup.run.success")}</p>
        {#if href}<a class="btn small" href={href} target="_blank" rel="noopener noreferrer"><Icon name="layout-dashboard" size="sm" />{t("setup.run.openDashboard")}</a>{/if}
      </div>
    {/if}
  </div>
</Card>

<Card icon="server" tone="c-inv" title={t("setup.run.services")} span="span-4">
  {#if services.length}
    <ul class="services" data-testid="setup-services">
      {#each services as entry, index (index)}
        <li><strong>{serviceKey(entry.service) ? t(`setup.service.${serviceKey(entry.service)}`) : entry.service || "—"}</strong><span class="num small" lang="en">{entry.image || "—"}</span>{#if entry.state || entry.status}<span class="badge" lang="en">{entry.state || entry.status}</span>{/if}</li>
      {/each}
    </ul>
  {:else}
    <p class="muted small">{t("setup.run.noServices")}</p>
  {/if}
</Card>

<ConfirmDialog
  open={replacing}
  title={t("setup.run.conflict.replaceTitle", { service })}
  lines={[t("setup.run.conflict.confirm", { name: conflict?.container_name || "" })]}
  confirmLabel={t("setup.run.conflict.replaceAction", { service })}
  danger
  onconfirm={() => { replacing = false; resolveConflict(); }}
  oncancel={() => (replacing = false)}
/>

<style>
  .stack { display: flex; flex-direction: column; gap: var(--s3); min-width: 0; }
  .stack p { margin: 0; }
  .small { font-size: 12.5px; overflow-wrap: anywhere; }
  .small-value { font-size: 12.5px; overflow-wrap: anywhere; white-space: normal; }
  .status-line { font-weight: 560; }
  .row-wrap { gap: var(--s2); align-items: center; }
  .conflict { display: flex; flex-direction: column; gap: var(--s2); }
  .services { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--s2); }
  .services li { display: flex; flex-direction: column; gap: 2px; }
  .lead { display: flex; align-items: center; gap: var(--s3); flex-wrap: wrap; }
  .lead p { margin: 0; flex: 1 1 220px; }
  .code { margin: 0; max-height: 240px; overflow: auto; font-size: 12px; background: var(--m-surface-2); border-radius: var(--m-radius-control); padding: var(--s3); }
  .note { flex-wrap: wrap; }
</style>
