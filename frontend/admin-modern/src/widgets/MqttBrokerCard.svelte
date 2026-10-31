<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import Fact from "../../../shared/ui/Fact.svelte";
  import ConfirmDialog from "../../../shared/ui/ConfirmDialog.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { errorView } from "../lib/errors.js";
  import { BROKER_ISSUES, BUILTIN_BROKER, CONNECT_TYPES, brokerState, connectSteps } from "../lib/mqtt.js";
  import { mqtt, loadBroker, enableBroker, stopBroker, saveBroker, setPassword, revealPassword, waitForDevice, newGroup, setUpGroup, watchTopics } from "../state/mqtt.svelte.js";

  /** onsetup: called after a new device was handed to the mapping, so the page can show it. */
  let { onsetup = null } = $props();

  const TONES = { off: "c-idle", running: "c-ok", stopped: "c-warn", unknown: "c-warn" };

  let confirming = $state("");
  let port = $state("");
  let name = $state("");
  let prefix = $state("");
  let bridge = $state(false);
  let zendure = $state(false);
  let newPassword = $state("");
  let passwordFor = $state("");
  let shown = $state.raw({});
  let removing = $state("");
  let connecting = $state(false);
  let deviceType = $state("shelly_gen2");
  let connectAccount = $state("");
  let copied = $state("");
  let copyFailed = $state(false);
  let copiedPassword = $state.raw(null);

  const view = $derived(mqtt.broker);
  const settings = $derived(view?.settings || null);
  const state = $derived(brokerState(view));
  const busy = $derived(Boolean(mqtt.busy));
  const impact = $derived(view?.stop_impact || { external_devices: [], zendure_devices: [], grid_meter: false });
  const issues = $derived([...(view?.issues || []), ...(mqtt.error?.payload?.issues || []), ...(mqtt.result?.answer?.issues || [])]);
  const failure = $derived(mqtt.error && !mqtt.error.payload?.issues?.length ? errorView(mqtt.error.error, t) : null);
  const address = $derived(view?.host_address ? `${view.host_address}:${settings?.port || 1883}` : "");

  $effect(() => {
    loadBroker();
  });

  $effect(() => (mqtt.session && mqtt.waiting ? watchTopics() : undefined));

  const fresh = $derived(newGroup());
  const account = $derived(connectAccount || view?.accounts?.[0]?.name || "");

  /** The clipboard exists only over HTTPS or on localhost; elsewhere the value stays on screen to copy by hand. */
  async function copy(what, text) {
    try {
      await navigator.clipboard.writeText(String(text));
      copied = what;
      copyFailed = false;
      return true;
    } catch {
      copied = "";
      copyFailed = true;
      return false;
    }
  }

  async function copyPassword() {
    const answer = await revealPassword(account);
    if (!answer?.password) return;
    copiedPassword = (await copy("password", answer.password)) ? null : { account, password: answer.password };
  }

  function setUp(group) {
    setUpGroup(group);
    onsetup?.();
  }

  $effect(() => {
    if (settings) port = String(settings.port);
  });

  function issueText(issue) {
    return BROKER_ISSUES.includes(issue.code) ? t(`mqtt.code.${issue.code}`) : issue.message;
  }

  const stopLines = $derived([
    t("mqtt.broker.stopLead"),
    ...impact.external_devices.map((device) => t("mqtt.broker.stopExternal", { name: device.name })),
    ...impact.zendure_devices.map((device) =>
      device.held_output_limit_w == null
        ? t("mqtt.broker.stopZendure", { name: device.name })
        : t("mqtt.broker.stopZendureHeld", { name: device.name, watts: device.held_output_limit_w }),
    ),
    ...(impact.grid_meter ? [t("mqtt.broker.stopMeter")] : []),
  ]);

  function accounts() {
    return (view?.accounts || []).map((account) => ({ name: account.name, topic_prefix: account.topic_prefix, bridge: account.bridge, zendure: account.zendure }));
  }

  async function addAccount(event) {
    event.preventDefault();
    if (!name.trim()) return;
    const answer = await saveBroker({ accounts: [...accounts(), { name: name.trim(), topic_prefix: prefix.trim() || null, bridge, zendure }] });
    if (answer?.ok) {
      const added = name.trim();
      name = prefix = "";
      bridge = zendure = false;
      await show(added);
    }
  }

  async function removeAccount() {
    const gone = removing;
    removing = "";
    await saveBroker({ accounts: accounts().filter((account) => account.name !== gone) });
  }

  async function show(account) {
    const answer = await revealPassword(account);
    if (answer?.password) shown = { ...shown, [account]: answer.password };
  }

  async function renew(account, value) {
    const answer = await setPassword(account, value || "");
    if (answer?.password) shown = { ...shown, [account]: answer.password };
    newPassword = "";
  }

  function savePort(event) {
    event.preventDefault();
    const value = Number(port);
    if (Number.isInteger(value)) saveBroker({ port: value });
  }
