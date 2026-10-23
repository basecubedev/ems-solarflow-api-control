<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import KeyValues from "../widgets/KeyValues.svelte";
  import PlanButton from "../widgets/PlanButton.svelte";
  import { t, fmt, say, langOf } from "../i18n/i18n.svelte.js";
  import { host, ensure, held } from "../state/host.svelte.js";
  import { forms } from "../state/forms.svelte.js";
  import { detail } from "../state/prefs.svelte.js";
  import { EXPORT_ACCESS_STATUSES, EXPORT_STATES, confinementGaps, exportPathTone, exportSetupReport, exportTone, rescueSsh, rescueState, shellAccess } from "../lib/access.js";
  import { UNIT_STATES, word } from "../lib/words.js";

  /**
   * Key-based SSH for the owner, the console rescue account, and a read-only
   * account that copies files off this appliance. The console never enables
   * password logins and never handles a private key.
   */
  const status = $derived(host.status);
  const ssh = $derived(status?.ssh || {});
  const rescue = $derived(status?.system?.rescue || {});
  const backup = $derived(held("backup"));
  const account = $derived(backup?.account || {});
  const rescueVerdict = $derived(rescueState(rescue));
  const gaps = $derived(confinementGaps(backup));
  const setup = $derived(exportSetupReport(backup));
  const shell = $derived(shellAccess(ssh));
  const accounts = $derived((ssh.accounts || []).map((item) => item.name));
  const keyAccount = $derived(accounts.includes(forms.keyAccount) ? forms.keyAccount : accounts[0] || "");
  const yesNo = (value) => (value === true ? t("common.yes") : value === false ? t("common.no") : null);

  $effect(() => {
    if (host.backup === undefined) ensure("backup");
  });
</script>

<p class="callout c-acc span-12"><Icon name="key-round" /><span>{t("access.lead")}</span></p>

