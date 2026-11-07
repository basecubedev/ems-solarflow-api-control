<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import Segmented from "../../../shared/ui/Segmented.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { energyDetail, setEnergyDetail } from "../state/prefs.svelte.js";

  /** The line above an energy page: what its figures rest on, and Basic or Expert, one choice for every energy page. */
  let { text = "" } = $props();
</script>

<div class="energy-head span-12">
  <p class="muted">{text}</p>
  <Segmented
    options={[{ value: "basic", label: t("energy.detail.basic") }, { value: "expert", label: t("energy.detail.expert") }]}
    value={energyDetail.detail}
    label={t("energy.title")}
    onchange={setEnergyDetail}
  />
</div>

<style>
  .energy-head { display: flex; justify-content: space-between; align-items: center; gap: var(--s3); flex-wrap: wrap; }
  .energy-head p { margin: 0; font-size: 13px; }
</style>
