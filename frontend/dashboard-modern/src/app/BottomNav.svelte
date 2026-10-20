<script>
  import Icon from "../ui/Icon.svelte";
  import { MOBILE_PAGES, PAGES, pageById } from "./pages.js";
  import { router, href } from "./router.svelte.js";
  import { t } from "../i18n/i18n.svelte.js";
  import { chooseClassic } from "../state/prefs.svelte.js";
  import { auth } from "../state/auth.svelte.js";

  const pages = MOBILE_PAGES.map(pageById);
  const others = PAGES.filter((page) => !MOBILE_PAGES.includes(page.id));
  const moreActive = $derived(!MOBILE_PAGES.includes(router.page));
  let menu = $state();

  function close() {
    menu?.hidePopover?.();
  }
</script>

<nav class="bottom-nav" aria-label={t("nav.main")}>
  {#each pages as page (page.id)}
    <a href={href(page.id)} aria-current={router.page === page.id ? "page" : undefined}><Icon name={page.icon} /><span title={t(`nav.${page.id}`)}>{t(`nav.${page.id}`)}</span></a>
  {/each}
  <button type="button" popovertarget="more-menu" aria-current={moreActive ? "page" : undefined} class:active={moreActive}><Icon name="ellipsis" /><span>{t("nav.more")}</span></button>
</nav>

<div class="popover more-menu" popover id="more-menu" bind:this={menu}>
  <nav class="nav" aria-label={t("nav.more")}>
    {#each others as page (page.id)}
      <a href={href(page.id)} onclick={close} aria-current={router.page === page.id ? "page" : undefined}>
        <Icon name={page.icon} /><span>{t(`nav.${page.id}`)}</span>
        {#if page.operator && !auth.authenticated}<svg class="i lock" aria-label={t("nav.signInRequired")} role="img"><use href="#i-lock"></use></svg>{/if}
      </a>
    {/each}
  </nav>
  <button class="ghost-link" type="button" onclick={() => chooseClassic()}><Icon name="arrow-left-right" /><span>{t("nav.classicSwitch")}</span></button>
</div>

<style>
  .more-menu { top: auto; bottom: 76px; }
  .more-menu .nav { margin-bottom: var(--s3); }
  .more-menu .nav a span { display: inline; }
  .more-menu .nav a { justify-content: flex-start; }
  .more-menu .nav a .lock { display: inline; }
  .active { color: var(--m-accent); }
</style>
