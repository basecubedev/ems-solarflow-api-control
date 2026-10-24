<script>
  import Card from "../../../../shared/ui/Card.svelte";
  import Icon from "../../../../shared/ui/Icon.svelte";
  import ConfirmDialog from "../../../../shared/ui/ConfirmDialog.svelte";
  import SettingField from "../SettingField.svelte";
  import CatalogText from "../CatalogText.svelte";
  import SettingsSection from "../SettingsSection.svelte";
  import SetupHardware from "./SetupHardware.svelte";
  import { t } from "../../i18n/i18n.svelte.js";
  import { safetyBlocks, sharedRisk, tabSections } from "../../lib/settings.js";
  import { BUSY_OPERATIONS, CONFIG_CONFLICTS, featureValue } from "../../lib/setupDraft.js";
  import { setup } from "../../state/setup.svelte.js";
  import { setupDraft, applyConfig, clearDraft, discardLocal, downloadConfig, rejoinCurrent, reviewAgain, setFeature } from "../../state/setupDraft.svelte.js";

  let { onrestart } = $props();

  let password = $state("");
  let confirmation = $state("");
  let clearing = $state(false);

  const catalog = $derived(setupDraft.catalog);
  const config = $derived({ catalog: { feature_sections: (catalog?.sections || []).filter((section) => !["devices", "grid_meter"].includes(section.id)) } });
  const features = $derived(tabSections(config, "features"));
  const advanced = $derived(tabSections(config, "expert"));
  const blocks = $derived(safetyBlocks(config));
  const values = $derived(setupDraft.draft.features);
  const hints = $derived(setupDraft.hints);
  const preview = $derived(setupDraft.preview);
  const tone = $derived(hints.some((hint) => hint.tone === "error") ? "error" : hints.some((hint) => hint.tone === "warn") ? "warn" : preview ? "ready" : "pending");
  const verdict = $derived(setup.stale ? "superseded" : preview?.unavailable ? "unavailable" : tone === "ready" ? "ready" : tone === "pending" ? "checking" : "attention");
  const summary = $derived(preview?.summary || {});
  const inverterCount = $derived(Number(summary.inverters || 0));
  const meterCount = $derived(Number(summary.grid_meters || 0));
  const readOnlyCount = $derived(Number(summary.read_only_devices || 0));

  function onset(field, value) {
    setFeature(field.path, value);
  }

  async function download(event) {
    event.preventDefault();
    if (await downloadConfig(password, confirmation)) {
      password = "";
      confirmation = "";
    }
  }
</script>

