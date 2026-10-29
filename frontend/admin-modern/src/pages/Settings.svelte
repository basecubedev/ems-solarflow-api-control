<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import Empty from "../../../shared/ui/Empty.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import Segmented from "../../../shared/ui/Segmented.svelte";
  import ConfirmDialog from "../../../shared/ui/ConfirmDialog.svelte";
  import ControlState from "../widgets/ControlState.svelte";
  import StaleNote from "../widgets/StaleNote.svelte";
  import SettingField from "../widgets/SettingField.svelte";
  import CatalogText from "../widgets/CatalogText.svelte";
  import SettingsSection from "../widgets/SettingsSection.svelte";
  import DevicesEditor from "../widgets/DevicesEditor.svelte";
  import DiscoveryPanel from "../widgets/DiscoveryPanel.svelte";
  import SourcesPanel from "../widgets/SourcesPanel.svelte";
  import { t, tn, catalogText } from "../i18n/i18n.svelte.js";
  import { system, ensure, reload } from "../state/system.svelte.js";
  import { settings, setValue, discard, discardHardware, previewDraft, applyDraft, resetOverrides } from "../state/settings.svelte.js";
  import { serviceAction, runServiceAction } from "../state/services.svelte.js";
  import { EDIT_TABS, changeGroups, restartNeeded, fieldMatches, levelled, liveOverride, previewValue, safetyBlocks, sharedRisk, tabSections } from "../lib/settings.js";
  import { errorView } from "../lib/errors.js";
  import { href } from "../app/router.svelte.js";

  ensure("config");

  const TABS = [...EDIT_TABS, "devices"];

  /** The tab in the address, #/settings/<tab>, so another page can open one. */
  function hashTab() {
    const part = (globalThis.location?.hash || "").replace(/^#\/?/, "").split(/[?/]/)[1];
    return TABS.includes(part) ? part : "features";
  }

  let tab = $state(hashTab());
  let query = $state("");
  const shownText = (text) => catalogText(text).text;
  let backup = $state(true);
  let confirmApply = $state(false);
  let confirmReset = $state(false);
  let confirmRestart = $state(false);

  const config = $derived(system.config.data);
  const ok = $derived(config?.status === "ok");
  const devices = $derived(Array.isArray(settings.draft?.devices) ? settings.draft.devices : []);
  const values = $derived(settings.draft?.features || {});
  const searching = $derived(query.trim() !== "");

  function sectionFields(section) {
    const levels = levelled(section);
    return [...levels.normal, ...levels.advanced, ...levels.expert];
  }
  function matchingSections(name) {
    return tabSections(config, name).filter((section) => !searching || sectionFields(section).some((field) => fieldMatches(field, query, shownText)));
  }
  const blocks = $derived(ok ? safetyBlocks(config).map((block) => ({ ...block, fields: block.fields.filter((field) => fieldMatches(field, query, shownText)) })).filter((block) => block.fields.length) : []);
  const hits = $derived.by(() => {
    if (!ok || !searching) return {};
    const count = (sections) => sections.reduce((sum, section) => sum + sectionFields(section).filter((field) => fieldMatches(field, query, shownText)).length, 0);
    return { features: count(tabSections(config, "features")), expert: count(tabSections(config, "expert")), safety: blocks.reduce((sum, block) => sum + block.fields.length, 0) };
  });
  const TAB_LOOKS = { features: { icon: "toggle-right", tone: "c-out" }, safety: { icon: "shield-check", tone: "c-soc" }, expert: { icon: "wrench", tone: "c-pv" }, devices: { icon: "zap", tone: "c-inv" } };
  const tabOptions = $derived(TABS.map((value) => ({ value, ...TAB_LOOKS[value], label: searching && value !== "devices" ? `${t(`settings.tab.${value}`)} (${hits[value] || 0})` : t(`settings.tab.${value}`) })));

  const preview = $derived(settings.preview);
  const groups = $derived(changeGroups(preview?.diff));
  const notes = $derived([...(preview?.validation?.errors || []), ...(preview?.validation?.warnings || [])].map((item) => item?.message).filter(Boolean));
  const stale = $derived(Boolean(preview) && !settings.fresh);
  const unreadable = $derived(preview && preview.status !== "ok" && !preview.validation ? preview : null);
  const canApply = $derived(settings.reviewed && preview?.changed === true && !settings.applying);
  const applied = $derived(settings.applied);
  const needsRestart = $derived(restartNeeded(applied?.diff, applied?.result));
  const rollback = $derived(settings.applyError?.payload?.credential_rollback || settings.applyError?.credential_rollback || null);

  function choose(value) {
    tab = value;
    globalThis.history?.replaceState(null, "", `${href("settings")}/${value}`);
  }

  function restart() {
    confirmRestart = false;
    runServiceAction("sync");
    globalThis.location.hash = href("services");
  }

  $effect(() => {
    const follow = () => (tab = hashTab());
    globalThis.addEventListener("hashchange", follow);
    return () => globalThis.removeEventListener("hashchange", follow);
  });
</script>

{#snippet failure(error)}
  {@const view = errorView(error, t)}
  <p class="form-error" lang={view.lang}>{view.text}</p>
{/snippet}

{#snippet safetyBlock(block)}
  {@const risk = sharedRisk(block.fields)}
  <div class="block" data-group={block.id}>
    <h3 class="sub"><CatalogText text={block.title} /></h3>
    {#if block.summary}<p class="muted small"><CatalogText text={block.summary} /></p>{/if}
    {#if risk}<p class="note warn"><Icon name="triangle-alert" />{t(`settings.risk.${risk}.shared`)}</p>{/if}
    <div class="fields">
      {#each block.fields as field (field.path)}
        <SettingField {field} value={values[field.path]} live={liveOverride(config.overrides, field.path)} hideRisk={Boolean(risk)} onchange={(value) => setValue(field, value)} />
      {/each}
    </div>
  </div>
{/snippet}

{#if config && system.config.error}<div class="span-12"><StaleNote resource={system.config} /></div>{/if}
{#if !config && system.config.loading}
  <div class="span-12"><Empty text={t("common.loading")} icon="loader" /></div>
{:else if system.config.error && !config}
  <div class="span-12"><Empty error {...errorView(system.config.error, t)} /></div>
{:else if config && !ok}
  <div class="span-12">
    <Empty error={config.status !== "missing"} icon="file-warning" text={t(`settings.config.${config.status === "missing" ? "missing" : "invalid"}`)}>
      {#if config.message}<span class="muted" lang="en">{" "}{config.message}</span>{/if}
    </Empty>
  </div>
{:else if ok}
  <div class="toolbar span-12">
    <Segmented large options={tabOptions} value={tab} label={t("settings.tabs")} onchange={choose} />
    <label class="search">
      <Icon name="search" size="sm" />
      <input class="input" type="search" placeholder={t("settings.edit.search")} aria-label={t("settings.edit.search")} bind:value={query} data-testid="settings-search" />
    </label>
    <button class="icon-btn" type="button" onclick={() => reload("config")} disabled={system.config.loading} aria-label={t("common.refresh")} title={t("common.refresh")}><Icon name="refresh-cw" /></button>
  </div>

  {#if settings.overrides.length}
    <div class="note span-12 live-note" role="note">
      <Icon name="info" />
      <span>{tn("settings.edit.overrides", settings.overrides.length)}</span>
      <button class="btn secondary small" type="button" onclick={() => (confirmReset = true)} disabled={settings.resetting} data-testid="reset-overrides"><Icon name="rotate-ccw" size="sm" />{settings.resetting ? t("settings.edit.resetting") : t("settings.edit.reset")}</button>
    </div>
    {#if settings.resetError}<div class="span-12">{@render failure(settings.resetError)}</div>{/if}
  {/if}

  {#if tab === "features" || tab === "expert"}
    {@const list = matchingSections(tab)}
    <Card icon={TAB_LOOKS[tab].icon} tone={TAB_LOOKS[tab].tone} title={t(`settings.tab.${tab}`)} subtitle={t(`settings.tab.${tab}.sub`)} span="span-12">
      {#if list.length}
        <div class="sections" data-testid={`settings-${tab}`}>
          {#each list as section (section.id)}<SettingsSection {section} {query} />{/each}
        </div>
      {:else}
        <Empty text={t("settings.edit.noMatch")} icon="search" />
      {/if}
    </Card>
  {:else if tab === "safety"}
    <Card icon="toggle-right" tone="c-out" title={t("control.title")} subtitle={t("control.sub")} span="span-4">
      <ControlState control={config.summary?.control} overrides={config.overrides} />
    </Card>
    <Card icon="shield-check" tone="c-soc" title={t("settings.tab.safety")} subtitle={t("settings.tab.safety.sub")} span="span-8">
      {#if blocks.length}
        <div class="blocks" data-testid="settings-safety">
          {#each blocks as block (block.id)}{@render safetyBlock(block)}{/each}
        </div>
      {:else}
        <Empty text={searching ? t("settings.edit.noMatch") : t("settings.edit.noSafety")} icon="shield-alert" />
      {/if}
    </Card>
  {:else}
    <Card icon="search" tone="c-out" title={t("discovery.title")} subtitle={t("discovery.sub")} span="span-12">
      <DiscoveryPanel {config} />
    </Card>
    <Card icon="radio-tower" tone="c-soc" title={t("sources.title")} subtitle={t("sources.sub")} span="span-12">
      <SourcesPanel />
    </Card>
    <Card icon="zap" tone="c-inv" title={t("settings.devices")} subtitle={tn("settings.devicesSub", devices.length)} span="span-12">
      <DevicesEditor {config} />
    </Card>
  {/if}

  {#if settings.changes || preview || settings.previewError}
    <section class="changes span-12" aria-label={t("settings.edit.changesTitle")} data-testid="settings-changes">
      <div class="change-bar">
        <span class="count" data-testid="change-count">{settings.changes ? tn("settings.edit.unsaved", settings.changes) : t("settings.edit.noUnsaved")}</span>
        <button class="btn secondary small" type="button" onclick={discard} disabled={!settings.changes || settings.applying || settings.previewing} data-testid="discard"><Icon name="rotate-ccw" size="sm" />{t("settings.edit.discard")}</button>
        <button class="btn small" type="button" onclick={previewDraft} disabled={settings.previewing || settings.applying || !settings.changes} data-testid="review"><Icon name="file-check" size="sm" />{settings.previewing ? t("settings.edit.reviewing") : t("settings.edit.review")}</button>
      </div>
      {#if settings.hardwareStale}
        <p class="note warn" role="alert" data-testid="hardware-stale"><Icon name="triangle-alert" /><span>{t("devices.stale")}</span><button class="btn secondary small" type="button" onclick={discardHardware}>{t("devices.discardStale")}</button></p>
      {/if}
      {#if settings.previewError}{@render failure(settings.previewError)}{/if}
      {#if unreadable}
        {@render failure(unreadable)}
      {:else if preview}
        <div class="review" data-testid="settings-review">
          <p class="verdict">
            {#if preview.validation?.ok}<Status tone="c-ok" text={t("settings.edit.valid")} />{:else}<Status tone="c-fault" text={tn("settings.edit.invalid", (preview.validation?.errors || []).length)} />{/if}
            {#if stale}<span class="muted small">{t("settings.edit.stale")}</span>{/if}
          </p>
          {#if !groups.length}<p class="muted">{t("settings.edit.nothing")}</p>{/if}
          {#each groups as group (group.when)}
            <h3 class="sub">{t(`settings.edit.when.${group.when}`)} <span class="muted num">({group.rows.length})</span></h3>
            <ul class="diff">
              {#each group.rows as row, index (`${row.path}-${index}`)}
                <li data-kind={row.kind}><span class="path num">{row.path}</span><span class="value num">{#if row.kind === "changed"}{previewValue(row.before)} → {previewValue(row.after)}{:else if row.kind === "added"}+ {previewValue(row.after)}{:else}− {previewValue(row.before)}{/if}</span></li>
              {/each}
            </ul>
          {/each}
          {#each notes as note, index (index)}<p class="note warn"><Icon name="triangle-alert" /><span lang="en">{note}</span></p>{/each}
          {#if preview.preview}
            <details class="generated">
              <summary>{t("settings.edit.generated")}</summary>
              <pre class="num">{JSON.stringify(preview.preview, null, 2)}</pre>
            </details>
          {/if}
          {#if preview.validation?.ok && preview.changed}
            <div class="apply">
              <label class="check"><input type="checkbox" bind:checked={backup} data-testid="apply-backup" />{t("settings.edit.backup")}</label>
              <button class="btn" type="button" onclick={() => (confirmApply = true)} disabled={!canApply} data-testid="apply"><Icon name="save" size="sm" />{settings.applying ? t("settings.edit.applying") : t("settings.edit.apply")}</button>
            </div>
          {/if}
          {#if settings.applyError}{@render failure(settings.applyError)}{/if}
          {#if rollback}
            <p class="note warn" data-testid="credential-rollback"><Icon name="triangle-alert" /><span lang="en">{rollback.message}{#if rollback.failed_refs?.length}{" "}({rollback.failed_refs.join(", ")}){/if}</span></p>
          {/if}
        </div>
      {/if}
    </section>
  {/if}

  {#if applied && !settings.changes && !preview}
    <Card icon="circle-check" tone="c-ok" title={t("settings.edit.applied")} span="span-12">
      <div class="applied" data-testid="settings-applied">
        {#if applied.result?.changed === false}
          <p class="muted">{t("settings.edit.appliedNothing")}</p>
        {:else}
          <p><Icon name="circle-check" size="sm" /> {applied.result?.backup_path ? t("settings.edit.appliedBackup") : t("settings.edit.appliedNoBackup")}</p>
          {#if applied.result?.backup_path}<p class="muted small num" lang="en">{applied.result.backup_path}</p>{/if}
          {#if needsRestart}
            <p>{t("settings.edit.restartNeeded")}</p>
            <div class="row-wrap">
              <button class="btn" type="button" onclick={() => (confirmRestart = true)} disabled={Boolean(serviceAction.busy)} data-testid="restart-ems"><Icon name="rotate-cw" size="sm" />{serviceAction.busy === "sync" ? t("settings.edit.restarting") : t("settings.edit.restart")}</button>
              <a class="btn secondary small" href={href("services")}>{t("settings.edit.openServices")}</a>
            </div>
          {:else}
            <p class="muted">{t("settings.edit.liveNow")}</p>
          {/if}
        {/if}
      </div>
    </Card>
  {/if}
{/if}

<ConfirmDialog
  open={confirmApply}
  title={t("settings.edit.confirmTitle")}
  lines={[backup ? t("settings.edit.confirmBackup") : t("settings.edit.confirmNoBackup")]}
  confirmLabel={t("settings.edit.apply")}
  danger={!backup}
  onconfirm={() => { confirmApply = false; applyDraft({ backup }); }}
  oncancel={() => (confirmApply = false)}
/>
<ConfirmDialog
  open={confirmRestart}
  title={t("services.confirmSyncTitle")}
  lines={[t("services.confirmSync"), t("services.confirmSyncSafe")]}
  confirmLabel={t("services.sync")}
  onconfirm={restart}
  oncancel={() => (confirmRestart = false)}
/>
<ConfirmDialog
  open={confirmReset}
  title={t("settings.edit.resetTitle")}
  lines={[tn("settings.edit.resetLine", settings.overrides.length)]}
  confirmLabel={t("settings.edit.reset")}
  onconfirm={() => { confirmReset = false; resetOverrides(); }}
  oncancel={() => (confirmReset = false)}
/>

<style>
  .toolbar { display: flex; align-items: center; gap: var(--s3); flex-wrap: wrap; }
  .search { display: flex; align-items: center; gap: var(--s2); flex: 1 1 220px; min-width: 0; color: var(--m-muted); }
  .live-note { align-items: center; flex-wrap: wrap; }
  .live-note span { flex: 1 1 240px; }
  .sections, .blocks { display: flex; flex-direction: column; gap: var(--s3); }
  .block { display: flex; flex-direction: column; gap: var(--s2); }
  .block + .block { border-top: 1px solid var(--m-line); padding-top: var(--s4); }
  .fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--s4) var(--s5); margin-top: var(--s2); }
  .small { font-size: 12.5px; overflow-wrap: anywhere; }
  .sub { font-size: 13px; font-weight: 620; margin: var(--s4) 0 var(--s2); }
  .block .sub { margin-top: 0; }
  .changes {
    position: sticky; bottom: var(--s4); z-index: 4; display: flex; flex-direction: column; gap: var(--s3);
    padding: var(--s3) var(--s4); border-radius: var(--m-radius-card); border: 1px solid var(--m-line-strong);
    background: var(--m-surface-2); box-shadow: var(--m-shadow-pop); max-height: 70vh;
  }
  .review { overflow-y: auto; min-height: 0; }
  .change-bar { display: flex; align-items: center; gap: var(--s2); flex-wrap: wrap; flex: none; }
  .count { flex: 1 1 160px; font-weight: 600; }
  .verdict { display: flex; align-items: center; gap: var(--s3); flex-wrap: wrap; margin: 0; }
  .review .sub { margin-top: var(--s3); }
  .diff { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; font-size: 12.5px; }
  .diff li { display: flex; justify-content: space-between; gap: var(--s3); flex-wrap: wrap; }
  .diff .path { color: var(--m-muted); overflow-wrap: anywhere; }
  .apply { display: flex; align-items: center; justify-content: space-between; gap: var(--s3); flex-wrap: wrap; margin-top: var(--s3); }
  .applied p { margin: 0 0 var(--s2); }
  .generated summary { cursor: pointer; font-size: 13px; font-weight: 600; }
  .generated pre { font-size: 12px; overflow-x: auto; margin: var(--s2) 0 0; padding: var(--s3); border-radius: var(--m-radius-inner); background: var(--m-well); }
  @container content (max-width: 720px) { .fields { grid-template-columns: minmax(0, 1fr); } }
  @media (max-width: 760px) { .changes { bottom: calc(76px + env(safe-area-inset-bottom)); max-height: 55vh; } }
</style>
