<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import Empty from "../../../shared/ui/Empty.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import ConfirmDialog from "../../../shared/ui/ConfirmDialog.svelte";
  import Dialog from "../../../shared/ui/Dialog.svelte";
  import StepList from "../widgets/StepList.svelte";
  import BackupDetails from "../widgets/BackupDetails.svelte";
  import RestorePanel from "../widgets/RestorePanel.svelte";
  import StaleNote from "../widgets/StaleNote.svelte";
  import { saveBlob } from "../../../shared/lib/download.js";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { system, ensure, reload } from "../state/system.svelte.js";
  import { adminDownload, adminSend } from "../state/auth.svelte.js";
  import { backupJobs, contactLost, createBackup, dismissRestore, followAgain, reloadIfGone, startUnanswered, uploadBackup } from "../state/backupJobs.svelte.js";
  import { CREATE_SCOPES, INFLUX_REASONS, archiveView, listWarningView, newestFirst, setView, scopeAvailable, stepView } from "../lib/backups.js";
  import { errorKey, errorView } from "../lib/errors.js";

  ensure("backups");

  const TYPE_ICONS = { config: "file-cog", databases: "database", influxdb: "chart-spline", system: "boxes" };
  const TYPE_TONES = { config: "c-soc", databases: "c-out", influxdb: "c-grid", system: "c-inv" };
  const OUTCOME_ICONS = { ok: "circle-check", partial: "triangle-alert", lost: "wifi-off", unknown: "circle-help", fault: "circle-x" };

  const f = $derived(fmt());
  const listing = $derived(system.backups.data);
  const archives = $derived((Array.isArray(listing?.backups) ? listing.backups : []).map(archiveView));
  const sets = $derived((Array.isArray(listing?.sets) ? listing.sets : []).map(setView));
  const rows = $derived(newestFirst([...sets.map((set) => ({ ...set, rowKind: "set" })), ...archives.map((archive) => ({ ...archive, rowKind: "archive" }))]));
  const warnings = $derived((Array.isArray(listing?.warnings) ? listing.warnings : []).map(listWarningView).filter((warning) => warning && warning.key !== "backups.unsafeLocation"));
  const created = backupJobs.create;
  const createSteps = $derived(created.steps.map((step) => stepView(step)));
  const createOutcome = $derived.by(() => {
    if (created.running) return null;
    if (created.result?.ok && created.result.backup_set?.status === "partial") return { tone: "partial", text: t("backups.createdPartial"), warnings: (created.result.warnings || []).map(String) };
    if (created.result) return created.result.ok ? { tone: "ok", text: t("backups.created") } : created.result.message ? { tone: "fault", text: created.result.message, lang: "en" } : { tone: "fault", text: t("backups.createFailed") };
    if (contactLost(created)) return { tone: "lost", text: t("jobs.contactLost") };
    if (startUnanswered(created)) return { tone: "unknown", text: t("error.noAnswer") };
    if (created.error) return { tone: "fault", ...errorView(created.error, t, created.jobId ? t("jobs.unreadable") : t("backups.createFailed")) };
    return null;
  });
  const busy = $derived(backupJobs.busy);

  let scope = $state("system");
  const uploading = $derived(backupJobs.uploading);
  const upload = $derived.by(() => {
    const current = backupJobs.upload;
    if (!current) return null;
    if (current.state === "busy") return { tone: "busy", text: t("backups.uploading", { name: current.name }) };
    if (current.state === "ok") return { tone: "ok", text: t("backups.uploaded", { name: current.name }) };
    return { tone: "fault", ...errorView(current.error, t, t("backups.uploadFailed")) };
  });
  let notice = $state.raw(null);
  let restoreTarget = $state.raw(backupJobs.restoreTarget);
  let detailsFor = $state.raw(null);
  let deleting = $state.raw(null);
  let setDeleteMode = $state("metadata_and_archives");
  let fileInput = $state();

  $effect(() => {
    if (!scopeAvailable(scope, listing)) scope = "system";
  });

  async function download(archive) {
    notice = { tone: "busy", text: t("backups.preparing", { name: archive.name }) };
    try {
      saveBlob(await adminDownload("/api/admin/maintenance/backups/export", { id: archive.id }), archive.name || "ems-backup.tar.gz");
      notice = { tone: "ok", text: t("backups.downloaded", { name: archive.name }) };
    } catch (error) {
      notice = { tone: "fault", ...errorView(error, t, t("backups.downloadFailed")) };
      reloadIfGone(error);
    }
  }

  function bringBack(event) {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = "";
    uploadBackup(file);
  }

  function openRestore(target) {
    if (backupJobs.restore.running) return;
    dismissRestore();
    restoreTarget = target;
  }

  function closeRestore() {
    dismissRestore();
    restoreTarget = null;
  }

  async function remove() {
    const target = deleting;
    deleting = null;
    if (!target) return;
    const body = { id: target.id, confirm: true };
    if (target.kind === "set") body.mode = setDeleteMode;
    try {
      await adminSend("/api/admin/maintenance/backups/delete", { body });
      notice = { tone: "ok", text: t("backups.deleted", { name: target.name }) };
      if (restoreTarget?.id === target.id) closeRestore();
    } catch (error) {
      notice = { tone: "fault", ...errorView(error, t, t("backups.deleteFailed")) };
    } finally {
      reload("backups", "overview");
    }
  }

  function typeName(item) {
    return item.type ? t(`backup.type.${item.type}`) : item.typeText || "—";
  }