{#snippet status(entry)}
  {#if entry}
    <p class={entry.tone === "error" ? "form-error" : entry.tone === "ok" ? "form-ok" : "muted small"} role="status">
      {#if entry.raw}<span lang="en">{entry.raw}</span>
      {:else if entry.key === "busy"}{t("setup.config.busy", { what: BUSY_OPERATIONS.includes(entry.operation) ? t(`setup.operation.${entry.operation}`) : t("setup.operation.other") })}
      {:else}{t(`setup.config.status.${entry.key}`, entry.values || {})}{#if entry.backup} {t("setup.config.backedUp", { path: entry.backup })}{:else if entry.preserved} {t("setup.config.preserved")}{/if}{/if}
    </p>
  {/if}
{/snippet}

<Card title={t("setup.config.validation")} span="span-12">
  <div class="stack" data-testid="setup-validation" data-tone={tone}>
    <div class="row-wrap">
      <span class="verdict {tone}" data-testid="setup-verdict-state">{t(`setup.config.verdict.${verdict}`)}</span>
      <span class="muted small">{setup.template ? t("setup.config.templateLoaded", { tag: setup.template.tag }) : t("setup.config.templateMissing")}</span>
    </div>
    <ul class="hints">
      {#each hints.length ? hints : [{ tone: "info", key: "waiting" }] as hint, index (index)}
        <li class="hint {hint.tone}"><Icon name={hint.tone === "error" ? "circle-x" : hint.tone === "warn" ? "triangle-alert" : "circle-check"} size="sm" />{#if hint.raw}<span lang="en">{hint.raw}</span>{:else}<span>{t(`setup.hint.${hint.key}`, hint.values || {})}</span>{/if}</li>
      {/each}
    </ul>
    <p class="muted small num">
      {t("setup.config.devices", { inverters: inverterCount, meters: meterCount })}{#if readOnlyCount} · {t("setup.config.readOnly", { count: readOnlyCount })}{/if}
      · {t("setup.config.release")}: <strong lang="en">{preview?.release || t("setup.config.notPrepared")}</strong>
      · {t("setup.config.base")}: <strong>{preview?.base?.replaces_config_path ? t("setup.config.baseReplaces") : t("setup.config.baseTemplate")}</strong>
    </p>
    {#if setup.stale}
      <div class="note warn" role="alert" data-testid="setup-workflow-conflict">
        <Icon name="triangle-alert" />
        <span>{#if setupDraft.workflowMessage}<span lang="en">{setupDraft.workflowMessage}</span>{:else}{t("setup.config.stale")}{/if}</span>
        <button class="btn secondary small" type="button" onclick={rejoinCurrent}>{t("setup.rejoin")}</button>
        <button class="btn secondary small" type="button" onclick={discardLocal}>{t("setup.config.discardLocal")}</button>
      </div>
    {/if}
  </div>
</Card>

<Card title={t("setup.config.hardware")} subtitle={t("setup.config.hardwareSub")} span="span-12">
  {#snippet actions()}
    <button class="btn secondary small" type="button" onclick={() => (clearing = true)} data-testid="setup-clear-draft"><Icon name="eraser" size="sm" />{t("setup.config.clear")}</button>
  {/snippet}
  {#if catalog}<SetupHardware />{:else}<p class="muted">{setupDraft.catalogError ? t("setup.config.catalogFailed") : t("common.loading")}</p>{/if}
</Card>

{#if catalog}
  <Card title={t("setup.config.features")} subtitle={t("setup.config.featuresSub")} span="span-12">
    <div class="sections" data-testid="setup-features">
      {#each features as section (section.id)}<SettingsSection {section} {values} {onset} startOpen={section.collapsible === false} />{/each}
    </div>
  </Card>

  <Card title={t("setup.config.safety")} subtitle={t("setup.config.safetySub")} span="span-12">
    <div class="sections" data-testid="setup-safety">
      {#each blocks as block (block.id)}
        {@const risk = sharedRisk(block.fields)}
        <div class="block">
          <h3 class="sub"><CatalogText text={block.title} /></h3>
          {#if block.summary}<p class="muted small"><CatalogText text={block.summary} /></p>{/if}
          {#if risk}<p class="note warn"><Icon name="triangle-alert" />{t(`settings.risk.${risk}.shared`)}</p>{/if}
          <div class="fields">
            {#each block.fields as field (field.path)}<SettingField {field} value={featureValue(values, field)} hideRisk={Boolean(risk)} type={field.secret ? "password" : ""} onchange={(value) => setFeature(field.path, value)} />{/each}
          </div>
        </div>
      {/each}
    </div>
  </Card>

  <Card title={t("setup.config.advanced")} subtitle={t("setup.config.advancedSub")} span="span-12">
    <div class="sections" data-testid="setup-advanced">
      {#each advanced as section (section.id)}<SettingsSection {section} {values} {onset} startOpen={section.collapsible === false} />{/each}
    </div>
  </Card>
{/if}

<Card title={t("setup.config.generated")} subtitle={t("setup.config.generatedSub")} span="span-12">
  <div class="stack" data-testid="setup-generated">
    <details>
      <summary>{t("setup.config.showGenerated")}</summary>
      <pre class="code" lang="en">{JSON.stringify(preview?.config || {}, null, 2)}</pre>
    </details>
    <form class="row-wrap" onsubmit={download}>
      <label class="field"><span>{t("setup.config.password")}</span><input class="input" type="password" autocomplete="new-password" bind:value={password} data-testid="setup-download-password" /></label>
      <label class="field"><span>{t("setup.config.passwordAgain")}</span><input class="input" type="password" autocomplete="new-password" bind:value={confirmation} /></label>
      <button class="btn secondary small" type="submit" disabled={!setupDraft.exportAllowed || Boolean(setupDraft.busy)} data-testid="setup-download"><Icon name="download" size="sm" />{t("setup.config.download")}</button>
      <button class="btn small" type="button" onclick={applyConfig} disabled={!setupDraft.exportAllowed || Boolean(setupDraft.busy)} data-testid="setup-apply"><Icon name="save" size="sm" />{t("setup.config.apply")}</button>
    </form>
    <p class="muted small">{t("setup.config.downloadHint")}</p>
    <p class="muted small">{t("setup.config.applyHint")}</p>
    {#if setupDraft.exportAllowed && setupDraft.draft.selections.length && !setupDraft.exportStatus}<p class="muted small">{t("setup.config.mqttIncluded")}</p>{/if}
    {@render status(setupDraft.exportStatus)}
    {@render status(setupDraft.applyStatus)}
    {#if setupDraft.rollback}<p class="note warn" role="alert" data-testid="setup-rollback"><Icon name="triangle-alert" /><span lang="en">{setupDraft.rollback.message}{#if setupDraft.rollback.failed_refs?.length} ({setupDraft.rollback.failed_refs.join(", ")}){/if}</span></p>{/if}
    {#if setupDraft.conflict}
      <div class="note warn" role="alert" data-testid="setup-config-conflict">
        <Icon name="triangle-alert" />
        <span>{CONFIG_CONFLICTS.includes(setupDraft.conflict.error) ? t(`setup.config.status.conflict.${setupDraft.conflict.error}`) : ""}</span>
        <button class="btn small" type="button" onclick={reviewAgain}>{t("setup.config.review")}</button>
        <button class="btn secondary small" type="button" onclick={onrestart}>{t("setup.config.discardSetup")}</button>
      </div>
      {#if setupDraft.conflict.message}<details><summary>{t("setup.config.technical")}</summary><pre class="code" lang="en">{setupDraft.conflict.message}</pre></details>{/if}
    {/if}
    <details>
      <summary>{t("setup.config.template")}</summary>
      <p class="muted small">{t("setup.config.templateLead")}</p>
      <pre class="code" lang="en">{JSON.stringify(setup.template?.template || {}, null, 2)}</pre>
    </details>
  </div>
</Card>

<ConfirmDialog
  open={clearing}
  title={t("setup.config.clearTitle")}
  lines={[t("setup.config.clearLine")]}
  confirmLabel={t("setup.config.clear")}
  danger
  onconfirm={() => { clearing = false; clearDraft(); }}
  oncancel={() => (clearing = false)}
/>

<style>
  .stack { display: flex; flex-direction: column; gap: var(--s3); min-width: 0; }
  .stack p { margin: 0; }
  .small { font-size: 12.5px; overflow-wrap: anywhere; }
  .row-wrap { gap: var(--s2); align-items: flex-end; }
  .verdict { font-weight: 620; }
  .verdict.error { color: var(--m-fault); }
  .verdict.warn { color: var(--m-warn); }
  .verdict.ready { color: var(--m-ok); }
  .hints { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
  .hint { display: flex; gap: var(--s2); align-items: flex-start; font-size: 13px; }
  .hint.error :global(svg.i) { color: var(--m-fault); }
  .hint.warn :global(svg.i) { color: var(--m-warn); }
  .hint.info :global(svg.i) { color: var(--m-ok); }
  .sections { display: flex; flex-direction: column; gap: var(--s3); }
  .block { display: flex; flex-direction: column; gap: var(--s2); }
  .block p { margin: 0; }
  .sub { font-size: 13px; font-weight: 620; margin: 0; }
  .fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--s3) var(--s4); }
  .field { display: flex; flex-direction: column; gap: 4px; font-size: 13px; flex: 1 1 180px; min-width: 0; }
  .code { margin: 0; max-height: 360px; overflow: auto; font-size: 12px; background: var(--m-surface-2); border-radius: var(--m-radius-control); padding: var(--s3); }
  details > summary { cursor: pointer; font-weight: 560; }
  .note { flex-wrap: wrap; }
  @container content (max-width: 720px) { .fields { grid-template-columns: minmax(0, 1fr); } }
</style>
