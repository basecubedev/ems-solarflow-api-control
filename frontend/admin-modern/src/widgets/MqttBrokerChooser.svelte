<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import ConfirmDialog from "../../../shared/ui/ConfirmDialog.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { BROKER_CHOICE_ERRORS, brokerAddress, preferredBroker } from "../lib/mqtt.js";
  import { mqtt, loadBrokers, searchBrokers, checkBroker, useBrokerAccount, addBroker, enableBroker } from "../state/mqtt.svelte.js";

  /**
   * value: the ref of the broker the live topics and a new mapping use.
   * locked: true while live topics run, so the broker they read stays put.
   */
  let { value = $bindable(""), locked = false } = $props();

  const TONES = { running: "c-ok", off: "c-idle", stopped: "c-warn", anonymous: "c-ok", account: "c-ok", needs_account: "c-warn", untrusted: "c-warn", unreachable: "c-warn", unchecked: "c-idle" };
  const ACTIONS = ["search", "check", "account", "manual", "brokers"];
  const EMPTY_FORM = { host: "", port: "1883", tls: false, tls_insecure: false, username: "", password: "" };

  let confirming = $state(false);
  let accountFor = $state("");
  let username = $state("");
  let password = $state("");
  let manual = $state(false);
  let form = $state({ ...EMPTY_FORM });

  const brokers = $derived(mqtt.brokers?.brokers || []);
  const busy = $derived(Boolean(mqtt.busy));
  const searching = $derived(mqtt.busy === "search" || Boolean(mqtt.brokers?.searching));
  const anyUsable = $derived(brokers.some((item) => item.usable));
  const failure = $derived(ACTIONS.includes(mqtt.error?.name) ? mqtt.error.payload?.error || "" : "");
  const searched = $derived(mqtt.result?.name === "search" && mqtt.result.answer?.ok ? mqtt.result.answer : null);

  $effect(() => {
    loadBrokers();
  });

  $effect(() => {
    if (!mqtt.brokers || locked) return;
    const next = preferredBroker(brokers, value);
    if (next !== value) value = next;
  });

  function title(item) {
    if (item.kind === "builtin") return t("mqtt.mapping.builtin");
    return brokerAddress(item) || item.ref;
  }

  function detail(item) {
    if (item.kind === "builtin") return brokerAddress(item);
    if (item.kind === "profile") return t("mqtt.brokers.profile", { name: item.ref });
    if (item.kind === "saved") return t("mqtt.brokers.saved");
    return t(item.origin === "manual" ? "mqtt.brokers.manual" : "mqtt.brokers.found");
  }

  function stateOf(item) {
    return item.kind === "builtin" ? item.state : item.access;
  }

  function stateText(item) {
    return item.kind === "builtin" ? t(`mqtt.state.${item.state}`) : t(`mqtt.brokers.access.${item.access}`);
  }

  function errorText(code) {
    return BROKER_CHOICE_ERRORS.includes(code) ? t(`mqtt.code.${code}`) : t("mqtt.brokers.failed");
  }

  function askAccount(ref) {
    accountFor = accountFor === ref ? "" : ref;
    username = password = "";
  }

  async function submitAccount(event, ref) {
    event.preventDefault();
    const answer = await useBrokerAccount(ref, username, password);
    if (answer?.ok) {
      accountFor = "";
      username = password = "";
      value = ref;
    }
  }

  async function submitManual(event) {
    event.preventDefault();
    const answer = await addBroker({
      host: form.host.trim(),
      port: Number(form.port),
      tls: form.tls,
      tls_insecure: form.tls && form.tls_insecure,
      username: form.username,
      password: form.password,
    });
    if (answer?.ok) {
      manual = false;
      form = { ...EMPTY_FORM };
      value = answer.ref;
    }
  }
</script>

