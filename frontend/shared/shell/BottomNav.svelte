<script>
  import { tick } from "svelte";
  import Icon from "../ui/Icon.svelte";
  import Sheet from "../ui/Sheet.svelte";
  import Segmented from "../ui/Segmented.svelte";
  import { swapPinned } from "./pages.js";
  import { t } from "../i18n/i18n.svelte.js";
  import { prefs, setPref, THEMES, THEME_NAMES, LANGUAGES } from "../state/prefs.svelte.js";
  import { useShell } from "./context.js";

  const shell = useShell();
  const { pageSet, session } = shell;
  const { router, href, setPinned } = shell.nav;
  const themes = THEMES.map((value) => ({ value, label: THEME_NAMES[value] }));
  const languages = LANGUAGES.map((value) => ({ value, label: value.toUpperCase() }));

  const pinned = $derived(router.pinned.map(pageSet.pageById));
  const others = $derived(pageSet.pages.filter((page) => !router.pinned.includes(page.id)));
  const current = $derived(router.pinned.includes(router.page) ? null : pageSet.pageById(router.page));
  const focusId = $derived(current?.id ?? others[0]?.id);
  const customized = $derived(router.pinned.join() !== pageSet.mobile.join());

  let open = $state(false);
  let editing = $state(false);
  let picked = $state(null);
  let announced = $state("");
  let signOutFailed = $state(false);
  let moreButton = $state();
  let content = $state();

  const name = (id) => t(`nav.${id}`);
  /** The bar's own label: a page may name a shorter one ("shortLabel") for the narrow slot; its full name stays the title. */
  const slotName = (page) => (page.shortLabel ? t(page.shortLabel) : name(page.id));

  async function focusIn(selector) {
    await tick();
    content?.querySelector(selector)?.focus();
  }

  function closed() {
    open = false;
    editing = false;
    picked = null;
    announced = "";
    signOutFailed = false;
    if (document.activeElement === document.body) moreButton?.focus();
  }

  function startEditing() {
    editing = true;
    picked = null;
    announced = "";
    focusIn("button.tile");
  }

  function stopEditing() {
    editing = false;
    picked = null;
    announced = "";
    focusIn('[data-testid="customize-bar"]');
  }

  function choose(id) {
    announced = "";
    if (picked === null || picked === id) {
      picked = picked === id ? null : id;
      return;
    }
    const next = swapPinned(router.pinned, picked, id);
    if (!next) {
      picked = id;
      return;
    }
    setPinned(next);
    picked = null;
    announced = t("nav.barNow", { pages: router.pinned.map(name).join(", ") });
    focusIn(`button.tile[data-page="${id}"]`);
  }

  function resetBar() {
    setPinned(pageSet.mobile);
    picked = null;
    announced = t("nav.barNow", { pages: router.pinned.map(name).join(", ") });
    focusIn('[data-testid="customize-done"]');
  }

  function signIn() {
    open = false;
    session.signIn();
  }

  async function signOut() {
    signOutFailed = false;
    try {
      await session.signOut();
    } catch {
      signOutFailed = true;
    }
  }
</script>