<Card title={t("access.ssh")} subtitle={t("access.sshSub")} span="span-6">
  {#snippet actions()}<Status tone={ssh.enabled ? "c-ok" : "c-idle"} text={ssh.enabled ? t("access.running") : t("access.stopped")} />{/snippet}
  <div class="body" data-testid="ssh-service">
    <KeyValues
      rows={[
        { label: t("access.unitState"), value: say(word("systemd", UNIT_STATES, ssh.service?.active)), lang: langOf(word("systemd", UNIT_STATES, ssh.service?.active)) },
        { label: t("access.passwordLogin"), value: ssh.hardening?.passwordauthentication?.value || ssh.password_authentication, lang: "en" },
      ]}
    />
    <div class="row-wrap" data-testid="ssh-stage-service">
      <PlanButton plan={{ key: "ssh-enable", endpoint: "/api/ssh/enable", title: { key: "plan.title.sshEnable" } }} label={t("access.enable")} icon="play" testid="ssh-enable" />
      <PlanButton plan={{ key: "ssh-disable", endpoint: "/api/ssh/disable", title: { key: "plan.title.sshDisable" }, danger: true }} label={t("access.disable")} icon="square" variant="secondary" testid="ssh-disable" />
    </div>
  </div>
</Card>

<Card title={t("access.rescue")} subtitle={rescue.account || "ems-rescue"} span="span-6">
  {#snippet actions()}<Status tone={rescueVerdict.tone} text={t(rescueVerdict.key)} />{/snippet}
  <div class="body" data-testid="rescue-account">
    <KeyValues
      rows={[
        { label: t("access.canLogIn"), value: yesNo(rescue.can_log_in) },
        { label: t("access.sshPassword"), value: t(`rescue.ssh.${rescueSsh(rescue.ssh)}`) },
        detail.expert && { label: t("access.shell"), value: rescue.shell, mono: true },
      ]}
    />
    <p class="note"><Icon name="info" />{t(`${rescueVerdict.key}.hint`, { account: rescue.account || "ems-rescue" })}</p>
  </div>
</Card>

<Card title={t("access.addKey")} subtitle={t("access.addKeySub")} span="span-6">
  <div class="body form" data-testid="ssh-stage-add">
    <label class="field">
      <span>{t("access.account")}</span>
      <select class="input" value={keyAccount} onchange={(event) => (forms.keyAccount = event.currentTarget.value)} data-testid="key-account">
        {#each accounts as name, index (index)}<option value={name}>{name}</option>{/each}
      </select>
    </label>
    <label class="field">
      <span>{t("access.publicKey")}</span>
      <textarea class="input mono" rows="4" placeholder="ssh-ed25519 AAAA... user@laptop" bind:value={forms.keyValue} data-testid="key-value"></textarea>
    </label>
    <p class="note warn"><Icon name="shield-alert" />{t("access.noPrivate")}</p>
    <div class="row-wrap">
      <PlanButton
        plan={{ key: "key-add", endpoint: "/api/ssh/keys", body: { account: keyAccount, public_key: forms.keyValue }, title: { key: "plan.title.keyAdd" }, confirm: "plan.addKey", forget: ["keyValue"] }}
        label={t("access.addKeyButton")}
        icon="plus"
        small={false}
        disabled={!keyAccount || !forms.keyValue.trim()}
        testid="key-add"
      />
    </div>
  </div>
</Card>

<Card title={t("access.shellAccess")} subtitle={t("access.shellAccessSub")} span="span-6">
  {#snippet actions()}<Status tone={shell.enabled ? "c-warn" : shell.confirmedOff ? "c-ok" : "c-warn"} text={shell.enabled ? t("access.shellOn") : shell.confirmedOff ? t("access.shellOff") : t("access.shellOffUnconfirmed")} />{/snippet}
  <div class="body" data-testid="ssh-stage-shell-access">
    <p class="callout c-warn" data-testid="shell-access-warning"><Icon name="shield-alert" /><span>{t("access.shellWarning")}</span></p>
    <KeyValues
      rows={[
        { label: t("access.account"), value: shell.account },
        { label: t("access.keyDeployment"), value: shell.keysAllowed ? t("access.keysAllowed") : t("access.keysRefused") },
      ]}
    />
    {#if !shell.enabled && !shell.confirmedOff}
      <p class="note warn" data-testid="shell-access-not-refused"><Icon name="triangle-alert" />{shell.daemonAccepts ? t("access.shellDaemonAccepts") : t("access.shellDaemonUnknown")}</p>
    {/if}
    {#if shell.homeBroken}<p class="note warn" data-testid="shell-access-home-broken"><Icon name="triangle-alert" />{t("access.shellHomeBroken")}</p>{/if}
    <div class="row-wrap">
      <PlanButton plan={{ key: "shell-enable", endpoint: "/api/ssh/shell-access/enable", title: { key: "plan.title.shellEnable" }, confirm: "plan.enable", danger: true }} label={t("access.shellEnable")} icon="unlock" disabled={shell.enabled} testid="shell-access-enable" />
      <PlanButton plan={{ key: "shell-disable", endpoint: "/api/ssh/shell-access/disable", title: { key: "plan.title.shellDisable" } }} label={t("access.shellDisable")} icon="lock" variant="secondary" disabled={!shell.enabled} testid="shell-access-disable" />
    </div>
  </div>
</Card>

{#each ssh.accounts || [] as item, index (index)}
  <Card title={t("access.keysFor", { account: item.name })} span="span-12">
    {#if !item.exists}
      <p class="muted small">{t("access.accountMissing", { account: item.name })}</p>
    {:else if !(item.keys || []).length}
      <p class="muted small" data-testid="ssh-no-keys">{t("access.noKeys")}</p>
    {:else}
      <div class="table-scroll">
        <table class="data" data-testid="ssh-key-table">
          <thead><tr><th>{t("access.keyType")}</th><th>{t("access.keyComment")}</th><th>{t("access.fingerprint")}</th><th class="r">{t("access.action")}</th></tr></thead>
          <tbody>
            {#each item.keys as key, row (row)}
              <tr>
                <td>{key.key_type}</td>
                <td>{key.comment || "—"}</td>
                <td class="mono">{key.fingerprint}</td>
                <td class="r">
                  <PlanButton
                    plan={{ key: `key-remove-${index}-${row}`, endpoint: "/api/ssh/keys/remove-plan", body: { account: item.name, fingerprint: key.fingerprint }, title: { key: "plan.title.keyRemove" }, confirm: "plan.remove", danger: true }}
                    label={t("access.remove")}
                    icon="trash-2"
                    variant="secondary"
                    testid="ssh-remove-key"
                  />
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </Card>
{/each}

<Card title={t("access.backupAccount")} subtitle={t("access.backupAccountSub")} span="span-6">
  {#snippet actions()}{#if backup && !backup.error}<Status tone={account.exists ? "c-ok" : "c-warn"} text={account.exists ? t("access.available") : t("access.notCreated")} />{/if}{/snippet}
  <div class="body" data-testid="backup-account">
    {#if backup === null || backup === undefined}
      <p class="muted small"><Icon name="loader" size="sm" /> {t("overview.reading")}</p>
    {:else if backup.error}
      <p class="form-error">{t("access.backupFailed", { code: backup.error })}</p>
    {:else}
      <KeyValues
        rows={[
          { label: t("access.account"), value: account.name },
          { label: t("access.authorizedKeys"), value: account.key_count },
          { label: t("access.protocol"), value: String(backup.protocol || "").toUpperCase() },
          { label: t("access.shellAccessFact"), value: yesNo(backup.shell_access) },
          { label: t("access.writeAccess"), value: yesNo(backup.write_access) },
        ]}
      />
    {/if}
  </div>
</Card>

<Card title={t("access.export")} subtitle={t("access.exportSub")} span="span-6">
  {#snippet actions()}
    {#if backup && !backup.error}<Status tone={exportTone(backup.export_access?.status)} text={say(word("exportAccess", EXPORT_ACCESS_STATUSES, backup.export_access?.status)) || t("common.unknown")} lang={langOf(word("exportAccess", EXPORT_ACCESS_STATUSES, backup.export_access?.status))} />{/if}
  {/snippet}
  <div class="body" data-testid="backup-export">
    {#if backup && !backup.error}
      <Status tone={backup.confined ? "c-ok" : "c-warn"} text={backup.confined ? t("access.confined") : t("access.notConfined")} />
      <KeyValues
        rows={[
          { label: t("access.detail"), value: backup.export_access?.detail, lang: "en" },
          gaps.unverified && { label: t("access.confinement"), value: t("access.confinementUnverified") },
          gaps.violations.length && { label: t("access.notEnforced"), value: gaps.violations.join(", "), mono: true },
          setup && { label: t("access.exportSetup"), value: setup.detail ? `${setup.status} — ${setup.detail}` : setup.status, lang: "en" },
          { label: t("access.exportRoot"), value: backup.export_root, mono: true },
          (backup.unmanaged_entries || []).length && { label: t("access.unmanaged"), value: backup.unmanaged_entries.join(", "), mono: true },
          detail.expert && { label: t("access.chroot"), value: backup.chroot?.configured, mono: true },
          detail.expert && { label: t("access.forcedCommand"), value: backup.chroot?.force_command, mono: true },
        ]}
      />
    {/if}
  </div>
</Card>

{#if backup && !backup.error && (backup.paths || []).length}
  <Card title={t("access.paths")} subtitle={t("access.pathsSub")} span="span-12">
    <div class="table-scroll">
      <table class="data" data-testid="backup-paths">
        <thead><tr><th>{t("access.pathName")}</th><th>{t("access.path")}</th><th>{t("access.access")}</th><th>{t("access.exportState")}</th><th class="r">{t("access.size")}</th></tr></thead>
        <tbody>
          {#each backup.paths as item, index (index)}
            <tr>
              <td>{item.name}</td>
              <td class="mono">{detail.expert ? item.path : `/${item.name}`}</td>
              <td lang="en">{item.access}</td>
              <td><Status tone={exportPathTone(item)} text={EXPORT_STATES.includes(item.state) ? t(`export.${item.state}`) : String(item.state || "—")} lang={item.state && !EXPORT_STATES.includes(item.state) ? "en" : undefined} /></td>
              <td class="r num">{item.exists ? `${fmt().number(item.size_mb, 1)} MB` : t("access.missing")}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  </Card>

  {#if (backup.examples || []).length}
    <Card title={t("access.copy")} subtitle={t("access.copySub")} span="span-12">
      <div class="examples">
        {#each backup.examples as example, index (index)}
          <div class="example" data-testid="backup-example">
            <p class="example-title" lang="en">{example.title}</p>
            <pre class="log">{example.command}</pre>
          </div>
        {/each}
      </div>
    </Card>
  {/if}
{/if}

<style>
  .body { display: flex; flex-direction: column; gap: var(--s3); align-items: stretch; }
  .body > :global(.status) { align-self: flex-start; }
  .body .note, .body .callout { margin: 0; }
  .form { gap: var(--s3); }
  .small { font-size: 13px; margin: 0; }
  .examples { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: var(--s3); }
  .example-title { margin: 0 0 var(--s2); font-size: 13px; font-weight: 600; }
</style>
