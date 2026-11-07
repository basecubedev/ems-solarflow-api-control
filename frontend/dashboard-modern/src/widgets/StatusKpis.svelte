<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script module>
  export const manifest = { id: "status-kpis", data: ["live", "energy.today"], refresh: "sse", minWidth: 280, states: ["waiting", "stale", "fault", "attention", "paused", "ok", "no-pv"] };
</script>

<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { live } from "../state/live.svelte.js";
  import { systemStatus, systemHardware, devices } from "../state/derived.svelte.js";
  import { selfSufficiency, channelKwh } from "../lib/energy.js";
  import { FLOW_ACTIVATE_W } from "../lib/flow.js";
  import { finite } from "../../../shared/lib/format.js";

  const LOOK = {
    ok: { tone: "c-ok", icon: "circle-check" },
    paused: { tone: "c-warn", icon: "circle-pause" },
    attention: { tone: "c-warn", icon: "triangle-alert" },
    fault: { tone: "c-fault", icon: "circle-x" },
    stale: { tone: "c-fault", icon: "clock-alert" },
    waiting: { tone: "c-idle", icon: "loader" },
  };

  const f = $derived(fmt());
  const status = $derived(systemStatus());
  const look = $derived(LOOK[status.level] || LOOK.waiting);
  const stats = $derived(live.snapshot?.energy_stats);
  const today = $derived(stats?.today);
  const ratio = $derived(selfSufficiency(today));
  const pvToday = $derived(channelKwh(today, "pv_yield").kwh);
  const savings = $derived(stats?.enabled === false ? null : finite(today?.savings_value));
  const price = $derived(finite(stats?.price_per_kwh));
  const currency = $derived(stats?.currency || "EUR");
  const hardware = $derived(systemHardware());
  const batteryTile = $derived(!hardware.pv && hardware.battery);
  const tileCount = $derived(hardware.pv || hardware.battery ? 4 : 3);
  const socReported = $derived(devices().some((device) => device.hasBattery && device.socReported));
  const batteryMeta = $derived.by(() => {
    const w = finite(live.snapshot?.battery_power_w);
    if (w === null) return "";
    if (w >= FLOW_ACTIVATE_W) return t("storage.charging", { v: f.power(w, { signed: true }) });
    if (w <= -FLOW_ACTIVATE_W) return t("storage.discharging", { v: f.power(Math.abs(w)) });
    return t("storage.idle");
  });

  const statusMeta = $derived.by(() => {
    if (status.level === "stale") return t("status.metaStale", { n: status.ageSeconds });
    if (status.level === "waiting" || !status.fleet) return "";
    if (status.fleet.total === 0) return t("status.noDevices");
    const fleet = t("status.metaFleet", { online: status.fleet.online, total: status.fleet.total });
    if (status.level === "paused") return `${t("status.metaPaused")} · ${fleet}`;
    return `${t("status.metaRegulating")} · ${fleet}`;
  });
</script>

<div class="kpis span-12" style:--kpis={tileCount}>
  <section class="card kpi" data-testid="kpi-status" data-level={status.level}>
    <div class="icon-tile {look.tone}"><Icon name={look.icon} /></div>
    <div class="kpi-text">
      <div class="kpi-label">{t("status.title")}</div>
      <div class="kpi-value status-word {look.tone}" aria-live="polite">{t(status.key, status.vars)}</div>
      <div class="kpi-meta">{statusMeta}</div>
    </div>
  </section>
  <section class="card kpi" data-testid="kpi-self">
    <div class="icon-tile c-bat"><Icon name="leaf" /></div>
    <div class="kpi-text">
      <div class="kpi-label">{t("kpi.selfSufficiency")}</div>
      <div class="kpi-value num">{ratio ? f.percent(ratio.ratio, { fraction: true }) : "—"}{#if ratio?.partial}<span class="partial" title={t("kpi.partial")}>{" "}◦</span>{/if}</div>
      <div class="kpi-meta">{t("kpi.selfSufficiencyMeta")}</div>
    </div>
  </section>
  {#if batteryTile}
    <section class="card kpi" data-testid="kpi-battery">
      <div class="icon-tile c-bat"><Icon name="battery" /></div>
      <div class="kpi-text">
        <div class="kpi-label">{t("kpi.battery")}</div>
        <div class="kpi-value num">{socReported ? f.percent(live.snapshot?.average_soc) : "—"}</div>
        <div class="kpi-meta num">{batteryMeta}</div>
      </div>
    </section>
  {:else if hardware.pv}
    <section class="card kpi" data-testid="kpi-solar">
      <div class="icon-tile c-pv"><Icon name="sun" /></div>
      <div class="kpi-text">
        <div class="kpi-label">{t("kpi.solarNow")}</div>
        <div class="kpi-value num">{f.power(live.snapshot?.pv_total_w)}</div>
        <div class="kpi-meta num">{pvToday !== null ? t("kpi.todayValue", { v: f.energy(pvToday) }) : ""}</div>
      </div>
    </section>
  {/if}
  <section class="card kpi" data-testid="kpi-savings">
    <div class="icon-tile c-money"><Icon name="piggy-bank" /></div>
    <div class="kpi-text">
      <div class="kpi-label">{t("kpi.savings")}</div>
      <div class="kpi-value num">{f.money(savings, currency)}</div>
      <div class="kpi-meta">{price !== null && stats?.enabled !== false ? t("kpi.savingsMeta", { p: f.money(price, currency) }) : ""}</div>
    </div>
  </section>
</div>

<style>
  .kpis { display: grid; grid-template-columns: repeat(var(--kpis, 4), minmax(0, 1fr)); gap: var(--s5); }
  .kpi { display: flex; gap: var(--s4); align-items: center; padding: var(--s4) var(--s5); }
  .kpi-text { min-width: 0; }
  .kpi-label { font-size: 12.5px; color: var(--m-muted); font-weight: 500; }
  .kpi-value { font-size: 24px; font-weight: 650; letter-spacing: -0.02em; line-height: 1.2; white-space: nowrap; }
  .status-word { font-size: 19px; color: var(--c); }
  .kpi-meta { font-size: 12px; color: var(--m-muted); margin-top: 1px; }
  .partial { color: var(--m-muted); font-size: 0.7em; }
  @container content (max-width: 1560px) {
    .kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .kpi:last-child:nth-child(odd) { grid-column: 1 / -1; }
  }
  @container content (max-width: 740px) { .kpis { grid-template-columns: minmax(0, 1fr); } }
  @media (max-width: 760px) {
    .kpis { gap: var(--s3); }
    .kpi { flex-direction: column; align-items: flex-start; gap: 10px; padding: var(--s4); }
    .kpi-value { font-size: 20px; white-space: normal; }
    .status-word { font-size: 17px; }
  }
</style>
