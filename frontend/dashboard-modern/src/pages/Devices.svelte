<script>
  import DeviceCard from "../widgets/DeviceCard.svelte";
  import GridMeter from "../widgets/GridMeter.svelte";
  import DeviceHistory from "../widgets/DeviceHistory.svelte";
  import EnergyFlow from "../widgets/EnergyFlow.svelte";
  import Empty from "../ui/Empty.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { live } from "../state/live.svelte.js";
  import { devices } from "../state/derived.svelte.js";

  const list = $derived(devices());
  const names = $derived(list.map((device) => device.name));
</script>

{#if !live.snapshot}
  <div class="span-12"><Empty text={t("device.none")} icon="loader" /></div>
{:else}
  <div class="device-grid span-12">
    {#each list as device (device.name)}
      <DeviceCard {device} />
    {/each}
    <GridMeter />
  </div>
  <EnergyFlow span="span-12" fixedMode="devices" />
  <DeviceHistory {names} />
{/if}

<style>
  .device-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: var(--s5); align-items: start; }
  @media (max-width: 760px) { .device-grid { grid-template-columns: 1fr; gap: var(--s4); } }
</style>
