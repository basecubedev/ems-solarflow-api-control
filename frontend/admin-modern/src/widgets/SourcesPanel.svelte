<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import ConfirmDialog from "../../../shared/ui/ConfirmDialog.svelte";
  import { t, tn } from "../i18n/i18n.svelte.js";
  import { errorView } from "../lib/errors.js";
  import { sources, loadSources, saveCredential, removeCredential, refreshBrokers, saveCloudKey, testCloudKey, refreshCloud, forgetCloudKey } from "../state/sources.svelte.js";

  /** onchanged: called after an action that changes what the search can find. */
  let { onchanged = null } = $props();

  let label = $state("");
  let username = $state("");
  let password = $state("");
  let key = $state("");
  let removing = $state.raw(null);
  let forgetting = $state(false);
  let missingLabel = $state(false);

  const cloud = $derived(sources.cloud || {});
  const saved = $derived(Boolean(cloud.token_saved));
  const message = $derived(sources.message);
  const failure = $derived(message?.error ? errorView(message.error, t) : null);

  $effect(() => {
    loadSources();
  });

  async function save(event) {
    event.preventDefault();
    missingLabel = !label.trim();
    if (missingLabel) return;
    if (await saveCredential({ label: label.trim(), username, password })) {
      label = "";
      username = "";
      password = "";
      onchanged?.();
    }
  }

  async function saveKey(event) {
    event.preventDefault();
    if (!key.trim()) return;
    if (await saveCloudKey(key.trim())) {
      key = "";
      onchanged?.();
    }
  }
</script>

