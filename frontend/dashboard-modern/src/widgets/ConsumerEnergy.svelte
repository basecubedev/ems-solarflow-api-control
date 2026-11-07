<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script module>
  export const manifest = { id: "consumer-energy", data: ["energy"], refresh: "sse", minWidth: 320, states: ["empty", "disabled"] };
</script>

<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Segmented from "../../../shared/ui/Segmented.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { live } from "../state/live.svelte.js";
  import { consumerOrder } from "../state/prefs.svelte.js";
  import { PERIODS, channelKwh } from "../lib/energy.js";
  import { consumerEnergy, consumerRows, homeShare, ordered } from "../lib/consumers.js";

  /**
   * stats: the energy summary. A consumer is priced by the share of the house
   * the grid supplied while it drew: that part cost the price, the rest was
   * saved. One that is gone keeps its values, called by its last name and the
   * day it last drew; the rows follow the order the consumers page is read in.
   */
  let { stats } = $props();

  const TABLE_PERIODS = [...PERIODS.filter((item) => item.kind !== "best"), { key: "lifetime", label: "energy.lifetime", kind: "lifetime" }];

  let periodKey = $state("today");

  const f = $derived(fmt());
  const currency = $derived(stats?.currency || "EUR");
  const period = $derived(stats?.[periodKey] || null);
  const homeKwh = $derived(channelKwh(period, "home_consumption").kwh ?? 0);
  const rows = $derived(
    ordered(
      consumerRows(live.snapshot, period).map((row) => ({
        ...row,
        label: row.live ? row.name : t("consumers.former", { name: row.name, date: f.date(row.lastDate) }),
        ...consumerEnergy(period, row.key),
      })),
      consumerOrder.order,
      (row) => row.kwh,
    ),
  );
  const measuredKwh = $derived(rows.reduce((sum, row) => sum + row.kwh, 0));
  const otherKwh = $derived(Math.max(0, homeKwh - measuredKwh));
  const totals = $derived(rows.reduce((sum, row) => ({ cost: sum.cost + row.cost, saved: sum.saved + row.saved }), { cost: 0, saved: 0 }));
  const share = (kwh) => {
    const value = homeShare(kwh, homeKwh);
    return value === null ? "—" : f.percent(value);
  };
</script>

{#snippet actions()}
  <Segmented options={TABLE_PERIODS.map((item) => ({ value: item.key, label: t(item.label) }))} value={periodKey} label={t("consumers.period")} onchange={(value) => (periodKey = value)} />
{/snippet}

<Card icon="piggy-bank" tone="c-money" title={t("consumers.energyTitle")} subtitle={t("consumers.energySub")} span="span-12" {actions}>
  <div class="table-scroll">
    <table class="data" data-testid="consumer-energy">
      <thead>
        <tr>
          <th>{t("consumers.name")}</th>
          <th class="r">{t("consumers.energy")}</th>
          <th class="r">{t("consumers.own")}</th>
          <th class="r">{t("consumers.grid")}</th>
          <th class="r">{t("consumers.cost")}</th>
          <th class="r">{t("consumers.saved")}</th>
          <th class="r">{t("consumers.shareHome")}</th>
        </tr>
      </thead>
      <tbody>
        {#each rows as row (row.key)}
          <tr data-consumer={row.name} class:former={!row.live}>
            <td>{row.label}</td>
            <td class="r num">{f.energy(row.kwh)}</td>
            <td class="r num">{f.energy(row.ownKwh)}</td>
            <td class="r num">{f.energy(row.gridKwh)}</td>
            <td class="r num">{f.money(row.cost, currency)}</td>
            <td class="r num">{f.money(row.saved, currency)}</td>
            <td class="r num">{share(row.kwh)}</td>
          </tr>
        {/each}
        <tr class="other" data-testid="consumer-energy-unmeasured">
          <td>{t("consumers.unmeasured")}</td>
          <td class="r num">{f.energy(otherKwh)}</td>
          <td class="r num muted">—</td>
          <td class="r num muted">—</td>
          <td class="r num muted">—</td>
          <td class="r num muted">—</td>
          <td class="r num">{share(otherKwh)}</td>
        </tr>
      </tbody>
      <tfoot>
        <tr>
          <th>{t("consumers.total")}</th>
          <td class="r num">{f.energy(homeKwh)}</td>
          <td></td>
          <td></td>
          <td class="r num">{f.money(totals.cost, currency)}</td>
          <td class="r num">{f.money(totals.saved, currency)}</td>
          <td></td>
        </tr>
      </tfoot>
    </table>
  </div>
  <p class="note">{t("consumers.priceNote", { price: f.money(stats?.price_per_kwh ?? 0, currency) })}</p>
</Card>

<style>
  .table-scroll { overflow-x: auto; }
  .other td, .former td:first-child { color: var(--m-muted); }
  tfoot th, tfoot td { border-top: 1px solid var(--m-line-strong); font-weight: 600; }
</style>