</script>

{#snippet result(item, testid)}
  <div role="status" aria-live="polite">
    {#if item}
      <p class="outcome" class:failed={item.tone === "fault"} data-testid={testid}>
        <Icon name={item.tone === "busy" ? "loader" : item.tone === "ok" ? "circle-check" : "circle-x"} /><span lang={item.lang}>{item.text}</span>
      </p>
    {/if}
  </div>
{/snippet}

<Card title={t("backups.title")} subtitle={t("backups.sub")} span="span-12">
  {#snippet actions()}
    <button class="icon-btn" type="button" onclick={() => reload("backups")} disabled={system.backups.loading || busy} aria-label={t("common.refresh")} title={t("common.refresh")}><Icon name="refresh-cw" /></button>
  {/snippet}
  <StaleNote resource={system.backups} />
  {#if listing}
    <div class="facts">
      <div class="fact"><div class="fact-label">{t("backups.count")}</div><div class="fact-value num">{f.number(listing.summary?.total || 0)}</div></div>
      <div class="fact">
        <div class="fact-label">{t("backups.latest")}</div>
        <div class="fact-value num" class:na={!listing.summary?.latest_created_at}>{listing.summary?.latest_created_at ? f.when(listing.summary.latest_created_at) : t("overview.noBackup")}</div>
      </div>
      <div class="fact"><div class="fact-label">{t("backups.where")}</div><div class="fact-value num wrap" title={listing.backup_dir}>{listing.backup_dir || "—"}</div></div>
    </div>
    {#if listing.safe_location === false}<p class="note warn"><Icon name="triangle-alert" />{t("backups.unsafeLocation")}</p>{/if}
    {#each warnings as warning, index (index)}<p class="note warn"><Icon name="triangle-alert" />{#if warning.key}{t(warning.key)}{:else}<span lang="en">{warning.text}</span>{/if}</p>{/each}
  {:else if system.backups.error}
    <Empty error {...errorView(system.backups.error, t)} />
  {:else}
    <Empty text={t("common.loading")} icon="loader" />
  {/if}
</Card>

<Card title={t("backups.createTitle")} subtitle={t("backups.createSub")} span="span-8">
  <fieldset class="scopes" disabled={busy}>
    <legend class="sr-only">{t("backups.scope")}</legend>
    {#each CREATE_SCOPES as option (option)}
      {@const available = scopeAvailable(option, listing)}
      <label class="scope inner {TYPE_TONES[option]}" class:chosen={scope === option} class:off={!available} data-scope={option}>
        <input type="radio" name="backup-scope" value={option} bind:group={scope} disabled={!available} />
        <span class="icon-tile sm {TYPE_TONES[option]}"><Icon name={TYPE_ICONS[option]} /></span>
        <span class="words">
          <strong>{t(`backups.scope.${option}`)}{#if option === "system"}<span class="badge">{t("backups.recommended")}</span>{/if}</strong>
          <span class="muted">{#if available}{t(`backups.scope.${option}.desc`)}{:else if INFLUX_REASONS.includes(listing?.influxdb?.reason)}{t(`backups.scope.influxdb.${listing.influxdb.reason}`)}{:else if listing?.influxdb?.message}<span lang="en">{listing.influxdb.message}</span>{:else}{t("backups.scope.influxdb.off")}{/if}</span>
        </span>
      </label>
    {/each}
  </fieldset>
  <div class="row-wrap actions">
    <button class="btn" type="button" onclick={() => createBackup(scope)} disabled={busy || !listing} data-testid="create-backup">
      <Icon name={created.running ? "loader" : "archive"} size="sm" />{created.running ? t("backups.creating") : t("backups.create")}
    </button>
  </div>
  <p class="note"><Icon name="info" />{t("backups.encryptionNote")}</p>
  {#if createSteps.length}<StepList steps={createSteps} label={t("backups.progress")} />{/if}
  <div role="status" aria-live="polite">
    {#if created.lost}<p class="note warn"><Icon name="wifi-off" />{t("jobs.lost")}</p>{/if}
    {#if createOutcome}<p class="outcome {createOutcome.tone}" data-testid="create-outcome"><Icon name={OUTCOME_ICONS[createOutcome.tone] || "circle-x"} /><span lang={createOutcome.lang}>{createOutcome.text}</span></p>{/if}
  </div>
  {#if createOutcome}
    {#each createOutcome.warnings || [] as warning, index (index)}<p class="note warn"><Icon name="triangle-alert" /><span lang="en">{warning}</span></p>{/each}
    {#if createOutcome.tone === "lost"}<button class="btn secondary small" type="button" onclick={() => followAgain(created)}><Icon name="refresh-cw" size="sm" />{t("jobs.followAgain")}</button>{/if}
  {/if}
</Card>

<Card title={t("backups.elsewhereTitle")} subtitle={t("backups.elsewhereSub")} span="span-4">
  <p class="lead muted">{t("backups.elsewhere")}</p>
  <button class="btn secondary" type="button" onclick={() => fileInput?.click()} disabled={busy || uploading}><Icon name="upload" size="sm" />{t("backups.bringBack")}</button>
  <input class="sr-only" type="file" accept=".gz,.enc,application/gzip" bind:this={fileInput} onchange={bringBack} tabindex="-1" aria-hidden="true" data-testid="backup-import" />
  {@render result(upload, "transfer-status")}
</Card>

<Card title={t("backups.listTitle")} subtitle={t("backups.listSub")} span="span-12">
  {@render result(notice, "list-status")}
  {#if !listing}
    <Empty {...(system.backups.error ? errorView(system.backups.error, t) : { text: t("common.loading") })} icon="loader" error={Boolean(system.backups.error)} />
  {:else if !archives.length && !sets.length}
    <Empty text={t("backups.none")} icon="archive" />
  {:else}
    <ul class="list" data-testid="backup-list">
      {#each rows as row (`${row.rowKind}-${row.id}`)}
        {#if row.rowKind === "set"}
          {@const set = row}
          <li class="row inner" data-kind="set">
            <span class="icon-tile sm c-inv"><Icon name="boxes" /></span>
            <div class="words">
              <div class="name ellipsis" title={set.name}>{set.name}</div>
              <div class="meta muted num">
                {[set.createdAt ? f.dateTime(set.createdAt) : "", set.members.map((member) => typeName(member) + (member.present ? "" : ` (${t("backups.missingMember")})`)).join(", ")].filter(Boolean).join(" · ")}
              </div>
            </div>
            <div class="row-actions">
              <Status tone={set.complete ? "c-ok" : "c-warn"} text={set.complete ? t("backups.setComplete") : set.partial && !set.missing ? t("backups.setPartial") : t("backups.setIncomplete")} />
              <button class="btn secondary small" type="button" disabled={busy || !set.restorable} onclick={() => openRestore({ kind: "set", id: set.id, type: "system", name: set.name, locked: false })} aria-label={t("backups.restoreNamed", { name: set.name })}>{t("backups.restore")}</button>
              <button class="btn secondary small danger-text" type="button" disabled={busy} onclick={() => { setDeleteMode = "metadata_and_archives"; deleting = { kind: "set", id: set.id, name: set.name }; }} aria-label={t("backups.deleteNamed", { name: set.name })}><Icon name="trash-2" size="sm" /></button>
            </div>
          </li>
        {:else}
          {@const archive = row}
          <li class="row inner" data-kind="archive" data-type={archive.type || archive.typeText}>
            <span class="icon-tile sm {TYPE_TONES[archive.type] || 'c-idle'}"><Icon name={TYPE_ICONS[archive.type] || "file-archive"} /></span>
            <div class="words">
              <div class="name ellipsis" title={archive.name}>{archive.name}</div>
              <div class="meta muted num">
                {[typeName(archive), archive.createdAt ? f.dateTime(archive.createdAt) : "", f.bytes(archive.size), archive.version || ""].filter(Boolean).join(" · ")}
              </div>
              {#if !archive.valid}<p class="flag fault"><Icon name="circle-x" size="sm" />{#if errorKey({ error: archive.problem })}{t(errorKey({ error: archive.problem }))}{:else if archive.problem}<span lang="en">{archive.problem}</span>{:else}{t("backups.invalid")}{/if}</p>{/if}
              {#if archive.type === "influxdb"}<p class="flag"><Icon name="info" size="sm" />{t("backups.influxNote")}</p>{/if}
            </div>
            <div class="row-actions">
              {#if archive.locked}<Status tone="c-idle" icon="lock" text={t("backups.encrypted")} />{/if}
              <button class="btn secondary small" type="button" onclick={() => (detailsFor = archive)} aria-label={t("backups.detailsNamed", { name: archive.name })}>{t("common.details")}</button>
              <button class="btn secondary small" type="button" onclick={() => download(archive)} aria-label={t("backups.downloadNamed", { name: archive.name })} title={t("backups.download")}><Icon name="download" size="sm" /></button>
              <button class="btn secondary small" type="button" disabled={busy || !archive.valid} onclick={() => openRestore({ kind: "archive", id: archive.id, type: archive.type || "config", name: archive.name, locked: archive.locked })} aria-label={t("backups.restoreNamed", { name: archive.name })} data-testid="restore-open">{t("backups.restore")}</button>
              <button class="btn secondary small danger-text" type="button" disabled={busy} onclick={() => (deleting = { kind: "archive", id: archive.id, name: archive.name })} aria-label={t("backups.deleteNamed", { name: archive.name })} title={t("backups.delete")}><Icon name="trash-2" size="sm" /></button>
            </div>
          </li>
        {/if}
      {/each}
    </ul>
  {/if}
</Card>

{#if restoreTarget}
  {#key restoreTarget.id}
    <RestorePanel target={restoreTarget} onclose={closeRestore} />
  {/key}
{/if}

<BackupDetails archive={detailsFor} onclose={() => (detailsFor = null)} />

<ConfirmDialog
  open={deleting?.kind === "archive"}
  title={t("backups.deleteTitle")}
  lines={[t("backups.deleteConfirm", { name: deleting?.name || "" })]}
  confirmLabel={t("backups.delete")}
  danger
  onconfirm={remove}
  oncancel={() => (deleting = null)}
/>

<Dialog open={deleting?.kind === "set"} title={t("backups.deleteSetTitle")} onclose={() => (deleting = null)}>
  <p class="muted">{t("backups.deleteSetLead", { name: deleting?.name || "" })}</p>
  <fieldset class="set-modes">
    <legend class="sr-only">{t("backups.deleteSetTitle")}</legend>
    <label class="check"><input type="radio" name="set-delete" value="metadata_and_archives" bind:group={setDeleteMode} />{t("backups.deleteSetAll")}</label>
    <label class="check"><input type="radio" name="set-delete" value="metadata_only" bind:group={setDeleteMode} />{t("backups.deleteSetOnly")}</label>
  </fieldset>
  {#snippet actions()}
    <button class="btn secondary" type="button" onclick={() => (deleting = null)}>{t("auth.cancel")}</button>
    <button class="btn danger" type="button" onclick={remove}>{t("backups.delete")}</button>
  {/snippet}
</Dialog>

<style>
  .scopes { border: 0; padding: 0; margin: 0; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--s2); }
  .scope { display: flex; gap: var(--s3); align-items: flex-start; cursor: pointer; border: 1px solid var(--m-line); }
  .scope input { position: absolute; opacity: 0; pointer-events: none; }
  .scope:has(input:focus-visible) { outline: 2px solid var(--m-accent); outline-offset: 2px; }
  .scope.chosen { border-color: color-mix(in srgb, var(--c, var(--m-accent)) 55%, transparent); background: color-mix(in srgb, var(--c, var(--m-accent)) 9%, var(--m-surface-2)); }
  .scope.off { opacity: 0.55; cursor: not-allowed; }
  .scope .words { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
  .scope strong { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .scope .muted { font-size: 12.5px; }
  .actions { margin-top: var(--s4); }
  .lead { margin: 0 0 var(--s3); font-size: 13px; }
  .outcome { display: flex; gap: 8px; align-items: flex-start; margin: var(--s3) 0; font-weight: 560; overflow-wrap: anywhere; }
  .outcome :global(svg.i) { color: var(--m-ok); margin-top: 1px; flex: none; }
  .outcome.failed :global(svg.i), .outcome.fault :global(svg.i) { color: var(--m-fault); }
  .outcome.lost :global(svg.i), .outcome.partial :global(svg.i), .outcome.unknown :global(svg.i) { color: var(--m-warn); }
  .list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--s2); }
  .row { display: flex; align-items: center; gap: var(--s3); flex-wrap: wrap; }
  .row .words { flex: 1 1 180px; min-width: 0; }
  .name { font-weight: 600; font-family: var(--m-mono); font-size: 13px; }
  .meta { font-size: 12.5px; margin-top: 2px; }
  .flag { display: flex; gap: 6px; align-items: center; margin: 4px 0 0; font-size: 12.5px; color: var(--m-muted); }
  .flag.fault { color: var(--m-fault); }
  .row-actions { display: flex; gap: var(--s2); align-items: center; flex-wrap: wrap; margin-left: auto; }
  .danger-text:not(:disabled):hover { color: var(--m-fault); border-color: color-mix(in srgb, var(--m-fault) 50%, transparent); }
  .set-modes { border: 0; padding: 0; margin: var(--s3) 0 0; display: flex; flex-direction: column; gap: var(--s2); }
  @container content (max-width: 640px) {
    .scopes { grid-template-columns: minmax(0, 1fr); }
    .row .name { white-space: normal; overflow-wrap: anywhere; }
  }
</style>