<div class="sources" data-testid="search-sources">
  <section class="source">
    <h3 class="sub">{t("sources.local.title")}</h3>
    <p class="muted small">{t("sources.local.lead")}</p>
    {#if sources.credentials?.length}
      <ul class="list" data-testid="credentials">
        {#each sources.credentials as credential (credential.id)}
          <li class="inner row">
            <div class="words">
              <strong>{credential.label || credential.id}</strong>
              <span class="muted small">{[credential.username_configured ? t("sources.local.username") : "", credential.password_configured ? t("sources.local.password") : ""].filter(Boolean).join(" · ") || t("sources.local.anonymous")}</span>
            </div>
            <Status tone={credential.credentials_encrypted ? "c-ok" : "c-warn"} text={credential.credentials_encrypted ? t("sources.local.encrypted") : t("sources.local.notEncrypted")} />
            <button class="btn secondary small" type="button" onclick={() => (removing = credential)} disabled={Boolean(sources.busy)}><Icon name="trash-2" size="sm" />{t("devices.remove")}</button>
          </li>
        {/each}
      </ul>
    {:else if sources.credentials}
      <p class="muted small">{t("sources.local.none")}</p>
    {/if}
    <form class="form" onsubmit={save}>
      <input class="input" type="text" placeholder={t("sources.local.label")} aria-label={t("sources.local.label")} bind:value={label} aria-invalid={missingLabel ? "true" : undefined} data-testid="credential-label" />
      <input class="input" type="text" autocomplete="off" placeholder={t("devices.field.username")} aria-label={t("devices.field.username")} bind:value={username} data-testid="credential-username" />
      <input class="input" type="password" autocomplete="new-password" placeholder={t("devices.field.password")} aria-label={t("devices.field.password")} bind:value={password} data-testid="credential-password" />
      <button class="btn secondary small" type="submit" disabled={Boolean(sources.busy)} data-testid="credential-save"><Icon name="save" size="sm" />{t("sources.local.save")}</button>
    </form>
    {#if missingLabel}<p class="form-error">{t("sources.local.labelRequired")}</p>{/if}
    <button class="btn secondary small" type="button" onclick={() => refreshBrokers().then(() => onchanged?.())} disabled={Boolean(sources.busy)} data-testid="brokers-refresh"><Icon name="refresh-cw" size="sm" />{t("sources.local.refresh")}</button>
  </section>

  <section class="source">
    <h3 class="sub">{t("sources.cloud.title")}</h3>
    <div class="facts two">
      <div class="fact"><div class="fact-label">{t("sources.cloud.key")}</div><div class="fact-value" data-testid="cloud-key-state">{saved ? t("sources.cloud.saved") : t("sources.cloud.notSaved")}</div></div>
      <div class="fact"><div class="fact-label">TLS</div><div class="fact-value num" lang="en">{cloud.tls_mode || "system_ca"}</div></div>
      <div class="fact"><div class="fact-label">{t("sources.cloud.broker")}</div><div class="fact-value num" lang="en">{cloud.last_broker || "—"}</div></div>
      <div class="fact"><div class="fact-label">{t("sources.cloud.lastStatus")}</div><div class="fact-value" lang="en">{cloud.last_status || "—"}</div></div>
    </div>
    {#if cloud.last_error}<p class="note warn"><Icon name="triangle-alert" /><span lang="en">{cloud.last_error}</span></p>{/if}
    {#if !saved}<p class="muted small">{t("sources.cloud.lead")}</p>{/if}
    <form class="form" onsubmit={saveKey}>
      <input class="input" type="password" autocomplete="off" placeholder={t("sources.cloud.placeholder")} aria-label={t("sources.cloud.placeholder")} bind:value={key} data-testid="cloud-key" />
      <button class="btn secondary small" type="submit" disabled={Boolean(sources.busy) || !key.trim()} data-testid="cloud-save"><Icon name="save" size="sm" />{saved ? t("sources.cloud.replace") : t("sources.cloud.save")}</button>
    </form>
    <div class="row-wrap actions">
      <button class="btn secondary small" type="button" onclick={() => testCloudKey(key.trim())} disabled={Boolean(sources.busy) || (!saved && !key.trim())} data-testid="cloud-test">{t("sources.cloud.test")}</button>
      <button class="btn secondary small" type="button" onclick={() => refreshCloud().then(() => onchanged?.())} disabled={Boolean(sources.busy) || !saved} data-testid="cloud-refresh"><Icon name="refresh-cw" size="sm" />{t("sources.cloud.refresh")}</button>
      {#if saved}<button class="btn secondary small" type="button" onclick={() => (forgetting = true)} disabled={Boolean(sources.busy)} data-testid="cloud-forget"><Icon name="trash-2" size="sm" />{t("sources.cloud.forget")}</button>{/if}
    </div>
    {#if sources.cloudDevices?.length}
      <ul class="list">
        {#each sources.cloudDevices as device, index (index)}
          <li class="inner"><strong lang="en">{device.display_name || device.device_name || t("discovery.mqttDevice")}</strong> <span class="muted small num">{device.serial_number || device.device_id || ""}</span></li>
        {/each}
      </ul>
    {/if}
  </section>

  {#if message}
    <p class={failure ? "form-error" : "form-ok"} role="status" data-testid="sources-message">
      {t(`sources.message.${message.kind}`, message.values || {})}{#if failure} <span lang={failure.lang}>{failure.text}</span>{/if}
    </p>
  {/if}
</div>

<ConfirmDialog
  open={Boolean(removing)}
  title={t("sources.local.removeTitle")}
  lines={[t("sources.local.removeLine")]}
  confirmLabel={t("devices.remove")}
  danger
  onconfirm={() => { const credential = removing; removing = null; if (credential) removeCredential(credential.id).then(() => onchanged?.()); }}
  oncancel={() => (removing = null)}
/>
<ConfirmDialog
  open={forgetting}
  title={t("sources.cloud.forgetTitle")}
  lines={[t("sources.cloud.forgetLine")]}
  confirmLabel={t("sources.cloud.forget")}
  danger
  onconfirm={() => { forgetting = false; forgetCloudKey().then(() => onchanged?.()); }}
  oncancel={() => (forgetting = false)}
/>

<style>
  .sources { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--s4) var(--s5); }
  .source { display: flex; flex-direction: column; gap: var(--s2); min-width: 0; }
  .sub { font-size: 13px; font-weight: 620; margin: 0; }
  .small { font-size: 12.5px; overflow-wrap: anywhere; }
  .list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--s2); }
  .row { display: flex; align-items: center; gap: var(--s3); flex-wrap: wrap; }
  .words { flex: 1 1 160px; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
  .form { display: flex; gap: var(--s2); flex-wrap: wrap; }
  .form .input { flex: 1 1 140px; }
  .actions { gap: var(--s2); }
  .sources > p { grid-column: 1 / -1; margin: 0; }
  @container content (max-width: 720px) { .sources { grid-template-columns: minmax(0, 1fr); } }
</style>
