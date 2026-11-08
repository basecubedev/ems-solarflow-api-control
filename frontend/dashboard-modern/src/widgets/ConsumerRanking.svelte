<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script module>
  export const manifest = { id: "consumer-ranking", data: ["live", "energy.today"], refresh: "sse", minWidth: 280, states: ["waiting", "offline"] };
</script>

<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Segmented from "../../../shared/ui/Segmented.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { live } from "../state/live.svelte.js";
  import { CONSUMER_ORDERS, consumerOrder, setConsumerOrder } from "../state/prefs.svelte.js";
  import { channelKwh } from "../lib/energy.js";
  import { consumerEnergy, consumerList, ordered } from "../lib/consumers.js";
  import { finite } from "../../../shared/lib/format.js";

  /**
   * Every consumer as one bar, with the part of the house no consumer
   * measured: the highest draw first, or by name. The bars share one scale,
   * the largest part at full length, so thirty of them still compare; the
   * share of the house stands beside each.
   */
  const f = $derived(fmt());
  const snapshot = $derived(live.snapshot);
  const today = $derived(snapshot?.energy_stats?.today);
  const currency = $derived(snapshot?.energy_stats?.currency || "EUR");
  const homeW = $derived(finite(snapshot?.home_load_w));
  const consumers = $derived(consumerList(snapshot).map((item) => ({ ...item, today: consumerEnergy(today, item.key) })));
  const unmeasured = $derived({
    key: "",
    name: t("consumers.unmeasured"),
    online: true,
    unmeasured: true,
    powerW: finite(snapshot?.home_other_w) ?? 0,
    today: { kwh: Math.max(0, (channelKwh(today, "home_consumption").kwh ?? 0) - consumers.reduce((sum, item) => sum + item.today.kwh, 0)), cost: null },
  });
  const rows = $derived(
    consumerOrder.order === "name"
      ? [...ordered(consumers, "name"), unmeasured]
      : ordered([...consumers, unmeasured], "rank", (item) => item.powerW),
  );
  const largest = $derived(Math.max(1, ...rows.map((row) => row.powerW ?? 0)));
  const share = (watts) => (watts !== null && homeW > 0 ? f.percent(Math.min(1, watts / homeW) * 100) : "—");
  const tip = (row) => [row.name, row.powerW === null ? t("device.offline") : f.power(row.powerW), share(row.powerW)].join(" · ");
</script>

{#snippet actions()}
  <Segmented options={CONSUMER_ORDERS.map((value) => ({ value, label: t(`consumers.order.${value}`) }))} value={consumerOrder.order} label={t("consumers.order")} onchange={setConsumerOrder} />
{/snippet}

<Card icon="plug-zap" tone="c-out" title={t("consumers.nowTitle")} subtitle={t("consumers.nowSub", { power: f.power(homeW) })} span="span-12" {actions}>
  <div class="head" aria-hidden="true">
    <span>{t("consumers.name")}</span>
    <span></span>
    <span class="r">{t("consumers.power")}</span>
    <span class="r c-share">{t("consumers.shareNow")}</span>
    <span class="r c-today">{t("common.today")}</span>
    <span class="r c-cost">{t("consumers.costToday")}</span>
  </div>
  <ol class="ranking" data-testid="consumer-ranking" data-order={consumerOrder.order}>
    {#each rows as row (row.unmeasured ? "\u0000" : row.key)}
      <li
        class:offline={!row.online}
        class:unmeasured={row.unmeasured}
        title={tip(row)}
        data-testid={row.unmeasured ? "consumer-row-unmeasured" : "consumer-row"}
        data-consumer={row.unmeasured ? undefined : row.name}
      >
        <span class="name ellipsis">{row.name}{#if !row.online}<span class="badge">{t("device.offline")}</span>{/if}</span>
        <span class="track"><span class="fill" style:width={`${((row.powerW ?? 0) / largest) * 100}%`}></span></span>
        <span class="r num value" data-testid="consumer-power">{row.powerW === null ? "—" : f.power(row.powerW)}</span>
        <span class="r num c-share">{share(row.powerW)}</span>
        <span class="r num c-today">{f.energy(row.today.kwh)}</span>
        <span class="r num c-cost">{row.today.cost === null ? "—" : f.money(row.today.cost, currency)}</span>
      </li>
    {/each}
  </ol>
  <p class="note">{t("consumers.unmeasured")}: {t("consumers.otherHint")}</p>
</Card>

<style>
  .head, .ranking li {
    display: grid; align-items: center; gap: 4px 14px;
    grid-template-columns: minmax(110px, 1.2fr) minmax(80px, 2fr) 76px 96px 76px 92px;
  }
  .head { font-size: 12px; color: var(--m-muted); padding: 0 0 6px; border-bottom: 1px solid var(--m-line); }
  .ranking { list-style: none; margin: 0; padding: 0; }
  .ranking li { padding: 7px 0; border-bottom: 1px solid var(--m-line); font-size: 13.5px; }
  .ranking li:last-child { border-bottom: 0; }
  .name { min-width: 0; font-weight: 600; }
  .badge { margin-left: 8px; }
  .track { height: 8px; border-radius: 4px; background: color-mix(in srgb, var(--m-muted-2) 18%, transparent); overflow: hidden; }
  .fill { display: block; height: 100%; min-width: 2px; border-radius: 4px; background: var(--output); }
  .unmeasured .name { color: var(--m-muted); font-weight: 500; }
  .unmeasured .fill { background: var(--m-muted-2); }
  .offline { opacity: 0.6; }
  .offline .fill { min-width: 0; }
  .value { font-weight: 650; }
  .r { text-align: right; }
  @media (max-width: 760px) {
    .head, .ranking li { grid-template-columns: minmax(96px, 1fr) minmax(60px, 1.2fr) 70px 64px; }
    .c-today, .c-cost { display: none; }
  }
  @media (max-width: 420px) {
    .head, .ranking li { grid-template-columns: minmax(90px, 1fr) minmax(48px, 1fr) 64px; }
    .c-share { display: none; }
  }
</style>
