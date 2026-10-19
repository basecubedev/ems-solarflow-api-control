<script module>
  export const manifest = { id: "grid-meter", data: ["live", "energy.today"], refresh: "sse", minWidth: 280, states: ["offline"] };
</script>

<script>
  import Icon from "../ui/Icon.svelte";
  import Fact from "../ui/Fact.svelte";
  import Status from "../ui/Status.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { live } from "../state/live.svelte.js";
  import { channelKwh } from "../lib/energy.js";

  const f = $derived(fmt());
  const snapshot = $derived(live.snapshot);
  const valid = $derived(snapshot?.grid_power_valid !== false);
  const today = $derived(snapshot?.energy_stats?.today);
</script>

<article class="card meter" data-testid="grid-meter">
  <div class="head">
    <div class="icon-tile c-grid"><Icon name="gauge" /></div>
    <h2 class="name">{t("device.meter")}</h2>
    {#if valid}
      <Status tone="c-ok" icon="circle-check" text={t("device.online")} />
    {:else}
      <Status tone="c-fault" icon="wifi-off" text={t("device.offline")} />
    {/if}
  </div>
  <div class="facts two">
    <Fact label={t("device.grid")} icon="utility-pole" tone="c-grid" value={valid ? f.power(snapshot?.grid_power_w, { signed: true }) : "—"} missing={!valid} />
    <Fact label={t("flow.home")} icon="house" tone="c-out" value={f.power(snapshot?.home_load_w)} />
    <Fact label={t("flow.importedToday")} value={f.energy(channelKwh(today, "grid_import").kwh)} />
    <Fact label={t("flow.exportedToday")} value={f.energy(channelKwh(today, "grid_export").kwh)} />
  </div>
  {#if !valid}<p class="note"><Icon name="info" />{t("device.meterOffline")}</p>{/if}
</article>

<style>
  .meter { display: flex; flex-direction: column; gap: var(--s4); }
  .head { display: flex; gap: var(--s3); align-items: center; }
  .name { font-weight: 640; font-size: 16px; margin: 0; flex: 1; }
  .note { margin-top: 0; }
</style>