<fieldset class="chooser" disabled={locked} data-testid="mqtt-broker-chooser">
  <legend class="small muted">{t("mqtt.mapping.broker")}</legend>
  <ul class="list">
    {#each brokers as item (item.ref)}
      <li class="inner choice" class:chosen={value === item.ref} data-kind={item.kind} data-ref={item.ref} data-testid="mqtt-broker-choice">
        <label class="pick">
          <input type="radio" name="mqtt-mapping-broker" value={item.ref} checked={value === item.ref} disabled={!item.usable} onchange={() => (value = item.ref)} />
          <span class="words">
            <strong>{title(item)}</strong>
            {#if detail(item)}<span class="muted small">{detail(item)}</span>{/if}
          </span>
        </label>
        <Status tone={TONES[stateOf(item)] || "c-idle"} text={stateText(item)} />
        {#if item.kind === "builtin" && item.state === "off"}
          <button class="btn small fit" type="button" onclick={() => (confirming = true)} disabled={busy} data-testid="mqtt-chooser-enable"><Icon name="power" size="sm" />{t("mqtt.broker.enable")}</button>
        {:else if (item.kind === "found" || item.kind === "saved") && item.access === "needs_account"}
          <button class="btn secondary small fit" type="button" onclick={() => askAccount(item.ref)} aria-expanded={accountFor === item.ref ? "true" : "false"} disabled={busy} data-testid="mqtt-chooser-account"><Icon name="key" size="sm" />{t("mqtt.brokers.enterAccount")}</button>
        {:else if item.kind === "saved" || (item.kind === "found" && !item.usable)}
          <button class="btn secondary small fit" type="button" onclick={() => checkBroker(item.ref)} disabled={busy} data-testid="mqtt-chooser-check"><Icon name={mqtt.busy === "check" ? "loader" : "refresh-cw"} size="sm" />{t("mqtt.brokers.check")}</button>
        {/if}
        {#if item.access === "untrusted"}<p class="muted small fit">{t("mqtt.brokers.untrustedHint")}</p>{/if}
        {#if accountFor === item.ref}
          <form class="form wide" onsubmit={(event) => submitAccount(event, item.ref)}>
            <input class="input" type="text" autocomplete="username" placeholder={t("mqtt.connect.user")} aria-label={t("mqtt.connect.user")} bind:value={username} data-testid="mqtt-chooser-user" />
            <input class="input" type="password" autocomplete="current-password" placeholder={t("mqtt.connect.password")} aria-label={t("mqtt.connect.password")} bind:value={password} data-testid="mqtt-chooser-password" />
            <button class="btn small" type="submit" disabled={busy || !username.trim() || !password} data-testid="mqtt-chooser-account-use"><Icon name={mqtt.busy === "account" ? "loader" : "circle-check"} size="sm" />{t("mqtt.brokers.useAccount")}</button>
          </form>
        {/if}
      </li>
    {/each}
  </ul>

  {#if mqtt.brokers && !anyUsable}
    <p class="note warn" data-testid="mqtt-chooser-none"><Icon name="triangle-alert" />{t("mqtt.brokers.none")}</p>
  {/if}

  <div class="row-wrap">
    <button class="btn secondary small" type="button" onclick={searchBrokers} disabled={busy || searching} data-testid="mqtt-chooser-search"><Icon name={searching ? "loader" : "search"} size="sm" />{searching ? t("mqtt.brokers.searching") : t("mqtt.brokers.search")}</button>
    <button class="btn secondary small" type="button" onclick={() => (manual = !manual)} aria-expanded={manual ? "true" : "false"} disabled={busy} data-testid="mqtt-chooser-manual"><Icon name="plus" size="sm" />{t("mqtt.brokers.add")}</button>
  </div>
  {#if searching}<p class="muted small">{t("mqtt.brokers.searchingHint")}</p>{/if}
  {#if searched && !searching}<p class="muted small" data-testid="mqtt-chooser-searched">{t("mqtt.brokers.searchDone", { count: searched.found })}</p>{/if}

  {#if manual}
    <form class="manual inner" onsubmit={submitManual} data-testid="mqtt-chooser-manual-form">
      <label class="field"><span class="small muted">{t("mqtt.brokers.host")}</span><input class="input" type="text" placeholder="192.168.1.20" bind:value={form.host} data-testid="mqtt-chooser-host" /></label>
      <label class="field"><span class="small muted">{t("mqtt.broker.port")}</span><input class="input port" type="number" min="1" max="65535" bind:value={form.port} data-testid="mqtt-chooser-port" /></label>
      <label class="check"><input type="checkbox" bind:checked={form.tls} /><span>{t("mqtt.brokers.tls")}</span></label>
      {#if form.tls}<label class="check"><input type="checkbox" bind:checked={form.tls_insecure} /><span>{t("mqtt.brokers.tlsInsecure")}</span></label>{/if}
      <label class="field"><span class="small muted">{t("mqtt.connect.user")}</span><input class="input" type="text" autocomplete="username" bind:value={form.username} /></label>
      <label class="field"><span class="small muted">{t("mqtt.connect.password")}</span><input class="input" type="password" autocomplete="current-password" bind:value={form.password} /></label>
      <button class="btn small fit" type="submit" disabled={busy || !form.host.trim()} data-testid="mqtt-chooser-manual-add"><Icon name={mqtt.busy === "manual" ? "loader" : "circle-check"} size="sm" />{t("mqtt.brokers.addSubmit")}</button>
      <p class="muted small">{t("mqtt.brokers.manualHint")}</p>
    </form>
  {/if}

  {#if failure}<p class="form-error" data-testid="mqtt-chooser-error"><Icon name="triangle-alert" size="sm" /><span>{errorText(failure)}</span></p>{/if}
</fieldset>

<ConfirmDialog
  open={confirming}
  title={t("mqtt.broker.enableTitle")}
  lines={[t("mqtt.broker.enableLine"), t("mqtt.broker.enableAccount")]}
  confirmLabel={t("mqtt.broker.enable")}
  onconfirm={() => { confirming = false; enableBroker(); }}
  oncancel={() => (confirming = false)}
/>

<style>
  .chooser { border: 0; margin: 0; padding: 0; display: grid; gap: var(--s2); min-width: 0; }
  .list { list-style: none; margin: 0; padding: 0; display: grid; gap: var(--s2); }
  .choice { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: var(--s2); align-items: center; }
  .choice.chosen { outline: 2px solid var(--accent, currentColor); outline-offset: -2px; }
  .choice .fit, .choice .wide { grid-column: 1 / -1; justify-self: start; }
  .pick { display: flex; gap: var(--s2); align-items: center; min-width: 0; }
  .words { display: grid; gap: 2px; min-width: 0; overflow-wrap: anywhere; }
  .form { display: flex; flex-wrap: wrap; gap: var(--s2); align-items: center; }
  .manual { display: grid; grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr)); gap: var(--s2); align-items: end; }
  .manual .muted { grid-column: 1 / -1; }
  .field { display: grid; gap: 2px; }
  .port { width: 7rem; }
  .check { display: inline-flex; gap: var(--s1); align-items: center; }
  .fit { justify-self: start; }
</style>
