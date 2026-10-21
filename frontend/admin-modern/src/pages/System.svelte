<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import Empty from "../../../shared/ui/Empty.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import WorkflowRecovery from "../widgets/WorkflowRecovery.svelte";
  import StaleNote from "../widgets/StaleNote.svelte";
  import { t, tn, fmt } from "../i18n/i18n.svelte.js";
  import { system, ensure, reload } from "../state/system.svelte.js";
  import { HEALTHY_INSTALL_STATES, INSTALL_STATES } from "../lib/health.js";
  import { RUNTIME_STATES, STATUS_SOURCES, brokerView, telemetryDeviceView, telemetryWanted } from "../lib/telemetry.js";
  import { errorView } from "../lib/errors.js";
  import { classicPage } from "../state/prefs.svelte.js";

  ensure("overview", "telemetry", "migration");

  const PATHS = ["config", "data", "compose"];

  const f = $derived(fmt());
  const overview = $derived(system.overview.data);
  const install = $derived(overview?.install_state || {});
  const telemetry = $derived(system.telemetry.data);
  const migration = $derived(system.migration.data);
  const runtimeState = $derived(Object.hasOwn(RUNTIME_STATES, telemetry?.runtime_state) ? telemetry.runtime_state : "unavailable");
  const brokers = $derived((Array.isArray(telemetry?.brokers) ? telemetry.brokers : []).map(brokerView));
  const devices = $derived((Array.isArray(telemetry?.devices) ? telemetry.devices : []).map(telemetryDeviceView));
  const migrationWanted = $derived(!migration || migration.status !== "ok" || migration.review?.needs_migration === true);

  function installState() {
    return INSTALL_STATES.includes(install.state) ? install.state : "partial_install";
  }
</script>

