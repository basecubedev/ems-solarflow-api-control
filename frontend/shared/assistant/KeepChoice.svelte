<script>
  import Icon from "../ui/Icon.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { welcome, setWelcome } from "./welcome.svelte.js";

  /** The per-browser choice to open the console here, and the way to the overview. */
  let { hint, overviewHref } = $props();
</script>

<div class="choice span-12">
  <div class="choice-words">
    <label class="check"><input type="checkbox" checked={welcome.choice !== "off"} onchange={(event) => setWelcome(event.currentTarget.checked)} data-testid="assistant-keep" />{t("assistant.keep")}</label>
    <p class="muted">{hint}</p>
  </div>
  <a class="btn secondary" href={overviewHref} data-testid="assistant-overview">{t("assistant.toOverview")}<Icon name="arrow-right" size="sm" /></a>
</div>

<style>
  .choice { display: flex; align-items: center; gap: var(--s4); flex-wrap: wrap; padding: var(--s2) var(--s1); }
  .choice-words { flex: 1 1 260px; min-width: 0; }
  .choice-words p { margin: 4px 0 0 24px; font-size: 12.5px; }
</style>
