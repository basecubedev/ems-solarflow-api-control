<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import Empty from "../../../shared/ui/Empty.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import ConfirmDialog from "../../../shared/ui/ConfirmDialog.svelte";
  import WorkflowRecovery from "../widgets/WorkflowRecovery.svelte";
  import StaleNote from "../widgets/StaleNote.svelte";
  import { t, tn, fmt } from "../i18n/i18n.svelte.js";
  import { system, ensure, reload } from "../state/system.svelte.js";
  import { HEALTHY_INSTALL_STATES, INSTALL_STATES } from "../lib/health.js";
  import { RUNTIME_STATES, STATUS_SOURCES, brokerView, telemetryDeviceView, telemetryWanted } from "../lib/telemetry.js";
  import { migrationReview } from "../lib/migration.js";
  import { errorView } from "../lib/errors.js";
  import { href } from "../app/router.svelte.js";
  import { adminSend } from "../state/auth.svelte.js";
  import { migration as migrating, applyMigration } from "../state/migration.svelte.js";

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
  const outcome = $derived(migrating.outcome && migrating.outcome.revision === (migration?.revision ?? null) ? migrating.outcome : null);
  const migrationWanted = $derived(!migration || migration.status !== "ok" || migration.review?.needs_migration === true || Boolean(outcome && !outcome.conflict));
  const review = $derived(migrationReview(migration));
  let backup = $state(true);
  let confirmMigration = $state(false);
  let moving = $state.raw(null);
  let legacy = $state.raw({ busy: false, result: null, error: null, conflict: false });

  /** Copies the old root config.json to config/config.json, a backup first; the old file stays. */
  async function moveLegacy(overwrite) {
    moving = null;
    legacy = { busy: true, result: null, error: null, conflict: false };
    try {
      const result = await adminSend("/api/admin/config/migrate-legacy", { body: overwrite ? { overwrite: true } : {} });
      legacy = { busy: false, result, error: null, conflict: false };
    } catch (error) {
      legacy = { busy: false, result: null, error, conflict: error?.payload?.reason === "target_exists" };
    }
    reload("overview");
  }
  let reviewed = $state.raw(null);

  function askMigration() {
    reviewed = review;
    confirmMigration = true;
  }

  function migrate() {
    confirmMigration = false;
    if (reviewed?.canApply) applyMigration({ revision: reviewed.revision, backup });
    reviewed = null;
  }

  function installState() {
    return INSTALL_STATES.includes(install.state) ? install.state : "partial_install";
  }
</script>