</script>

<div class="broker" data-testid="mqtt-broker">
  <div class="row-wrap head">
    <Status tone={TONES[state]} text={t(`mqtt.state.${state}`)} />
    {#if state === "off" || state === "unknown"}
      <button class="btn small" type="button" onclick={() => (confirming = "enable")} disabled={busy || !view} data-testid="mqtt-broker-enable"><Icon name="power" size="sm" />{t("mqtt.broker.enable")}</button>
    {:else}
      <button class="btn secondary small" type="button" onclick={() => (confirming = "stop")} disabled={busy} data-testid="mqtt-broker-stop"><Icon name="square" size="sm" />{t("mqtt.broker.stop")}</button>
    {/if}
    <button class="icon-btn" type="button" onclick={loadBroker} aria-label={t("common.refresh")} title={t("common.refresh")}><Icon name="refresh-cw" /></button>
  </div>
  <p class="muted small">{t("mqtt.broker.lead")}</p>

  {#if settings}
    <div class="facts">
      <Fact label={t("mqtt.broker.address")} value={address || t("mqtt.broker.noAddress")} />
      <Fact label={t("mqtt.broker.anonymous")} value={settings.anonymous ? t("mqtt.broker.anonymousOn") : t("mqtt.broker.anonymousOff")} />
    </div>
    <div class="row-wrap">
      <form class="row-wrap" onsubmit={savePort}>
        <label class="field"><span class="small muted">{t("mqtt.broker.port")}</span><input class="input port" type="number" min="1" max="65535" bind:value={port} data-testid="mqtt-broker-port" /></label>
        <button class="btn secondary small" type="submit" disabled={busy || port === String(settings.port)}><Icon name="save" size="sm" />{t("mqtt.broker.savePort")}</button>
      </form>
      <button class="btn secondary small" type="button" onclick={() => saveBroker({ anonymous: !settings.anonymous })} disabled={busy} data-testid="mqtt-broker-anonymous">
        <Icon name={settings.anonymous ? "lock" : "lock-open"} size="sm" />{settings.anonymous ? t("mqtt.broker.anonymousDisable") : t("mqtt.broker.anonymousEnable")}
      </button>
    </div>
    <p class="muted small">{t("mqtt.broker.protected")}</p>

    <h3 class="sub">{t("mqtt.broker.accounts")}</h3>
    <ul class="list" data-testid="mqtt-broker-accounts">
      {#each view.accounts as account (account.name)}
        <li class="inner account">
          <div class="words">
            <strong>{account.name}</strong>
            <span class="muted small">
              {[account.topic_prefix ? t("mqtt.broker.prefixOnly", { prefix: account.topic_prefix }) : t("mqtt.broker.everyTopic"), account.bridge ? t("mqtt.broker.bridge") : "", account.zendure ? t("mqtt.broker.zendure") : ""].filter(Boolean).join(" · ")}
            </span>
            {#if shown[account.name]}<code class="secret" data-testid="mqtt-account-password">{shown[account.name]}</code>{/if}
          </div>
          {#if !account.password_saved}<Status tone="c-warn" text={t("mqtt.broker.noPassword")} />{/if}
          <div class="row-wrap">
            <button class="btn secondary small" type="button" onclick={() => show(account.name)} disabled={busy || !account.password_saved}><Icon name="eye" size="sm" />{t("mqtt.broker.showPassword")}</button>
            <button class="btn secondary small" type="button" onclick={() => renew(account.name)} disabled={busy}><Icon name="refresh-cw" size="sm" />{t("mqtt.broker.newPassword")}</button>
            <button class="btn secondary small" type="button" onclick={() => (removing = account.name)} disabled={busy}><Icon name="trash-2" size="sm" />{t("devices.remove")}</button>
          </div>
        </li>
      {/each}
    </ul>
    <form class="form" onsubmit={addAccount}>
      <input class="input" type="text" placeholder={t("mqtt.broker.accountName")} aria-label={t("mqtt.broker.accountName")} bind:value={name} data-testid="mqtt-account-name" />
      <input class="input" type="text" placeholder={t("mqtt.broker.prefix")} aria-label={t("mqtt.broker.prefix")} bind:value={prefix} />
      <label class="check"><input type="checkbox" bind:checked={bridge} /><span>{t("mqtt.broker.bridge")}</span></label>
      <label class="check"><input type="checkbox" bind:checked={zendure} /><span>{t("mqtt.broker.zendure")}</span></label>
      <button class="btn secondary small" type="submit" disabled={busy || !name.trim()} data-testid="mqtt-account-add"><Icon name="plus" size="sm" />{t("mqtt.broker.addAccount")}</button>
    </form>
    <form class="form" onsubmit={(event) => { event.preventDefault(); const target = passwordFor || view.accounts[0]?.name; if (target && newPassword) renew(target, newPassword); }}>
      <select class="input" value={passwordFor || view.accounts[0]?.name || ""} onchange={(event) => (passwordFor = event.currentTarget.value)} aria-label={t("mqtt.broker.accounts")} disabled={!view.accounts.length}>
        {#each view.accounts as account (account.name)}<option value={account.name}>{account.name}</option>{/each}
      </select>
      <input class="input" type="password" autocomplete="new-password" placeholder={t("mqtt.broker.ownPassword", { name: passwordFor || view.accounts[0]?.name || "" })} aria-label={t("mqtt.broker.ownPassword", { name: passwordFor || view.accounts[0]?.name || "" })} bind:value={newPassword} disabled={!view.accounts.length} />
      <button class="btn secondary small" type="submit" disabled={busy || !newPassword || !view.accounts.length}><Icon name="key" size="sm" />{t("mqtt.broker.setPassword")}</button>
    </form>
    {#if view.grid_meter_topic}
      <label class="field">
        <span class="small muted">{t("mqtt.broker.gridMeterAccount", { topic: view.grid_meter_topic })}</span>
        <select class="input" value={settings.grid_meter_account || ""} onchange={(event) => saveBroker({ grid_meter_account: event.currentTarget.value || null })} disabled={busy}>
          <option value="">{t("mqtt.broker.noGridMeterAccount")}</option>
          {#each view.accounts as account (account.name)}<option value={account.name}>{account.name}</option>{/each}
        </select>
      </label>
    {/if}
    <p class="muted small">{t("mqtt.broker.connectHint")}</p>
    {#if state === "running"}
      <div class="row-wrap"><button class="btn small" type="button" onclick={() => (connecting = !connecting)} aria-expanded={connecting ? "true" : "false"} data-testid="mqtt-connect"><Icon name="plug-zap" size="sm" />{t("mqtt.connect.title")}</button></div>
    {/if}
    {#if connecting}
      <section class="connect inner" data-testid="mqtt-connect-guide">
        <div class="row-wrap" role="radiogroup" aria-label={t("mqtt.connect.type")}>
          {#each CONNECT_TYPES as type (type)}
            <label class="check"><input type="radio" name="mqtt-device-type" value={type} checked={deviceType === type} onchange={() => (deviceType = type)} /><span>{t(`mqtt.connect.type.${type}`)}</span></label>
          {/each}
        </div>
        <ol class="steps">
          {#each connectSteps(deviceType) as key (key)}<li>{t(key)}</li>{/each}
        </ol>
        {#if view.accounts.length > 1}
          <label class="field"><span class="small muted">{t("mqtt.broker.accounts")}</span>
            <select class="input" value={account} onchange={(event) => (connectAccount = event.currentTarget.value)}>{#each view.accounts as item (item.name)}<option value={item.name}>{item.name}</option>{/each}</select>
          </label>
        {/if}
        <div class="copies">
          {#each [["address", t("mqtt.connect.server"), view.host_address || ""], ["port", t("mqtt.broker.port"), String(settings.port)], ["user", t("mqtt.connect.user"), settings.anonymous && !account ? "" : account]] as [what, label, value] (what)}
            <div class="copy inner"><span class="small muted">{label}</span><code>{value || "—"}</code><button class="btn secondary small fit" type="button" onclick={() => copy(what, value)} disabled={!value}>{copied === what ? t("mqtt.connect.copied") : t("mqtt.connect.copy")}</button></div>
          {/each}
          <div class="copy inner"><span class="small muted">{t("mqtt.connect.password")}</span><code data-testid="mqtt-connect-password">{copiedPassword?.account === account ? copiedPassword.password : "••••••"}</code><button class="btn secondary small fit" type="button" onclick={copyPassword} disabled={busy || !account}>{copied === "password" ? t("mqtt.connect.copied") : t("mqtt.connect.copy")}</button></div>
        </div>
        {#if copyFailed}<p class="note warn small" data-testid="mqtt-connect-copy-failed"><Icon name="triangle-alert" size="sm" />{t("mqtt.connect.copyFailed")}</p>{/if}
        {#if fresh}
          <p class="note ok" data-testid="mqtt-connect-found"><Icon name="circle-check" />{t("mqtt.connect.found", { key: fresh.key })}
            <button class="btn small" type="button" onclick={() => setUp(fresh)} data-testid="mqtt-connect-setup">{t("mqtt.connect.setUp")}</button>
          </p>
        {:else if mqtt.waiting}
          <p class="muted small" data-testid="mqtt-connect-waiting"><Icon name="loader" size="sm" />{t("mqtt.connect.waiting")}</p>
        {:else}
          <div class="row-wrap"><button class="btn secondary small" type="button" onclick={() => waitForDevice(BUILTIN_BROKER)} disabled={busy} data-testid="mqtt-connect-wait"><Icon name="radio-tower" size="sm" />{t("mqtt.connect.wait")}</button></div>
        {/if}
      </section>
    {/if}
  {/if}

  {#each issues as issue, index (`${issue.code}-${index}`)}
    <p class="form-error"><Icon name="triangle-alert" size="sm" /><span lang={BROKER_ISSUES.includes(issue.code) ? undefined : "en"}>{issueText(issue)}</span></p>
  {/each}
  {#if failure}<p class="form-error"><Icon name="triangle-alert" size="sm" /><span lang={failure.key ? undefined : "en"}>{failure.text}</span></p>{/if}
  {#if mqtt.result?.answer && !mqtt.result.answer.ok && mqtt.result.answer.message && !mqtt.result.answer.issues?.length}
    <p class="form-error"><Icon name="triangle-alert" size="sm" /><span lang="en">{mqtt.result.answer.message}</span></p>
  {/if}
</div>

<ConfirmDialog
  open={confirming === "enable"}
  title={t("mqtt.broker.enableTitle")}
  lines={[t("mqtt.broker.enableLine"), t("mqtt.broker.enableAccount")]}
  confirmLabel={t("mqtt.broker.enable")}
  onconfirm={() => { confirming = ""; enableBroker(); }}
  oncancel={() => (confirming = "")}
/>
<ConfirmDialog
  open={confirming === "stop"}
  title={t("mqtt.broker.stopTitle")}
  lines={stopLines}
  confirmLabel={t("mqtt.broker.stop")}
  danger={impact.zendure_devices.length > 0}
  onconfirm={() => { confirming = ""; stopBroker(); }}
  oncancel={() => (confirming = "")}
/>
<ConfirmDialog
  open={Boolean(removing)}
  title={t("mqtt.broker.removeTitle")}
  lines={[t("mqtt.broker.removeLine", { name: removing })]}
  confirmLabel={t("devices.remove")}
  danger
  onconfirm={removeAccount}
  oncancel={() => (removing = "")}
/>

<style>
  .broker { display: grid; gap: var(--s3); }
  .head { align-items: center; }
  .facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr)); gap: var(--s2); }
  .list { list-style: none; margin: 0; padding: 0; display: grid; gap: var(--s2); }
  .account { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: var(--s2); align-items: center; }
  .account .row-wrap { grid-column: 1 / -1; }
  .words { display: grid; gap: 2px; min-width: 0; }
  .secret { overflow-wrap: anywhere; user-select: all; }
  .form { display: flex; flex-wrap: wrap; gap: var(--s2); align-items: center; }
  .field { display: grid; gap: 2px; }
  .port { width: 7rem; }
  .check { display: inline-flex; gap: var(--s1); align-items: center; }
  .connect { display: grid; gap: var(--s2); }
  .steps { margin: 0; padding-left: 1.25rem; display: grid; gap: var(--s1); }
  .copies { display: grid; grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr)); gap: var(--s2); }
  .copy { display: grid; gap: 2px; min-width: 0; }
  .copy code { overflow-wrap: anywhere; }
  .fit { justify-self: start; }
</style>
