<script>
  import Card from "../ui/Card.svelte";
  import Fact from "../ui/Fact.svelte";
  import Empty from "../ui/Empty.svelte";
  import Icon from "../ui/Icon.svelte";
  import Status from "../ui/Status.svelte";
  import ConfirmDialog from "../ui/ConfirmDialog.svelte";
  import OperatorGate from "../ui/OperatorGate.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { auth, authorizedSend, operatorGet } from "../state/auth.svelte.js";
  import { errorText } from "../lib/errors.js";

  const BACKUP_TYPES = ["config", "databases", "influxdb"];
  const FILE_KINDS = ["config", "runtime_state", "dashboard_auth", "sqlite", "influxdb"];
  const INFLUX_MODES = ["bundled", "external"];

  let status = $state.raw(null);
  let backups = $state.raw([]);
  let upgrade = $state.raw(null);
  let detail = $state.raw(null);
  let message = $state("");
  let running = $state(false);
  let restoreFile = $state("");
  let restorePassword = $state("");
  let plan = $state.raw(null);
  let confirm = $state({ open: false, kind: "" });
  const f = $derived(fmt());

  async function loadAll() {
    if (!auth.authenticated) return;
    const [s, b, u] = await Promise.allSettled([
      operatorGet("/api/maintenance/status"),
      operatorGet("/api/maintenance/backups"),
      operatorGet("/api/maintenance/config-upgrade"),
    ]);
    if (s.status === "fulfilled") status = s.value;
    if (b.status === "fulfilled") backups = Array.isArray(b.value?.items) ? b.value.items : [];
    if (u.status === "fulfilled") upgrade = u.value;
  }

  $effect(() => {
    void auth.authenticated;
    loadAll();
  });

  const typeLabel = (type) => (BACKUP_TYPES.includes(type) ? t(`maint.type.${type}`) : type || "—");
  const influxMode = (mode) => (INFLUX_MODES.includes(mode) ? t(`maint.influx.${mode}`) : mode || t("common.on"));
  const kindLabel = (kind) => (FILE_KINDS.includes(kind) ? t(`maint.kind.${kind}`) : String(kind || "").replaceAll("_", " "));

  async function createBackup(type) {
    if (running) return;
    running = true;
    message = t("maint.creating", { type: typeLabel(type) });
    try {
      const payload = await authorizedSend("/api/maintenance/backups/create", { body: { type } });
      if (payload?.created) {
        message = t("maint.created", { name: payload.backup?.name || typeLabel(type) });
        await loadAll();
      } else {
        message = payload?.message || t("maint.notCreated");
      }
    } catch (error) {
      message = errorText(error, t);
    } finally {
      running = false;
    }
  }

  async function inspect(name) {
    message = "";
    try {
      detail = { name, payload: await authorizedSend("/api/maintenance/backups/inspect", { body: { file: name } }) };
    } catch (error) {
      message = errorText(error, t);
    }
  }

  async function preview() {
    if (running || !restoreFile) return;
    message = t("maint.previewing", { file: restoreFile });
    try {
      plan = await authorizedSend("/api/maintenance/backups/restore-plan", { body: { file: restoreFile, password: restorePassword || null } });
      message = t("maint.previewReady");
    } catch (error) {
      plan = null;
      message = errorText(error, t);
    }
  }

  async function restore() {
    confirm = { open: false, kind: "" };
    if (running || !plan) return;
    running = true;
    message = t("maint.restoring", { file: restoreFile });
    try {
      const payload = await authorizedSend("/api/maintenance/backups/restore", {
        body: {
          file: restoreFile,
          password: restorePassword || null,
          confirm_preview: true,
          confirm_restore: true,
          confirm_replace: true,
        },
      });
      if (payload?.restored) {
        const hints = [payload.requires_restart ? t("maint.restart") : "", payload.requires_relogin ? t("maint.relogin") : ""].filter(Boolean).join(" ");
        message = `${payload.message || t("maint.restored")} ${hints}`.trim();
        await loadAll();
      } else {
        message = payload?.message || t("maint.notRestored");
      }
    } catch (error) {
      message = errorText(error, t);
    } finally {
      restorePassword = "";
      plan = null;
      running = false;
    }
  }

  async function applyUpgrade() {
    confirm = { open: false, kind: "" };
    if (running || !upgrade?.plan_id || !upgrade.apply_available) return;
    running = true;
    message = t("maint.applying");
    try {
      const payload = await authorizedSend("/api/maintenance/config-upgrade/apply", {
        body: { refresh_comments: true, confirm_apply: true, plan_id: upgrade.plan_id },
      });
      if (payload?.changed) {
        const backup = payload.backup_name ? ` ${t("maint.backupName", { name: payload.backup_name })}` : "";
        const restart = payload.requires_restart ? ` ${t("maint.restart")}` : "";
        message = `${payload.message || t("maint.upgraded")}${backup}${restart}`.trim();
        await loadAll();
      } else {
        message = payload?.message || t("maint.upToDate");
      }
    } catch (error) {
      message = errorText(error, t);
    } finally {
      running = false;
    }
  }

  async function checkUpgrade() {
    try {
      upgrade = await operatorGet("/api/maintenance/config-upgrade");
    } catch (error) {
      message = errorText(error, t);
    }
  }

  const items = $derived(Array.isArray(upgrade?.items) ? upgrade.items : []);
  const valueCount = $derived(Number(upgrade?.add_count || 0) + Number(upgrade?.migration_count || 0));
  const size = (bytes) => (typeof bytes === "number" ? `${f.number(Math.max(1, Math.round(bytes / 1024)))} KiB` : "—");