<nav class="bottom-nav" aria-label={t("nav.main")}>
  {#each pinned as page (page.id)}
    <a href={href(page.id)} aria-current={router.page === page.id ? "page" : undefined} data-page={page.id}>
      <Icon name={page.icon} />
      <span class="slot-label" title={name(page.id)}>{slotName(page)}</span>
      {#if shell.locked(page)}<svg class="i slot-badge" aria-label={t("nav.signInRequired")} role="img"><use href="#i-lock"></use></svg>{/if}
    </a>
  {/each}
  <button
    type="button"
    bind:this={moreButton}
    onclick={() => (open = true)}
    aria-haspopup="dialog"
    aria-expanded={open ? "true" : "false"}
    aria-current={current ? "page" : undefined}
    aria-label={current ? t("nav.moreCurrent", { page: name(current.id) }) : undefined}
    data-testid="nav-more"
  >
    {#if current}
      <Icon name={current.icon} />
      <span class="slot-label" title={name(current.id)}>{slotName(current)}</span>
      <Icon name="chevron-up" size="slot-badge" />
    {:else}
      <Icon name="ellipsis" />
      <span class="slot-label">{t("nav.more")}</span>
    {/if}
  </button>
</nav>

{#snippet pickTile(page)}
  <button type="button" class="tile" aria-pressed={picked === page.id ? "true" : "false"} onclick={() => choose(page.id)} data-page={page.id}>
    <Icon name={page.icon} /><span>{name(page.id)}</span>
  </button>
{/snippet}

<Sheet {open} label={t("nav.more")} onclose={closed}>
  <div class="more" bind:this={content}>
    {#if editing}
      <h2 class="sheet-title">{t("nav.inBar")}</h2>
      <div class="tiles" role="group" aria-label={t("nav.inBar")} data-testid="bar-pages">
        {#each pinned as page (page.id)}{@render pickTile(page)}{/each}
      </div>
      <h2 class="sheet-title">{t("nav.inMore")}</h2>
      <div class="tiles" role="group" aria-label={t("nav.inMore")} data-testid="more-pages">
        {#each others as page (page.id)}{@render pickTile(page)}{/each}
      </div>
      <p class="hint" aria-live="polite">{announced || (picked ? t("nav.swapWith", { page: name(picked) }) : t("nav.swapHint"))}</p>
      <div class="sheet-actions">
        {#if customized}
          <button class="btn secondary" type="button" onclick={resetBar} data-testid="reset-bar"><Icon name="rotate-ccw" />{t("nav.resetBar")}</button>
        {/if}
        <button class="btn" type="button" onclick={stopEditing} data-testid="customize-done">{t("nav.customizeDone")}</button>
      </div>
    {:else}
      <h2 class="sheet-title">{t("nav.morePages")}</h2>
      <nav class="tiles" aria-label={t("nav.morePages")}>
        {#each others as page (page.id)}
          <a
            class="tile"
            href={href(page.id)}
            onclick={() => (open = false)}
            aria-current={router.page === page.id ? "page" : undefined}
            data-focus={page.id === focusId ? "" : undefined}
            data-page={page.id}
          >
            <Icon name={page.icon} /><span>{name(page.id)}</span>
            {#if shell.locked(page)}<svg class="i lock" aria-label={t("nav.signInRequired")} role="img"><use href="#i-lock"></use></svg>{/if}
          </a>
        {/each}
      </nav>
      <h2 class="sheet-title">{t("nav.quickSettings")}</h2>
      <div class="quick">
        <div class="pop-row"><span>{t("appearance.language")}</span>
          <Segmented options={languages} value={prefs.lang} label={t("appearance.language")} onchange={(v) => setPref("lang", v)} />
        </div>
        <div class="pop-row"><span>{t("appearance.theme")}</span>
          <Segmented options={themes} value={prefs.theme} label={t("appearance.theme")} onchange={(v) => setPref("theme", v)} />
        </div>
        <div class="pop-row">
          <span class="session"><Icon name={session.icon} size="sm" />{session.label}</span>
          {#if session.authenticated}
            <button class="btn secondary small" type="button" onclick={signOut} data-testid="sheet-sign-out">{t("auth.signOut")}</button>
          {:else}
            <button class="btn small" type="button" onclick={signIn} data-testid="sheet-sign-in">{t("auth.signIn")}</button>
          {/if}
        </div>
        {#if signOutFailed}<p class="form-error" role="alert">{t("auth.signOutFailed")}</p>{/if}
      </div>
      <div class="sheet-links">
        {#each shell.links as link (link.href)}
          <a class="ghost-link" href={link.href} data-testid={link.testid ? `sheet-${link.testid}` : undefined}><Icon name={link.icon} /><span>{t(link.label)}</span></a>
        {/each}
        <button class="ghost-link" type="button" onclick={startEditing} data-testid="customize-bar"><Icon name="pencil" /><span>{t("nav.customize")}</span></button>
        <button class="ghost-link" type="button" onclick={() => shell.classic(router.page)}><Icon name="arrow-left-right" /><span>{t("nav.classicSwitch")}</span></button>
      </div>
    {/if}
  </div>
</Sheet>

<style>
  .sheet-title { margin: var(--s3) 0 var(--s2); font-size: 12px; font-weight: 560; color: var(--m-muted); }
  .sheet-title:first-child { margin-top: var(--s1); }
  .tiles { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--s2); }
  .tile {
    position: relative; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 6px;
    min-height: 76px; padding: 12px 6px 10px; border-radius: var(--m-radius-inner);
    border: 1px solid var(--m-line); background: var(--m-surface-1); color: var(--m-text);
    font-size: 12.5px; font-weight: 500; line-height: 1.25; text-align: center; text-decoration: none; cursor: pointer;
  }
  .tile span { max-width: 100%; overflow-wrap: anywhere; }
  .tile:hover { background: var(--m-surface-3); }
  .tile[aria-current="page"], .tile[aria-pressed="true"] {
    background: color-mix(in srgb, var(--m-accent) 14%, var(--m-surface-1));
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--m-accent) 45%, transparent);
  }
  .tile[aria-current="page"] > :global(svg.i:not(.lock)), .tile[aria-pressed="true"] > :global(svg.i) { color: var(--m-accent); }
  .tile .lock { position: absolute; top: 7px; right: 7px; width: 13px; height: 13px; color: var(--m-muted-2); }
  .quick .pop-row { margin: 10px 0; }
  .session { display: inline-flex; align-items: center; gap: 6px; }
  .sheet-links { display: flex; flex-direction: column; gap: var(--s2); margin-top: var(--s3); }
  .hint { min-height: 2.6em; margin: var(--s3) 0 0; font-size: 12.5px; color: var(--m-muted); }
  .sheet-actions { display: flex; justify-content: flex-end; gap: var(--s2); margin-top: var(--s2); }
</style>
