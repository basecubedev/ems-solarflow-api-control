<script module>
  export const manifest = { id: "storage", data: ["live.devices"], refresh: "sse", minWidth: 260, states: ["no-battery", "offline", "soc-not-reported"] };
</script>

<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Bar from "../../../shared/ui/Bar.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { devices } from "../state/derived.svelte.js";

  const f = $derived(fmt());
  const batteries = $derived(devices().filter((device) => device.enabled && (device.socReported || device.batteryW !== null)));

  function meta(device) {
    if (!device.online) return t("storage.offline");
    const w = device.batteryW || 0;
    if (w >= 8) return t("storage.charging", { v: f.power(w, { signed: true }) });
    if (w <= -8) return t("storage.discharging", { v: f.power(Math.abs(w)) });
    return t("storage.idle");
  }
</script>

<Card title={t("storage.title")} subtitle={t("storage.sub")}>
  {#if !batteries.length}
    <div class="empty">{t("storage.none")}</div>
  {:else}
    <div class="list">
      {#each batteries as device (device.name)}
        <div class="item" class:offline={!device.online}>
          <div class="row">
            <div class="icon-tile sm c-bat"><Icon name="battery" /></div>
            <div class="row-main">
              <div class="row-title ellipsis">{device.name}</div>
              <div class="row-meta num">{meta(device)}</div>
            </div>
            <div class="row-value num">{device.soc !== null ? f.percent(device.soc) : t("flow.noSoc")}</div>
          </div>
          <Bar value={device.online ? device.soc : null} label={`${device.name} ${t("flow.soc")}`} />
        </div>
      {/each}
    </div>
  {/if}
</Card>

<style>
  .list { display: flex; flex-direction: column; gap: var(--s4); }
  .row { display: flex; align-items: center; gap: var(--s3); margin-bottom: 6px; }
  .row-main { flex: 1; min-width: 0; }
  .row-title { font-weight: 560; }
  .row-meta { font-size: 12px; color: var(--m-muted); }
  .row-value { font-weight: 650; font-size: 15px; }
  .offline { opacity: 0.6; }
</style>
