<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import Empty from "../../../shared/ui/Empty.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import ControlState from "../widgets/ControlState.svelte";
  import StaleNote from "../widgets/StaleNote.svelte";
  import { t, tn, fmt } from "../i18n/i18n.svelte.js";
  import { system, ensure, reload } from "../state/system.svelte.js";
  import { AUTOMATIC_FEATURES, FEATURE_IDS, deviceView, featureViews, gridMeterName } from "../lib/settings.js";
  import { errorView } from "../lib/errors.js";
  import { classicPage } from "../state/prefs.svelte.js";

  ensure("config");

  const f = $derived(fmt());
  const config = $derived(system.config.data);
  const ok = $derived(config?.status === "ok");
  const devices = $derived(ok ? (config.draft?.devices || []).map((item) => deviceView(item, config.overrides)) : []);
  const meter = $derived(ok ? config.draft?.grid_meter || null : null);
  const features = $derived(ok ? featureViews(config) : []);

  function edit(tab) {
    return classicPage(`maintenance-settings-${tab}`);
  }
</script>

<div class="lead span-12 inner" role="note">
  <span class="icon-tile sm c-acc"><Icon name="pencil" /></span>
  <p>{t("settings.editLead")}</p>
  <a class="btn small" href={classicPage("maintenance-settings")} data-testid="edit-settings"><Icon name="arrow-up-right" size="sm" />{t("settings.openEditor")}</a>
</div>

{#if config && system.config.error}<div class="span-12"><StaleNote resource={system.config} /></div>{/if}
{#if !config && system.config.loading}
  <div class="span-12"><Empty text={t("common.loading")} icon="loader" /></div>
{:else if system.config.error && !config}
  <div class="span-12"><Empty error {...errorView(system.config.error, t)} /></div>
{:else if config && !ok}
  <div class="span-12">
    <Empty error={config.status !== "missing"} icon="file-warning" text={t(`settings.config.${config.status === "missing" ? "missing" : "invalid"}`)}>
      {#if config.message}<span class="muted" lang="en"> {config.message}</span>{/if}
    </Empty>
  </div>
{:else if ok}
  <Card title={t("settings.devices")} subtitle={tn("settings.devicesSub", devices.length)} span="span-8">
    {#snippet actions()}
      <button class="icon-btn" type="button" onclick={() => reload("config")} disabled={system.config.loading} aria-label={t("common.refresh")} title={t("common.refresh")}><Icon name="refresh-cw" /></button>
      <a class="btn secondary small" href={edit("devices")}><Icon name="arrow-up-right" size="sm" />{t("settings.editDevices")}</a>
    {/snippet}
    {#if devices.length}
      <ul class="devices" data-testid="devices">
        {#each devices as device, index (`${device.name}-${index}`)}
          <li class="device inner" class:off={!device.enabled}>
            <span class="icon-tile sm {device.readOnly ? 'c-idle' : 'c-out'}"><Icon name={device.readOnly ? "eye" : "cpu"} /></span>
            <div class="words">
              <div class="name ellipsis">{device.name || t("settings.unnamed")}</div>
              <div class="muted small num">
                {[device.connection ? t(device.connection) : device.connectionText, device.model, device.address, device.maxPower !== null ? t("settings.maxPower", { power: f.power(device.maxPower) }) : ""].filter(Boolean).join(" · ")}
              </div>
            </div>
            {#if device.overridden}<span class="badge" title={t("settings.overriddenHint")}>{t("settings.overridden")}</span>{/if}
            <Status tone={device.enabled ? "c-ok" : "c-idle"} text={device.enabled ? t("settings.inUse") : t("settings.notInUse")} />
          </li>
        {/each}
      </ul>
    {:else}
      <Empty text={t("settings.noDevices")} icon="cpu" />
    {/if}
    <h3 class="sub">{t("settings.gridMeter")}</h3>
    {#if meter?.present !== false && meter?.type}
      <div class="device inner">
        <span class="icon-tile sm c-grid"><Icon name="gauge" /></span>
        <div class="words">
          <div class="name" lang="en">{gridMeterName(meter.type, config?.catalog?.grid_meter_variants)}</div>
          <div class="muted small num">{meter.ip || meter.host || ""}</div>
        </div>
      </div>
    {:else}
      <p class="muted">{t("settings.noGridMeter")}</p>
    {/if}
  </Card>

  <Card title={t("control.title")} subtitle={t("control.sub")} span="span-4">
    {#snippet actions()}<a class="btn secondary small" href={edit("safety")}><Icon name="arrow-up-right" size="sm" />{t("settings.editSafety")}</a>{/snippet}
    <ControlState control={config.summary?.control} overrides={config.overrides} />
  </Card>

  <Card title={t("settings.features")} subtitle={t("settings.featuresSub")} span="span-12">
    {#snippet actions()}
      <a class="btn secondary small" href={edit("features")}><Icon name="arrow-up-right" size="sm" />{t("settings.editFeatures")}</a>
      <a class="btn secondary small" href={edit("expert")}><Icon name="arrow-up-right" size="sm" />{t("settings.editExpert")}</a>
    {/snippet}
    <ul class="features" data-testid="features">
      {#each features as feature (feature.id)}
        <li class="inner">
          <div class="words">
            <div class="name">{FEATURE_IDS.includes(feature.id) ? t(`feature.${feature.id}`) : feature.title}</div>
            {#if FEATURE_IDS.includes(feature.id)}<div class="muted small">{t(`feature.${feature.id}.sub`)}</div>{:else if feature.summary}<div class="muted small" lang="en">{feature.summary}</div>{/if}
          </div>
          {#if feature.enabled !== null}
            <Status tone={feature.enabled ? "c-ok" : "c-idle"} text={feature.enabled ? t("common.on") : t("common.off")} />
          {:else if AUTOMATIC_FEATURES.includes(feature.id)}
            <span title={t("settings.featureAutoHint")}><Status tone="c-acc" text={t("settings.featureAuto")} /></span>
          {/if}
          {#if feature.overridden}<span class="badge" title={t("settings.overriddenHint")}>{t("settings.overridden")}</span>{/if}
        </li>
      {/each}
    </ul>
  </Card>
{/if}

<style>
  .lead { display: flex; align-items: center; gap: var(--s3); flex-wrap: wrap; border: 1px solid var(--m-line); }
  .lead p { margin: 0; flex: 1 1 260px; color: var(--m-muted); font-size: 13.5px; }
  .devices, .features { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--s2); }
  .device { display: flex; align-items: center; gap: var(--s3); }
  .device.off { opacity: 0.7; }
  .words { flex: 1; min-width: 0; }
  .name { font-weight: 600; }
  .small { font-size: 12.5px; overflow-wrap: anywhere; }
  .sub { font-size: 13px; font-weight: 620; margin: var(--s4) 0 var(--s2); }
  .features { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .features li { display: flex; align-items: center; gap: var(--s3); }
  @container content (max-width: 720px) { .features { grid-template-columns: minmax(0, 1fr); } }
</style>
