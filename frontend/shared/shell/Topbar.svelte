<script>
  import Icon from "../ui/Icon.svelte";
  import BrandMark from "../ui/BrandMark.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { useShell } from "./context.js";

  /** status: the app's own pills, between the page title and the session. */
  let { status } = $props();
  const shell = useShell();
  const { router, href } = shell.nav;
  const session = shell.session;

  let signOutFailed = $state(false);

  async function signOut() {
    signOutFailed = false;
    try {
      await session.signOut();
    } catch {
      signOutFailed = true;
    }
  }
</script>

<header class="topbar">
  <div class="topbar-title">
    <a class="topbar-mark" href={href(shell.pageSet.pages[0].id)} aria-label={t("app.name")}><BrandMark /></a>
    <div class="ellipsis">
      <h1 class="page-title">{t(`nav.${router.page}`)}</h1>
      <div class="page-sub">{t(`page.${router.page}.sub`)}</div>
    </div>
  </div>
  <div class="topbar-right">
    {@render status?.()}
    {#if session.pill !== false}<span class="pill hide-sm" data-testid="session-state"><Icon name={session.icon} size="sm" />{session.label}</span>{/if}
    {#if session.authenticated}
      {#if signOutFailed}<span class="form-error" role="alert">{t("auth.signOutFailed")}</span>{/if}
      <button class="btn secondary small" type="button" onclick={signOut} data-testid="sign-out">{t("auth.signOut")}</button>
    {:else}
      <button class="btn small" type="button" onclick={() => session.signIn()} data-testid="sign-in">{t("auth.signIn")}</button>
    {/if}
    <button class="icon-btn" type="button" popovertarget="appearance" aria-label={t("appearance.title")} title={t("appearance.title")} data-testid="appearance-button">
      <Icon name="settings-2" />
    </button>
  </div>
</header>
