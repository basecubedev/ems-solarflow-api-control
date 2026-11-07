<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import FactList from "./FactList.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";

  /**
   * The facts of a period on the consumers page: what it cost, what it saved,
   * its share of the home use and the consumer that drew most; in Expert also
   * the parts from the grid and from the own supply, and how many drew.
   */
  let { view, expert = false, currency = "EUR" } = $props();

  const f = $derived(fmt());
  const totals = $derived(view?.totals);
  const rows = $derived(
    totals?.count
      ? [
          { id: "cost", label: t("consumers.cost"), tone: "c-grid", value: f.money(totals.cost, currency) },
          { id: "saved", label: t("consumers.saved"), tone: "c-money", value: f.money(totals.saved, currency) },
          ...(view.share !== null && view.share !== undefined ? [{ id: "share", label: t("consumers.shareHome"), tone: "c-out", value: f.percent(view.share) }] : []),
          ...(totals.top ? [{ id: "top", label: t("consumers.top", { name: totals.top.name }), tone: "c-out", clip: true, value: f.energy(totals.top.kwh) }] : []),
          ...(expert
            ? [
                { id: "grid", label: t("consumers.grid"), tone: "c-grid", value: f.energy(totals.gridKwh) },
                { id: "own", label: t("consumers.own"), tone: "c-pv", value: f.energy(totals.ownKwh) },
                { id: "count", label: t("consumers.count"), divided: true, value: f.number(totals.count, 0) },
              ]
            : []),
        ]
      : [{ id: "none", label: t("consumers.nothing"), value: "—", na: true }],
  );
</script>

<FactList {rows} />
