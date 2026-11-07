<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import { t } from "../i18n/i18n.svelte.js";

  /** The button that starts the restart of a restartOffer, saying while it runs; quiet makes it a small secondary one, and button binds the element. */
  let { offer, onclick, quiet = false, button = $bindable() } = $props();
</script>

<button bind:this={button} class="btn" class:secondary={quiet} class:small={quiet} type="button" {onclick} disabled={!offer.ready} data-testid="restart-ems"><Icon name={offer.busy ? "loader" : "rotate-cw"} size="sm" />{offer.busy ? t("settings.edit.restarting") : t("settings.edit.restart")}</button>
