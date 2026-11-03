<script>
  import FactList from "./FactList.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { periodFigures, figureValue, figureListed, figureText, figureTone, channelsAllUnmeasured } from "../lib/energy.js";

  /**
   * The facts of any period: savings, the self-sufficiency ratio, solar and
   * home use, and in Expert AC charging, every other channel, the peak output
   * and since when the channels have been measured.
   */
  let { values, channels = [], since = null, expert = false, currency = "EUR", details = [] } = $props();

  const f = $derived(fmt());
  const unmeasured = $derived(channelsAllUnmeasured(values));
  const rows = $derived([
    ...periodFigures(channels, expert)
      .map((figure) => ({ figure, cell: figureValue(values, figure) }))
      .filter(({ figure, cell }) => figureListed(figure, cell, values, expert))
      .map(({ figure, cell }) => ({ id: figure.id, label: t(figure.label), tone: figureTone(figure), value: figureText(f, figure, cell, currency), na: cell.value === null })),
    ...(expert
      ? [{ id: "since", label: t("energy.measuredSince"), divided: true, value: since && !unmeasured ? f.date(since, { numeric: true }) : t("energy.notMeasured"), na: !since || unmeasured }]
      : []),
    ...details.map((detail) => ({ id: `detail:${detail.label}`, label: detail.label, value: detail.value })),
  ]);
</script>

<FactList {rows} />
