<script>
  import { untrack } from "svelte";
  import Dialog from "../../../shared/ui/Dialog.svelte";
  import Empty from "../../../shared/ui/Empty.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { adminSend } from "../state/auth.svelte.js";
  import { reloadIfGone } from "../state/backupJobs.svelte.js";
  import { BACKUP_PURPOSES, BACKUP_TYPES } from "../lib/backups.js";
  import { errorView } from "../lib/errors.js";

  /** archive: { id, name, locked } of the archive to show, or null when closed. */
  let { archive, onclose } = $props();

  let details = $state.raw(null);
  let password = $state("");
  let error = $state.raw(null);
  let busy = $state(false);
  const f = $derived(fmt());
  const manifest = $derived(details?.manifest || null);
  const source = $derived(manifest?.source || {});

  let request = 0;

  /** The newest request wins: a late answer for the archive shown before must not fill this one. */
  async function inspect(withPassword) {
    request += 1;
    const mine = request;
    busy = true;
    error = null;
    try {
      const data = await adminSend("/api/admin/maintenance/backups/inspect", { body: { id: archive.id, password: withPassword || null } });
      if (mine !== request) return;
      details = data;
      if (withPassword && data.locked) error = { text: t("backups.wrongPassword") };
    } catch (err) {
      if (mine === request) error = errorView(err, t, t("backups.inspectFailed"));
      reloadIfGone(err);
    } finally {
      if (mine === request) busy = false;
    }
  }

  $effect(() => {
    if (!archive) {
      request += 1;
      return;
    }
    untrack(() => {
      details = null;
      password = "";
      error = null;
      inspect(null);
    });
  });

  function unlock(event) {
    event.preventDefault();
    inspect(password);
  }

  function typeText(type) {
    return BACKUP_TYPES.includes(type) ? t(`backup.type.${type}`) : type || "—";
  }

  function tags(file) {
    return [file.sensitive && t("backups.tag.sensitive"), file.privacy_relevant && t("backups.tag.privacy"), !file.has_checksum && t("backups.tag.noChecksum")].filter(Boolean);
  }
</script>

<Dialog open={Boolean(archive)} title={archive?.name || ""} wide {onclose}>
  {#if busy && !details}
    <Empty text={t("common.loading")} icon="loader" />
  {:else if details?.locked}
    <form class="unlock" onsubmit={unlock} autocomplete="off">
      <p class="muted"><Icon name="lock" size="sm" />{t("backups.lockedLead")}</p>
      <label class="field" for="backup-details-password"><span>{t("backups.password")}</span>
        <input class="input" id="backup-details-password" type="password" autocomplete="off" bind:value={password} required />
      </label>
      <button class="btn" type="submit" disabled={busy || !password}>{t("backups.unlock")}</button>
    </form>
  {/if}
  {#if error}<p class="form-error" role="alert" lang={error.lang}>{error.text}</p>{/if}
  {#if manifest}
    <div class="facts">
      <div class="fact"><div class="fact-label">{t("backups.fact.type")}</div><div class="fact-value">{typeText(manifest.backup_type)}</div></div>
      <div class="fact"><div class="fact-label">{t("backups.fact.created")}</div><div class="fact-value num">{manifest.created_at ? f.dateTime(manifest.created_at) : "—"}</div></div>
      <div class="fact"><div class="fact-label">{t("backups.fact.purpose")}</div><div class="fact-value">{BACKUP_PURPOSES.includes(manifest.backup_purpose) ? t(`backup.purpose.${manifest.backup_purpose}`) : manifest.backup_purpose || "—"}</div></div>
      <div class="fact"><div class="fact-label">{t("backups.fact.version")}</div><div class="fact-value num" title={source.build_label || ""}>{source.ems_version || source.build_label || source.git_describe || source.git_commit_short || "—"}</div></div>
      <div class="fact"><div class="fact-label">{t("backups.fact.files")}</div><div class="fact-value num">{f.number(manifest.files_count || 0)}</div></div>
      <div class="fact"><div class="fact-label">{t("backups.fact.sensitive")}</div><div class="fact-value num">{f.number(manifest.sensitive_count || 0)}</div></div>
    </div>
    {#if Number(manifest.sensitive_count) > 0}<p class="note"><Icon name="shield-alert" />{t("backups.sensitiveNote")}</p>{/if}
    <h3 class="sub">{t("backups.contents")}</h3>
    <ul class="files">
      {#each manifest.files || [] as file (file.path)}
        <li><span class="path">{file.path}</span><span class="muted">{[file.size_bytes !== undefined ? f.bytes(file.size_bytes) : "", ...tags(file)].filter(Boolean).join(" · ")}</span></li>
      {/each}
    </ul>
  {/if}
</Dialog>

<style>
  .unlock { display: flex; flex-direction: column; gap: var(--s3); margin-bottom: var(--s3); }
  .unlock p { display: flex; gap: 6px; align-items: center; margin: 0; }
  .sub { font-size: 13px; font-weight: 620; margin: var(--s4) 0 var(--s2); }
  .files { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
  .files li { display: flex; justify-content: space-between; gap: var(--s3); padding: 6px 0; border-bottom: 1px solid var(--m-line); font-size: 13px; flex-wrap: wrap; }
  .path { font-family: var(--m-mono); font-size: 12.5px; overflow-wrap: anywhere; }
</style>
