<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";

  /**
   * A shared read that failed again while an older answer is still shown.
   * The older answer stays, so the page does not blank out over one failed
   * read, but it is never passed off as current.
   */
  let { resource } = $props();
  const f = $derived(fmt());
</script>

{#if resource.error && resource.data && resource.loadedAt}
  <p class="note stale" role="status" data-testid="stale-note"><Icon name="clock" />{t("stale.note", { when: f.when(resource.loadedAt) })}</p>
{/if}

<style>
  .stale { color: var(--m-warn); }
</style>
