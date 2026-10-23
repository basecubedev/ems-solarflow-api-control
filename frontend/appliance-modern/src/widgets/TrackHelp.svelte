<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { RELEASE_TRACKS } from "../lib/host.js";
</script>

<details class="more" data-testid="track-help">
  <summary><Icon name="chevron-right" />{t("track.help")}</summary>
  <dl class="body tracks">
    {#each RELEASE_TRACKS as track (track)}
      <div><dt>{t(`track.${track}`)}</dt><dd>{t(`track.${track}.help`)}</dd></div>
    {/each}
  </dl>
</details>

<style>
  .tracks { margin: 0; display: flex; flex-direction: column; gap: var(--s2); font-size: 13px; }
  .tracks dt { font-weight: 600; }
  .tracks dd { margin: 2px 0 0; color: var(--m-muted); }
</style>
