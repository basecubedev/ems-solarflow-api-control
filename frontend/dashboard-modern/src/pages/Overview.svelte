<script>
  import StatusKpis from "../widgets/StatusKpis.svelte";
  import EnergyFlow from "../widgets/EnergyFlow.svelte";
  import Storage from "../widgets/Storage.svelte";
  import ActiveRules from "../widgets/ActiveRules.svelte";
  import TodayTiles from "../widgets/TodayTiles.svelte";
  import PowerChart from "../widgets/PowerChart.svelte";
  import { fetchSeries } from "../lib/historyApi.js";
  import { localDayStart } from "../lib/history.js";

  let history = $state.raw(null);
  let controller = null;

  async function load() {
    controller?.abort();
    controller = new AbortController();
    const start = localDayStart(new Date(), -1);
    try {
      history = await fetchSeries({
        start,
        end: Date.now() / 1000,
        series: ["pv", "home", "battery", "grid"],
        signal: controller.signal,
      });
    } catch (error) {
      if (error?.name !== "AbortError") history = history || null;
    }
  }

  $effect(() => {
    load();
    const timer = setInterval(() => {
      if (!document.hidden) load();
    }, 60000);
    return () => {
      clearInterval(timer);
      controller?.abort();
    };
  });
</script>

<StatusKpis />
<EnergyFlow span="span-8" />
<div class="stack span-4 side">
  <Storage />
  <ActiveRules />
</div>
<TodayTiles {history} />
<PowerChart todayHistory={history} />

<style>
  @media (max-width: 1280px) and (min-width: 761px) {
    .side { display: grid; grid-template-columns: 1fr 1fr; }
  }
</style>