<Card icon="hard-drive" tone="c-inv" title={t("system.files")} subtitle={t("system.filesSub")} span="span-6">
  {#snippet actions()}
    <button class="icon-btn" type="button" onclick={() => reload("overview", "telemetry", "migration")} aria-label={t("common.refresh")} title={t("common.refresh")} disabled={system.overview.loading}><Icon name="refresh-cw" /></button>
  {/snippet}
  <StaleNote resource={system.overview} />
  {#if overview}
    <p class="state">
      <Status tone={HEALTHY_INSTALL_STATES.includes(install.state) ? "c-ok" : "c-warn"} text={t(`install.state.${installState()}`)} />
      <span class="muted">{t(`install.message.${installState()}`)}</span>
    </p>
    {#if install.legacy_migration_available === true || legacy.result || legacy.error}
      <div class="legacy inner" data-testid="legacy-config">
        {#if install.legacy_migration_available === true || legacy.conflict}<p class="muted small">{t("system.legacyLead", { from: install.paths?.legacy_config || "config.json", to: install.paths?.standard_config || "config/config.json" })}</p>{/if}
        {#if legacy.result}
          <p class="note" role="status"><Icon name="circle-check" />{legacy.result.migrated === false ? t("system.legacyAlready") : legacy.result.backups?.length > 1 ? t("system.legacyReplaced", { paths: legacy.result.backups.join(", ") }) : t("system.legacyMoved", { path: legacy.result.backup_path || "—" })}</p>
        {:else if legacy.error}
          {@const view = errorView(legacy.error, t)}
          <p class="form-error" role="alert"><span lang={view.lang}>{view.text}</span></p>
        {/if}
        {#if install.legacy_migration_available === true || legacy.conflict}
          <div class="row-wrap">
            {#if legacy.conflict}
              <button class="btn secondary small" type="button" onclick={() => (moving = { overwrite: true })} disabled={legacy.busy} data-testid="legacy-replace">{t("system.legacyReplace")}</button>
            {:else}
              <button class="btn small" type="button" onclick={() => (moving = { overwrite: false })} disabled={legacy.busy} data-testid="legacy-move"><Icon name={legacy.busy ? "loader" : "file-check"} size="sm" />{t("system.legacyMove")}</button>
            {/if}
          </div>
        {/if}
      </div>
    {/if}
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

<Card icon="radio-tower" tone="c-soc" title={t("system.telemetry")} subtitle={t("system.telemetrySub")} span="span-6">
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
  <Card icon="file-cog" tone="c-pv" title={t("system.migration")} subtitle={t("system.migrationSub")} span="span-12">
    {#if migration.status !== "ok"}
      <Empty error text={t("system.migrationUnreadable")}>{#if migration.message}<span class="muted" lang="en"> {migration.message}</span>{/if}</Empty>
    {:else}
      {#if review.required}
        <p class="summary"><Icon name="triangle-alert" />{tn("system.migrationNeeded", review.devices.length)}</p>
        {#if review.losing}<p class="note warn"><Icon name="shield-alert" />{tn("system.migrationLosesControl", review.losing)}</p>{/if}
        {#if review.devices.length}
          <ul class="rows" data-testid="migration-devices">
            {#each review.devices as device, index (`${device.name}-${index}`)}
              <li class="inner">
                <div class="words">
                  <strong>{device.name || t("system.migrationDevice")}</strong>
                  <span class="muted small">{device.model ? t("system.migrationModel", { model: device.model }) : t("system.migrationModelUnknown")}</span>
                  {#if device.message}<span class="muted small" lang="en">{device.message}</span>{/if}
                </div>
                <Status tone={device.disables ? "c-warn" : "c-ok"} text={device.disables ? t("system.migrationControlLost") : t("system.migrationControlKept")} />
              </li>
            {/each}
          </ul>
        {/if}
        <p class="state"><Status tone={review.valid ? "c-ok" : "c-fault"} text={review.valid ? t("system.migrationValid") : t("system.migrationInvalid")} /></p>
        {#each review.errors as error, index (index)}<p class="note warn"><Icon name="triangle-alert" /><span lang="en">{error}</span></p>{/each}
        <div class="row-wrap migrate">
          <label class="check"><input type="checkbox" bind:checked={backup} data-testid="migration-backup" />{t("settings.edit.backup")}</label>
          <button class="btn" type="button" onclick={askMigration} disabled={!review.canApply || migrating.busy} data-testid="migration-apply"><Icon name="play" size="sm" />{migrating.busy ? t("system.migrationApplying") : t("system.migrationApply")}</button>
        </div>
      {/if}
      {#if outcome?.error && !(outcome.conflict && !review.required)}
        {@const view = errorView(outcome.error, t)}
        <p class="note warn" data-testid="migration-result"><Icon name="circle-x" /><span lang={outcome.conflict ? undefined : view.lang}>{outcome.conflict ? t("system.migrationStale") : view.text}</span></p>
      {:else if outcome && !outcome.error && !review.required}
        <p class="note" data-testid="migration-result"><Icon name="circle-check" />{outcome.result?.changed === false ? t("system.migrationNoop") : t("system.migrationDone")}</p>
      {/if}
    {/if}
  </Card>
{:else if system.migration.error}
  <Card icon="file-cog" tone="c-pv" title={t("system.migration")} span="span-12"><Empty error {...errorView(system.migration.error, t)} /></Card>
{/if}

<WorkflowRecovery />

<Card icon="package-plus" tone="c-out" title={t("system.setup")} subtitle={t("system.setupSub")} span="span-12">
  <div class="row-wrap setup-entry" data-testid="system-setup">
    <p class="muted">{t("system.setupLead")}</p>
    <a class="btn secondary small" href={href("setup")}><Icon name="package-plus" size="sm" />{t("system.setupOpen")}</a>
  </div>
</Card>

<ConfirmDialog
  open={Boolean(moving)}
  title={moving?.overwrite ? t("system.legacyReplaceTitle") : t("system.legacyMoveTitle")}
  lines={moving?.overwrite ? [t("system.legacyReplaceLine")] : [t("system.legacyMoveLine")]}
  confirmLabel={moving?.overwrite ? t("system.legacyReplace") : t("system.legacyMove")}
  danger={Boolean(moving?.overwrite)}
  onconfirm={() => moveLegacy(Boolean(moving?.overwrite))}
  oncancel={() => (moving = null)}
/>
<ConfirmDialog
  open={confirmMigration}
  title={t("system.migrationConfirmTitle")}
  lines={[
    tn("system.migrationConfirmDevices", reviewed?.devices.length || 0),
    ...(reviewed?.losing ? [tn("system.migrationLosesControl", reviewed.losing)] : []),
    backup ? t("settings.edit.confirmBackup") : t("settings.edit.confirmNoBackup"),
  ]}
  confirmLabel={t("system.migrationApply")}
  danger={!backup || Boolean(reviewed?.losing)}
  onconfirm={migrate}
  oncancel={() => { confirmMigration = false; reviewed = null; }}
/>

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
  .migrate { margin-top: var(--s3); justify-content: space-between; align-items: center; }
  .setup-entry p { margin: 0; flex: 1 1 280px; }
  .legacy { display: flex; flex-direction: column; gap: var(--s2); margin-bottom: var(--s3); }
  .legacy p { margin: 0; }
</style>
