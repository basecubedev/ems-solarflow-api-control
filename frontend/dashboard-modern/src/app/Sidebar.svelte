<script>
  import Icon from "../ui/Icon.svelte";
  import { PAGES } from "./pages.js";
  import { router, href } from "./router.svelte.js";
  import { t } from "../i18n/i18n.svelte.js";
  import { chooseClassic, prefs, setPref } from "../state/prefs.svelte.js";
  import { auth } from "../state/auth.svelte.js";

  let { version = "" } = $props();
  const main = PAGES.filter((page) => page.group === "main");
  const system = PAGES.filter((page) => page.group === "system");
</script>

<aside class="sidebar">
  <a class="brand" href={href("overview")} aria-label={t("app.name")}>
    <span class="brand-mark"><Icon name="zap" /></span>
    <span class="brand-text">
      <span class="brand-name">{t("app.name")}</span><br />
      <span class="brand-sub">{t("app.sub")}</span>
    </span>
  </a>
  <nav class="nav" aria-label={t("nav.main")}>
    {#each main as page (page.id)}
      <a href={href(page.id)} aria-current={router.page === page.id ? "page" : undefined} title={t(`nav.${page.id}`)} data-page={page.id}>
        <Icon name={page.icon} /><span>{t(`nav.${page.id}`)}</span>
        {#if page.operator && !auth.authenticated}<svg class="i lock" aria-label={t("nav.signInRequired")} role="img"><use href="#i-lock"></use></svg>{/if}
      </a>
    {/each}
    <div class="nav-group-label">{t("nav.system")}</div>
    {#each system as page (page.id)}
      <a href={href(page.id)} aria-current={router.page === page.id ? "page" : undefined} title={t(`nav.${page.id}`)} data-page={page.id}>
        <Icon name={page.icon} /><span>{t(`nav.${page.id}`)}</span>
        {#if !auth.authenticated}<svg class="i lock" aria-label={t("nav.signInRequired")} role="img"><use href="#i-lock"></use></svg>{/if}
      </a>
    {/each}
  </nav>
  <div class="sidebar-foot">
    <button
      class="ghost-link"
      type="button"
      onclick={() => setPref("nav", prefs.navIconsOnly ? "labels" : "icons")}
      title={prefs.navIconsOnly ? t("nav.showLabels") : t("nav.hideLabels")}
      aria-label={prefs.navIconsOnly ? t("nav.showLabels") : t("nav.hideLabels")}
      aria-pressed={prefs.navIconsOnly ? "false" : "true"}
      data-testid="nav-labels"
    >
      <Icon name={prefs.navIconsOnly ? "panel-left-open" : "panel-left-close"} /><span>{t("nav.hideLabels")}</span>
    </button>
    <button class="ghost-link" type="button" onclick={() => chooseClassic()} title={t("nav.classicSwitch")} data-testid="switch-classic">
      <Icon name="arrow-left-right" /><span>{t("nav.classic")}</span>
    </button>
    {#if version}<div class="version">{version}</div>{/if}
  </div>
</aside>
