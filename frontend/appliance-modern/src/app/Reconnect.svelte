<script>
  import BrandMark from "../../../shared/ui/BrandMark.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { actions, checkReconnect } from "../state/actions.svelte.js";

  const shutdown = $derived(actions.reconnect === "system.shutdown");
  let checking = $state(false);

  async function check() {
    checking = true;
    try {
      await checkReconnect();
    } finally {
      checking = false;
    }
  }
</script>

<main class="reconnect" data-testid="reconnect-screen">
  <div class="panel">
    <div class="head">
      <BrandMark />
      <div>
        <div class="product">{t("app.name")}</div>
        <div class="muted">{t("app.sub")}</div>
      </div>
    </div>
    <div class="state">
      <span class="icon-tile {shutdown ? 'c-idle' : 'c-acc'}"><Icon name={shutdown ? "power" : "rotate-cw"} /></span>
      <div>
        <h1 class="title">{t("reconnect.title")}</h1>
        <p class="lead" role="status" aria-live="polite" data-testid="reconnect-detail">{shutdown ? t("reconnect.shutdown") : t("reconnect.reboot")}</p>
      </div>
    </div>
    <p class="muted hint">{t("reconnect.hint")}</p>
    <button class="btn" type="button" onclick={check} disabled={checking} aria-busy={checking ? "true" : undefined} data-testid="reconnect-check">
      <Icon name={checking ? "loader" : "refresh-cw"} />{t("reconnect.check")}
    </button>
  </div>
</main>

<style>
  .reconnect { min-height: 100vh; display: grid; place-items: center; padding: var(--s4); }
  .panel {
    width: min(480px, 100%); padding: var(--s6) var(--s5); border-radius: var(--m-radius-card);
    border: 1px solid var(--m-line); background: linear-gradient(180deg, color-mix(in srgb, var(--m-surface-2) 55%, var(--m-surface-1)), var(--m-surface-1));
    box-shadow: var(--m-shadow-pop); display: flex; flex-direction: column; gap: var(--s4);
  }
  .head { display: flex; align-items: center; gap: var(--s3); }
  .head :global(.brand-mark) { width: 44px; height: 44px; border-radius: 13px; }
  .product { font-weight: 650; font-size: 16px; }
  .state { display: flex; gap: var(--s3); align-items: flex-start; }
  .title { font-size: 19px; font-weight: 650; margin: 0 0 4px; }
  .lead { margin: 0; color: var(--m-muted); }
  .hint { margin: 0; font-size: 12.5px; }
  .btn { align-self: flex-start; }
</style>
