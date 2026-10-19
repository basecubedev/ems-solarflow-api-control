<script module>
  export const manifest = { id: "today-tiles", data: ["energy.today", "history:48h:pv,home,battery,grid"], refresh: "60s", minWidth: 280, states: ["partial-day", "no-history", "no-comparison"] };
</script>

<script>
  import Icon from "../ui/Icon.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { live } from "../state/live.svelte.js";
  import { channelKwh } from "../lib/energy.js";
  import { hourlyKwh, sameTimeComparison, localDayStart } from "../lib/history.js";

  let { history = null } = $props();

  const TILES = [
    { id: "pv", label: "today.solar", icon: "sun", token: "--pv", channel: "pv_yield", good: 1 },
    { id: "home", label: "today.home", icon: "house", token: "--output", channel: "home_consumption", good: -1 },
    { id: "battery", label: "today.batteryNet", icon: "battery-charging", token: "--battery", net: true, good: 1 },
    { id: "grid", label: "today.gridImport", icon: "utility-pole", token: "--grid", channel: "grid_import", positiveOnly: true, good: -1 },
  ];

  const f = $derived(fmt());
  const today = $derived(live.snapshot?.energy_stats?.today);
  const nowSeconds = $derived(Math.floor(live.now / 1000));
  const dayStart = $derived(localDayStart(new Date(live.now)));
  const currentHour = $derived(new Date(live.now).getHours());

  function tileValue(tile) {
    if (tile.net) {
      const charged = channelKwh(today, "battery_charge").kwh;
      const discharged = channelKwh(today, "battery_discharge").kwh;
      if (charged === null || discharged === null) return null;
      return charged - discharged;
    }
    return channelKwh(today, tile.channel).kwh;
  }

  function transformFor(tile) {
    return tile.positiveOnly ? (v) => Math.max(0, v) : undefined;
  }

  const tiles = $derived(
    TILES.map((tile) => {
      const values = history?.series?.[tile.id];
      const hours = values ? hourlyKwh(history.time, values, dayStart, { transform: transformFor(tile) }) : Array(24).fill(null);
      const comparison = history ? sameTimeComparison(history, tile.id, nowSeconds, dayStart, transformFor(tile)) : null;
      const max = Math.max(0.0001, ...hours.map((v) => Math.abs(v || 0)));
      return { ...tile, value: tileValue(tile), hours, max, comparison };
    }),
  );
</script>

<div class="today span-12" data-testid="today-tiles">
  {#each tiles as tile (tile.id)}
    <section class="card tile" style:--c={`var(${tile.token})`} aria-label={t(tile.label)}>
      <div class="head"><Icon name={tile.icon} size="sm" />{t(tile.label)}</div>
      <svg class="spark" viewBox="0 0 240 46" preserveAspectRatio="none" role="img" aria-label={t("today.hourlyAria")}>
        {#each tile.hours as kwh, hour (hour)}
          {#if kwh === null}
            {#if hour <= currentHour}<rect x={hour * 10 + 1.5} y="44" width="7" height="2" rx="1" class="empty-bar"></rect>{/if}
          {:else}
            <rect
              x={hour * 10 + 1.5}
              y={46 - Math.max(2, (Math.abs(kwh) / tile.max) * 44)}
              width="7"
              height={Math.max(2, (Math.abs(kwh) / tile.max) * 44)}
              rx="1.5"
              class="bar-rect"
              class:now={hour === currentHour}
              class:negative={kwh < 0}
            ><title>{`${String(hour).padStart(2, "0")}:00 · ${f.energy(kwh, { signed: tile.net })}`}</title></rect>
          {/if}
        {/each}
      </svg>
      <div class="foot">
        <div class="value num">{f.energy(tile.value, { signed: tile.net })}</div>
        <div class="delta num">
          {#if tile.comparison}
            <b class={tile.comparison.delta * tile.good >= 0 ? "up" : "down"}>{f.energy(tile.comparison.delta, { signed: true })}</b>
            <span>{t("today.vsYesterday", { time: f.time(live.now) })}</span>
          {:else}
            <span>{t("today.noComparison")}</span>
          {/if}
        </div>
      </div>
    </section>
  {/each}
</div>

<style>
  .today { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--s5); }
  .tile { padding: var(--s4) var(--s5); }
  .head { display: flex; align-items: center; gap: 10px; font-size: 13px; color: var(--m-muted); font-weight: 520; }
  .head :global(svg.i) { color: var(--c); }
  .spark { display: block; width: 100%; height: 46px; margin: 10px 0 8px; }
  .bar-rect { fill: var(--c); opacity: 0.7; }
  .bar-rect.now { opacity: 1; }
  .bar-rect.negative { opacity: 0.45; }
  .empty-bar { fill: var(--m-line-strong); }
  .foot { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; flex-wrap: wrap; }
  .value { font-size: 20px; font-weight: 650; letter-spacing: -0.02em; white-space: nowrap; }
  .delta { font-size: 12px; color: var(--m-muted); text-align: right; display: flex; flex-direction: column; }
  .delta b { font-weight: 600; }
  .up { color: var(--m-ok); }
  .down { color: var(--danger); }
  @media (max-width: 1280px) { .foot { flex-direction: column; align-items: flex-start; } .delta { text-align: left; } }
  @media (max-width: 1000px) { .today { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
  @media (max-width: 760px) { .today { gap: var(--s3); } .value { font-size: 18px; } }
</style>
