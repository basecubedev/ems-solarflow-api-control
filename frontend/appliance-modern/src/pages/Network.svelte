<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import KeyValues from "../widgets/KeyValues.svelte";
  import PlanButton from "../widgets/PlanButton.svelte";
  import { t, say, langOf } from "../i18n/i18n.svelte.js";
  import { host, scanWifi } from "../state/host.svelte.js";
  import { actions as flow, planOperation } from "../state/actions.svelte.js";
  import { forms } from "../state/forms.svelte.js";
  import { detail } from "../state/prefs.svelte.js";
  import { CONNECTIVITY, DEVICE_STATES, word } from "../lib/words.js";

  /**
   * A WLAN change can disconnect this session. If the appliance is on a WLAN
   * already, that profile is kept and comes back by itself when the new
   * network fails.
   */
  const status = $derived(host.status);
  const network = $derived(status?.network || {});
  const currentZone = $derived(status?.system?.timezone || "");
  const scan = $derived(host.wifi);
  const networks = $derived((scan && scan.networks) || []);
  const timezone = $derived(forms.timezone ?? currentZone);

  let passphrase = $state("");

  /** A passphrase is never kept past the plan it was typed for, whether the plan came straight back or was picked up later. */
  $effect(() => {
    if (flow.pending?.options?.key === "wifi-plan") passphrase = "";
  });

  function planWifi() {
    planOperation({
      key: "wifi-plan",
      endpoint: "/api/network/wifi/plan",
      body: { ssid: forms.wifiSsid, passphrase, hidden: forms.wifiHidden },
      title: { key: "plan.title.wifi" },
      confirm: "plan.apply",
      danger: true,
      forget: ["wifiSsid", "wifiHidden"],
    });
  }

  const signal = (item) => (item.signal === null || item.signal === undefined ? "?" : `${item.signal} %`);
</script>

