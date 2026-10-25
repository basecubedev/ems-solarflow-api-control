<script>
  import Card from "../../../../shared/ui/Card.svelte";
  import Icon from "../../../../shared/ui/Icon.svelte";
  import Status from "../../../../shared/ui/Status.svelte";
  import StepList from "../StepList.svelte";
  import { t } from "../../i18n/i18n.svelte.js";
  import { DOCKER_MODES, DOCKER_STATES, deploymentMessage, jobSteps } from "../../lib/setup.js";
  import { setup } from "../../state/setup.svelte.js";
  import { setupDeploy, loadConfigStatus, loadPlan, prepare } from "../../state/setupDeploy.svelte.js";

  const docker = $derived(setupDeploy.docker);
  const message = $derived(deploymentMessage(setupDeploy, { tag: setup.build.tag, generatedReady: setup.generatedReady }));
  const steps = $derived(jobSteps(setupDeploy.steps));

  $effect(() => {
    loadConfigStatus().then(() => loadPlan({ prepare: true }));
  });

  function imageState(image) {
    if (!image.status || image.status === "pending") return "";
    if (image.status === "done") return t("setup.deploy.imageDone");
    if (image.status === "downloading") return image.percent != null ? `${image.percent}%` : t("setup.deploy.downloading");
    return String(image.status);
  }
</script>

<Card icon="package-plus" tone="c-out" title={t("setup.deploy.title")} subtitle={t("setup.deploy.sub")} span="span-8">
  <div class="stack" data-testid="setup-deployment">
    <div class="facts two">
      <div class="fact"><div class="fact-label">{t("setup.deploy.config")}</div><div class="fact-value">{setup.generatedReady ? t("setup.deploy.saved") : t("setup.deploy.notSaved")}</div></div>
      <div class="fact"><div class="fact-label">{t("setup.deploy.path")}</div><div class="fact-value num small-value" lang="en">{setupDeploy.generatedPath}</div></div>
      <div class="fact"><div class="fact-label">{t("setup.deploy.workspace")}</div><div class="fact-value num small-value" lang="en">{setupDeploy.workspace || "—"}</div></div>
      <div class="fact"><div class="fact-label">{t("setup.deploy.bootstrap")}</div><div class="fact-value">{setupDeploy.bootstrap ? t("setup.deploy.fromRelease", { tag: setup.build.tag || "—" }) : "—"}</div></div>
    </div>

    <p class="status-line" role="status" data-testid="setup-deploy-status">{#if message.raw}<span lang="en">{message.raw}</span>{:else}{t(`setup.deploy.status.${message.key}`)}{/if}</p>
    {#if setupDeploy.error}<p class="form-error" lang="en">{setupDeploy.error}</p>{/if}
    {#if setupDeploy.errorDetail}<details><summary>{t("setup.config.technical")}</summary><pre class="code" lang="en">{setupDeploy.errorDetail}</pre></details>{/if}
    {#if steps.length}<StepList {steps} label={t("setup.deploy.progress")} />{/if}

    {#if setupDeploy.existing}
      <div class="note warn" role="alert" data-testid="setup-existing-install">
        <Icon name="triangle-alert" />
        <span>{t("setup.deploy.existing")}</span>
        <button class="btn small" type="button" onclick={() => prepare(true)}>{t("setup.deploy.replaceFiles")}</button>
      </div>
    {/if}
    {#if setupDeploy.workspaceConflict}
      <div class="note warn" role="alert" data-testid="setup-workspace-conflict">
        <Icon name="triangle-alert" />
        <span>{t("setup.deploy.workspaceConflict")}</span>
        <button class="btn small" type="button" onclick={() => prepare(true)}>{t("setup.deploy.replaceWorkspace")}</button>
      </div>
    {/if}
    <div class="row-wrap">
      {#if setupDeploy.status === "failed"}<button class="btn small" type="button" onclick={() => prepare(false)} disabled={!setupDeploy.plan?.can_prepare || !setupDeploy.dockerReady} data-testid="setup-prepare-retry"><Icon name="rotate-cw" size="sm" />{t("setup.deploy.retry")}</button>{/if}
      {#if setupDeploy.ready}<Status tone="c-ok" icon="circle-check" text={t("setup.deploy.readySummary")} />{/if}
    </div>
  </div>
</Card>

<Card icon="boxes" tone="c-inv" title={t("setup.deploy.docker")} span="span-4">
  {#snippet actions()}
    <button class="icon-btn" type="button" onclick={() => loadPlan({ prepare: true })} aria-label={t("setup.deploy.recheck")} title={t("setup.deploy.recheck")}><Icon name="refresh-cw" /></button>
  {/snippet}
  <div class="stack" data-testid="setup-docker">
    {#if docker && DOCKER_STATES.includes(docker.state)}<Status tone={docker.state === "ready" ? "c-ok" : "c-warn"} text={t(`setup.docker.${docker.state}`)} />{/if}
    <div class="facts two">
      <div class="fact"><div class="fact-label">{t("setup.deploy.mode")}</div><div class="fact-value">{docker ? (DOCKER_MODES.includes(docker.mode) ? t(`setup.dockerMode.${docker.mode}`) : docker.mode || "—") : t("setup.deploy.checking")}</div></div>
      <div class="fact"><div class="fact-label">{t("setup.deploy.version")}</div><div class="fact-value num" lang="en">{docker?.server_version || "—"}</div></div>
    </div>
    {#if docker?.message}<p class="muted small" lang="en">{docker.message}</p>{/if}
  </div>
</Card>

<Card icon="download" tone="c-out" title={t("setup.deploy.images")} span="span-12">
  {#if setupDeploy.images.length}
    <ul class="images" data-testid="setup-images">
      {#each setupDeploy.images as image, index (index)}
        <li class="row-wrap"><strong>{image.service === "influxdb" ? "InfluxDB" : "EMS"}</strong><span class="num small" lang="en">{image.image}</span>{#if imageState(image)}<span class="badge num">{imageState(image)}</span>{/if}</li>
      {/each}
    </ul>
    {#if setupDeploy.plan?.influxdb && !setupDeploy.plan.influxdb.bundled}<p class="muted small">{t("setup.deploy.influxNote")}</p>{/if}
  {:else}
    <p class="muted small">{t("setup.deploy.noImages")}</p>
  {/if}
</Card>

<style>
  .stack { display: flex; flex-direction: column; gap: var(--s3); min-width: 0; }
  .stack p { margin: 0; }
  .small { font-size: 12.5px; overflow-wrap: anywhere; }
  .small-value { font-size: 12.5px; overflow-wrap: anywhere; white-space: normal; }
  .status-line { font-weight: 560; }
  .row-wrap { gap: var(--s2); align-items: center; }
  .images { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--s2); }
  .code { margin: 0; max-height: 240px; overflow: auto; font-size: 12px; background: var(--m-surface-2); border-radius: var(--m-radius-control); padding: var(--s3); }
  .note { flex-wrap: wrap; }
</style>
