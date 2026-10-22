<script>
  import Icon from "../ui/Icon.svelte";
  import BrandMark from "../ui/BrandMark.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { prefs, setPref } from "../state/prefs.svelte.js";
  import { useShell } from "./context.js";

  let { version = "" } = $props();
  const shell = useShell();
  const { router, href } = shell.nav;
  const groups = shell.pageSet.groups.map((group) => ({
    ...group,
    pages: shell.pageSet.listed.filter((page) => page.group === group.id),
  }));
</script>

<aside class="sidebar">
  <a class="brand" href={href(shell.pageSet.pages[0].id)} aria-label={t("app.name")}>
    <BrandMark />
    <span class="brand-short" aria-hidden="true">{shell.short}</span>
    <span class="brand-text">
      <span class="brand-name">{t("app.name")}</span><br />
      <span class="brand-sub">{t("app.sub")}</span>
    </span>
  </a>
  <nav class="nav" aria-label={t("nav.main")}>
    {#each groups as group (group.id)}
      {#if group.label}<div class="nav-group-label">{t(group.label)}</div>{/if}
      {#each group.pages as page (page.id)}
        <a href={href(page.id)} aria-current={router.page === page.id ? "page" : undefined} title={t(`nav.${page.id}`)} data-page={page.id}>
          <Icon name={page.icon} /><span>{t(`nav.${page.id}`)}</span>
          {#if shell.locked(page)}<svg class="i lock" aria-label={t("nav.signInRequired")} role="img"><use href="#i-lock"></use></svg>{/if}
        </a>
      {/each}
    {/each}
  </nav>
  <div class="sidebar-foot">
    {#each shell.links as link (link.href)}
      <a class="ghost-link" href={link.href} title={t(link.label)} data-testid={link.testid}>
        <Icon name={link.icon} /><span>{t(link.label)}</span>
      </a>
    {/each}
    <button
      class="ghost-link"
      type="button"
      onclick={() => setPref("nav", prefs.navIconsOnly ? "labels" : "icons")}
      title={prefs.navIconsOnly ? t("nav.showLabels") : t("nav.hideLabels")}
      aria-label={prefs.navIconsOnly ? t("nav.showLabels") : t("nav.hideLabels")}
      data-testid="nav-labels"
    >
      <Icon name={prefs.navIconsOnly ? "panel-left-open" : "panel-left-close"} /><span>{t("nav.hideLabels")}</span>
    </button>
    <button class="ghost-link" type="button" onclick={() => shell.classic(router.page)} title={t("nav.classicSwitch")} data-testid="switch-classic">
      <Icon name="arrow-left-right" /><span>{t("nav.classic")}</span>
    </button>
    {#if version}<div class="version">{version}</div>{/if}
  </div>
</aside>