<Card icon="server" tone="c-out" title={t("network.hostname")} subtitle={network.hostname || t("common.unknown")} span="span-4">
  {#snippet actions()}{#if network.connectivity}<Status tone={network.connectivity === "full" ? "c-ok" : "c-warn"} text={say(word("connectivity", CONNECTIVITY, network.connectivity))} lang={langOf(word("connectivity", CONNECTIVITY, network.connectivity))} />{/if}{/snippet}
  <div data-testid="network-hostname">
    <KeyValues rows={[{ label: t("overview.mdns"), value: network.mdns }, { label: t("overview.activeConnection"), value: network.active_connection }]} />
  </div>
</Card>

{#each network.interfaces || [] as item, index (index)}
  <Card icon={item.type === "wifi" ? "wifi" : "network"} tone="c-out" title={item.device} subtitle={item.type === "wifi" ? t("network.wifi") : item.type === "ethernet" ? t("network.ethernet") : item.type} subtitleLang={item.type === "wifi" || item.type === "ethernet" ? undefined : "en"} span="span-4">
    {#snippet actions()}<Status tone={/^connected/.test(item.state || "") ? "c-ok" : "c-idle"} text={say(word("device", DEVICE_STATES, item.state)) || t("common.unknown")} lang={langOf(word("device", DEVICE_STATES, item.state))} />{/snippet}
    <div data-testid="network-interface">
      <KeyValues
        rows={[
          { label: t("network.addresses"), value: (item.addresses || []).join(", ") },
          item.type === "wifi" && { label: t("network.ssid"), value: item.ssid },
          item.type === "wifi" && { label: t("network.signal"), value: item.signal === null || item.signal === undefined ? null : `${item.signal} %` },
          detail.expert && { label: t("network.gateway"), value: item.gateway },
          detail.expert && { label: t("network.dns"), value: (item.dns || []).join(", ") },
        ]}
      />
    </div>
  </Card>
{/each}

<Card icon="wifi" tone="c-out" title={t("network.wifiChange")} subtitle={t("network.wifiChangeSub")} span="span-6">
  <form class="form" data-testid="network-wifi" onsubmit={(event) => { event.preventDefault(); planWifi(); }}>
    <p class="note warn"><Icon name="triangle-alert" />{t("network.wifiWarning")}</p>
    <div class="row-wrap">
      <button class="btn secondary small" type="button" onclick={scanWifi} disabled={scan === null} aria-busy={scan === null ? "true" : undefined} data-testid="wifi-scan">
        <Icon name={scan === null ? "loader" : "radar"} />{scan === null ? t("network.scanning") : t("network.scan")}
      </button>
      {#if scan?.error}<span class="form-error">{t("network.scanFailed", { code: scan.error })}</span>{/if}
    </div>
    {#if networks.length}
      <label class="field">
        <span>{t("network.visible")}</span>
        <select class="input" value={networks.some((item) => item.ssid === forms.wifiSsid) ? forms.wifiSsid : ""} onchange={(event) => (forms.wifiSsid = event.currentTarget.value)} data-testid="wifi-select">
          <option value="">{t("network.choose")}</option>
          {#each networks as item, index (`${item.ssid}-${index}`)}<option value={item.ssid}>{item.ssid} · {signal(item)} · {item.security || "—"}{item.active ? ` · ${t("network.connected")}` : ""}</option>{/each}
        </select>
      </label>
    {:else if scan && !scan.error}
      <p class="muted small">{t("network.noneVisible")}</p>
    {/if}
    <label class="field"><span>{t("network.ssid")}</span><input class="input" type="text" bind:value={forms.wifiSsid} autocomplete="off" data-testid="wifi-ssid" /></label>
    <label class="field"><span>{t("network.passphrase")}</span><input class="input" type="password" bind:value={passphrase} autocomplete="new-password" data-testid="wifi-pass" /></label>
    <label class="check"><input type="checkbox" bind:checked={forms.wifiHidden} data-testid="wifi-hidden" />{t("network.hidden")}</label>
    <p class="note"><Icon name="lock" />{t("network.passphraseNote")}</p>
    <div class="row-wrap">
      <button class="btn" type="submit" disabled={Boolean(flow.planning) || Boolean(flow.pending) || !forms.wifiSsid.trim()} aria-busy={flow.planning === "wifi-plan" ? "true" : undefined} data-testid="wifi-plan">
        <Icon name={flow.planning === "wifi-plan" ? "loader" : "wifi"} />{flow.planning === "wifi-plan" ? t("plan.planning") : t("network.planWifi")}
      </button>
    </div>
  </form>
</Card>

<div class="span-6 stack">
  <Card icon="pencil" tone="c-pv" title={t("network.hostnameChange")} subtitle={t("network.hostnameChangeSub")}>
    <div class="form" data-testid="network-hostname-stage">
      <label class="field"><span>{t("network.newHostname")}</span><input class="input mono" type="text" bind:value={forms.hostname} placeholder={network.hostname || ""} autocomplete="off" data-testid="hostname-input" /></label>
      <div class="row-wrap">
        <PlanButton
          plan={{ key: "hostname-plan", endpoint: "/api/network/hostname", body: { hostname: forms.hostname.trim() }, title: { key: "plan.title.hostname" }, confirm: "plan.apply", forget: ["hostname"] }}
          label={t("network.planHostname")}
          icon="pencil"
          small={false}
          disabled={!forms.hostname.trim()}
          testid="hostname-plan"
        />
      </div>
    </div>
  </Card>
  <Card icon="clock" tone="c-pv" title={t("network.timezoneChange")} subtitle={t("network.timezoneChangeSub")}>
    <div class="form" data-testid="network-timezone-stage">
      <p class="muted small" data-testid="timezone-current">{t("network.timezoneCurrent", { zone: currentZone || t("common.unknown") })}</p>
      <label class="field"><span>{t("network.timezone")}</span><input class="input mono" type="text" value={timezone} oninput={(event) => (forms.timezone = event.currentTarget.value)} autocomplete="off" data-testid="timezone-input" /></label>
      <div class="row-wrap">
        <PlanButton
          plan={{ key: "timezone-plan", endpoint: "/api/system/timezone", body: { timezone: String(timezone).trim() }, title: { key: "plan.title.timezone" }, confirm: "plan.apply", forget: ["timezone"] }}
          label={t("network.planTimezone")}
          icon="clock"
          small={false}
          disabled={!String(timezone).trim()}
          testid="timezone-plan"
        />
      </div>
    </div>
  </Card>
</div>

<style>
  .form { display: flex; flex-direction: column; gap: var(--s3); }
  .form .note { margin: 0; }
  .small { font-size: 13px; margin: 0; }
  .stack { display: flex; flex-direction: column; gap: var(--s5); }
</style>