</script>

<OperatorGate>
  {#if message}<div class="span-12 message" role="status">{message}</div>{/if}

  <Card title={t("maint.status")} subtitle={t("maint.statusSub")} span="span-6">
    {#if !status}
      <div class="skeleton"></div>
    {:else}
      <div class="facts two">
        <Fact label={t("maint.config")} value={status.config_path || "—"} title={status.config_path} />
        <Fact label={t("maint.backupDir")} value={status.backup_dir || "—"} title={status.backup_dir} />
        <Fact label={t("maint.backupTypes")} value={(status.backup_types || []).map(typeLabel).join(", ") || "—"} />
        <Fact label="InfluxDB" value={status.influxdb?.enabled ? `${influxMode(status.influxdb.mode)}${status.influxdb.backup_supported ? "" : ` (${t("maint.noBackup")})`}` : t("common.off")} />
        <Fact label={t("maint.restore")} value={status.restore_available_in_dashboard ? t("maint.available") : t("maint.cliOnly")} />
      </div>
    {/if}
  </Card>

  <Card title={t("maint.safety")} span="span-6">
    <ul class="notes">
      <li>{t("maint.safety.rollback")}</li>
      <li>{t("maint.safety.auth")}</li>
      <li>{t("maint.safety.influx")}</li>
      <li>{t("maint.safety.cli")}</li>
    </ul>
  </Card>

  <Card title={t("maint.backups")} subtitle={t("maint.backupsSub")} span="span-12">
    <p class="warn"><Icon name="triangle-alert" size="sm" />{t("maint.secretsWarning")}</p>
    <div class="row-wrap">
      {#each BACKUP_TYPES as type (type)}
        <button class="btn secondary small" type="button" disabled={running} onclick={() => createBackup(type)} title={t(`maint.type.${type}.desc`)}>
          <Icon name="archive" size="sm" />{t("maint.create", { type: typeLabel(type) })}
        </button>
      {/each}
    </div>
    {#if !backups.length}
      <Empty text={t("maint.noBackups")} />
    {:else}
      <div class="table-scroll">
        <table class="data">
          <thead><tr><th>{t("maint.name")}</th><th>{t("maint.type")}</th><th>{t("maint.modified")}</th><th class="r">{t("maint.size")}</th><th>{t("maint.encryption")}</th><th>EMS</th><th></th></tr></thead>
          <tbody>
            {#each backups as item (item.name)}
              <tr>
                <td class="mono">{item.name}</td>
                <td>{typeLabel(item.backup_type)}</td>
                <td class="num">{item.modified_at ? f.dateTime(item.modified_at) : "—"}</td>
                <td class="r num">{size(item.size_bytes)}</td>
                <td>{item.encrypted ? t("maint.encrypted") : t("maint.plain")}</td>
                <td>{item.ems_version || "—"}</td>
                <td><button class="btn secondary small" type="button" onclick={() => inspect(item.name)}>{t("common.details")}</button></td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
    {#if detail}
      {@const m = detail.payload?.manifest}
      <div class="inner detail">
        <h3 class="sub">{t("maint.provenance", { name: detail.name })}</h3>
        {#if !detail.payload?.manifest_available || !m}
          <p class="muted">{detail.payload?.encrypted ? t("maint.encryptedLocked") : t("maint.noManifest")}</p>
        {:else}
          <div class="facts">
            <Fact label={t("maint.purpose")} value={m.backup_purpose || "—"} />
            <Fact label={t("maint.createdAt")} value={m.created_at ? f.dateTime(m.created_at) : "—"} />
            <Fact label={t("maint.sourceEms")} value={m.ems_version || "—"} />
            <Fact label={t("maint.revision")} value={[m.git_branch, m.git_commit_short ? `@ ${m.git_commit_short}` : ""].filter(Boolean).join(" ") || "—"} />
            <Fact label={t("maint.format")} value={m.backup_format || "—"} />
            <Fact label={t("maint.encryption")} value={m.encrypted ? m.encryption_method || t("common.yes") : t("maint.none")} />
          </div>
          <h4 class="sub">{t("maint.contents", { n: (m.files || []).length })}</h4>
          <ul class="files">
            {#each m.files || [] as file, index (index)}
              <li><span class="mono">{file.path}</span><span class="muted">{kindLabel(file.kind)}</span>
                {#if file.sensitive}<span class="tag warn-tag">{t("maint.secret")}</span>{/if}
                {#if file.privacy_relevant}<span class="tag">{t("maint.privacy")}</span>{/if}</li>
            {/each}
          </ul>
        {/if}
      </div>
    {/if}
  </Card>

  <Card title={t("maint.restoreTitle")} subtitle={t("maint.restoreSub")} span="span-6">
    <div class="form">
      <label class="field"><span>{t("maint.backup")}</span>
        <select class="input" bind:value={restoreFile} onchange={() => (plan = null)}>
          <option value="">{t("maint.select")}</option>
          {#each backups as item (item.name)}<option value={item.name}>{item.name}</option>{/each}
        </select>
      </label>
      <label class="field"><span>{t("maint.password")}</span>
        <input class="input" type="password" autocomplete="off" bind:value={restorePassword} placeholder={t("maint.passwordHint")} />
      </label>
      <div class="row-wrap">
        <button class="btn secondary small" type="button" disabled={running || !restoreFile} onclick={preview}>{t("maint.preview")}</button>
        <button class="btn danger small" type="button" disabled={running || !plan} onclick={() => (confirm = { open: true, kind: "restore" })}>{t("maint.confirmRestore")}</button>
      </div>
    </div>
    {#if plan}
      <div class="inner plan" data-testid="restore-plan">
        <div class="sub">{t("maint.planFor", { file: plan.file || restoreFile, type: typeLabel(plan.backup_type) })}</div>
        <ul class="files">
          {#each plan.actions || [] as action, index (index)}
            <li><span class="tag">{action.action}</span><span class="mono">{action.path}</span>{#if action.source_path}<span class="muted">{t("maint.keptOutside", { path: action.source_path })}</span>{/if}</li>
          {:else}
            <li class="muted">{t("maint.noChanges")}</li>
          {/each}
        </ul>
        {#each plan.warnings || [] as warning, index (index)}<p class="warn">{warning}</p>{/each}
        {#if plan.requires_restart}<p class="muted">{t("maint.restartAfter")}</p>{/if}
        {#if plan.requires_relogin}<p class="muted">{t("maint.reloginAfter")}</p>{/if}
      </div>
    {/if}
  </Card>

  <Card title={t("maint.upgrade")} subtitle={t("maint.upgradeSub")} span="span-6">
    {#if !upgrade}
      <p class="muted">{t("maint.upgradeHint")}</p>
    {:else if !upgrade.apply_available}
      <Status tone="c-ok" icon="circle-check" text={t("maint.upToDate")} />
    {:else}
      <p>{valueCount ? t("maint.valueChanges", { n: valueCount }) : t("maint.valuesCurrent")}</p>
      {#each [["add", "maint.added"], ["migrate", "maint.migrated"], ["comment_add", "maint.comments"], ["comment_refresh", "maint.commentRefresh"]] as [kind, label] (kind)}
        {@const rows = items.filter((item) => item.kind === kind)}
        {#if rows.length}
          <h3 class="sub">{t(label)}</h3>
          <ul class="files">
            {#each rows as item, index (index)}
              <li><code>{item.path}</code>{#if kind === "add"}<span class="muted"> = {JSON.stringify(item.value)}</span>{:else if kind === "migrate"}<span class="muted">: {JSON.stringify(item.old_value)} → {JSON.stringify(item.value)}</span>{/if}</li>
            {/each}
          </ul>
        {/if}
      {/each}
      {#if upgrade.format_changed}<p class="muted">{t("maint.formatChanged")}</p>{/if}
      <p class="muted">{t("maint.upgradeNote")}</p>
    {/if}
    <div class="row-wrap">
      <button class="btn secondary small" type="button" onclick={checkUpgrade}>{t("maint.check")}</button>
      <button class="btn small" type="button" disabled={running || !upgrade?.apply_available} onclick={() => (confirm = { open: true, kind: "upgrade" })}>{t("maint.apply")}</button>
    </div>
  </Card>
</OperatorGate>

<ConfirmDialog
  open={confirm.open}
  title={confirm.kind === "restore" ? t("maint.restoreTitle") : t("maint.upgrade")}
  lines={[confirm.kind === "restore" ? t("maint.confirmRestoreText", { file: restoreFile }) : t("maint.confirmUpgradeText")]}
  confirmLabel={confirm.kind === "restore" ? t("maint.confirmRestore") : t("maint.apply")}
  danger={confirm.kind === "restore"}
  onconfirm={() => (confirm.kind === "restore" ? restore() : applyUpgrade())}
  oncancel={() => (confirm = { open: false, kind: "" })}
/>

<style>
  .message { padding: var(--s3) var(--s4); border-radius: var(--m-radius-inner); background: var(--m-surface-2); border: 1px solid var(--m-line-strong); }
  .notes { margin: 0; padding-left: 18px; display: flex; flex-direction: column; gap: 6px; color: var(--m-muted); font-size: 13px; }
  .warn { display: flex; gap: 8px; align-items: center; color: var(--m-warn); font-size: 12.5px; margin: 0 0 var(--s3); }
  .row-wrap { margin: var(--s3) 0; }
  .mono { font-family: var(--m-mono); font-size: 12px; overflow-wrap: anywhere; }
  .detail, .plan { margin-top: var(--s4); display: flex; flex-direction: column; gap: var(--s2); }
  .sub { font-size: 13px; font-weight: 620; margin: var(--s2) 0 0; }
  .files { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; font-size: 12.5px; }
  .files li { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  .tag { font-size: 11px; padding: 1px 7px; border-radius: 99px; background: var(--m-surface-3); color: var(--m-muted); }
  .warn-tag { color: var(--m-warn); }
  .form { display: flex; flex-direction: column; gap: var(--s3); }
  .skeleton { height: 100px; }
  code { font-family: var(--m-mono); font-size: 12px; }
</style>