<Card title={t("system.files")} subtitle={t("system.filesSub")} span="span-6">
  {#snippet actions()}
    <button class="icon-btn" type="button" onclick={() => reload("overview", "telemetry", "migration")} aria-label={t("common.refresh")} title={t("common.refresh")} disabled={system.overview.loading}><Icon name="refresh-cw" /></button>
  {/snippet}
  <StaleNote resource={system.overview} />
  {#if overview}
    <p class="state">
      <Status tone={HEALTHY_INSTALL_STATES.includes(install.state) ? "c-ok" : "c-warn"} text={t(`install.state.${installState()}`)} />
      <span class="muted">{t(`install.message.${installState()}`)}</span>
    </p>
    <ul class="paths">
      {#each PATHS as name (name)}
        {@const entry = overview.paths?.[name] || {}}
        <li class="inner">
          <span class="mark" class:missing={!entry.exists}><Icon name={entry.exists ? "circle-check" : "circle-x"} size="sm" /></span>
          <div class="words"><strong>{t(`health.path.${name}`)}</strong><span class="muted path">{entry.path || "—"}</span></div>
          <span class="muted small">{entry.exists ? t("system.found") : t("system.missing")}</span>
        </li>
      {/each}
    </ul>
    {#if overview.paths?.config?.modified_at}<p class="note"><Icon name="clock" />{t("system.configSaved", { when: f.relative(overview.paths.config.modified_at) })}</p>{/if}
  {:else if system.overview.error}
    <Empty error {...errorView(system.overview.error, t)} />
  {:else}
    <Empty text={t("common.loading")} icon="loader" />
  {/if}
</Card>

<Card title={t("system.telemetry")} subtitle={t("system.telemetrySub")} span="span-6">
  <StaleNote resource={system.telemetry} />
  {#if telemetry && !telemetryWanted(telemetry)}
    <Empty text={t("system.telemetryNone")} icon="radio-tower" />
  {:else if telemetry && telemetry.available === false}
    <Empty text={t("system.telemetryUnknown")} icon="circle-help">{#if telemetry.message}<span class="muted" lang="en"> {telemetry.message}</span>{/if}</Empty>
  {:else if telemetry}
    <div class="facts two">
      <div class="fact"><div class="fact-label">{t("system.connection")}</div><div class="fact-value">{t(`system.runtime.${runtimeState}`)}</div></div>
      <div class="fact"><div class="fact-label">{t("system.devices")}</div><div class="fact-value num">{f.number(telemetry.configured_device_count || 0)}</div></div>
      <div class="fact"><div class="fact-label">{t("system.devicesProblem")}</div><div class="fact-value num">{f.number(telemetry.invalid_device_count || 0)}</div></div>
      <div class="fact"><div class="fact-label">{t("system.offlineAfter")}</div><div class="fact-value num" class:na={telemetry.stale_after_seconds == null}>{telemetry.stale_after_seconds != null ? `${f.number(telemetry.stale_after_seconds)} s` : "—"}</div></div>
    </div>
    {#if STATUS_SOURCES.includes(telemetry.source)}<p class="note"><Icon name="info" />{t(`system.source.${telemetry.source}`)}</p>{/if}
    {#if brokers.length}
      <h3 class="sub">{t("system.brokers")}</h3>
      <ul class="rows">
        {#each brokers as broker (broker.ref)}
          <li class="inner">
            <div class="words"><strong>{broker.ref}</strong><span class="muted small num">{[broker.endpoint, tn("system.brokerDevices", broker.devices)].filter(Boolean).join(" · ")}</span>
              {#if broker.issue}<span class="issue"><Icon name="triangle-alert" size="sm" />{t(`system.issue.${broker.issue}`)}</span>{/if}
              {#if broker.lastError}<span class="muted small" lang="en">{broker.lastError}</span>{/if}
            </div>
            <Status tone={broker.tone} text={t(`system.broker.${broker.state}`)} />
          </li>
        {/each}
      </ul>
    {/if}
    {#if devices.length}
      <h3 class="sub">{t("system.telemetryDevices")}</h3>
      <ul class="rows">
        {#each devices as device, index (`${device.name}-${index}`)}
          <li class="inner">
            <div class="words"><strong>{device.name || "—"}</strong><span class="muted small num">{[device.broker, device.lastSeen ? t("system.seenAt", { time: f.when(device.lastSeen, { seconds: true }) }) : "", tn("system.metrics", device.metrics)].filter(Boolean).join(" · ")}</span></div>
            <Status tone={device.tone} text={device.known ? t(`system.deviceState.${device.status}`) : device.status} />
          </li>
        {/each}
      </ul>
    {/if}
    <p class="note"><Icon name="eye" />{t("system.telemetryReadOnly")}</p>
  {:else if system.telemetry.error}
    <Empty error {...errorView(system.telemetry.error, t)} />
  {:else}
    <Empty text={t("common.loading")} icon="loader" />
  {/if}
</Card>

{#if migration && migrationWanted}
  <Card title={t("system.migration")} subtitle={t("system.migrationSub")} span="span-12">
    {#if migration.status !== "ok"}
      <Empty error text={t("system.migrationUnreadable")}>{#if migration.message}<span class="muted" lang="en"> {migration.message}</span>{/if}</Empty>
    {:else}
      <p class="summary"><Icon name="triangle-alert" />{tn("system.migrationNeeded", (migration.review?.changes || []).length)}</p>
      {#if (migration.review?.warnings_disabling_control || []).length}<p class="note warn"><Icon name="shield-alert" />{tn("system.migrationLosesControl", migration.review.warnings_disabling_control.length)}</p>{/if}
      <a class="btn" href={classicPage("maintenance-status")}><Icon name="arrow-up-right" size="sm" />{t("system.migrationOpen")}</a>
    {/if}
  </Card>
{:else if system.migration.error}
  <Card title={t("system.migration")} span="span-12"><Empty error {...errorView(system.migration.error, t)} /></Card>
{/if}

<WorkflowRecovery />

<style>
  .state { display: flex; align-items: center; gap: var(--s2); flex-wrap: wrap; margin: 0 0 var(--s3); font-size: 13px; }
  .paths, .rows { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--s2); }
  .paths li { display: flex; align-items: center; gap: var(--s3); }
  .mark { display: inline-flex; color: var(--m-ok); }
  .mark.missing { color: var(--m-fault); }
  .rows li { display: flex; align-items: center; gap: var(--s3); }
  .words { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
  .path { font-family: var(--m-mono); font-size: 12px; overflow-wrap: anywhere; }
  .small { font-size: 12.5px; }
  .sub { font-size: 13px; font-weight: 620; margin: var(--s4) 0 var(--s2); }
  .issue { display: flex; gap: 6px; align-items: center; color: var(--m-warn); font-size: 12.5px; }
  .summary { display: flex; gap: 8px; align-items: center; margin: 0 0 var(--s3); font-weight: 560; }
  .summary :global(svg.i) { color: var(--m-warn); }
</style>
