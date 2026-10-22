<script>
  import { untrack } from "svelte";
  import Sidebar from "../../shared/shell/Sidebar.svelte";
  import Topbar from "../../shared/shell/Topbar.svelte";
  import BottomNav from "../../shared/shell/BottomNav.svelte";
  import Appearance from "../../shared/shell/Appearance.svelte";
  import { provideShell } from "../../shared/shell/context.js";
  import SignIn from "./app/SignIn.svelte";
  import TopbarStatus from "./app/TopbarStatus.svelte";
  import Reconnecting from "./app/Reconnecting.svelte";
  import WorkflowBanner from "./widgets/WorkflowBanner.svelte";
  import Overview from "./pages/Overview.svelte";
  import Empty from "../../shared/ui/Empty.svelte";
  import { pageSet, pageById } from "./app/pages.js";
  import { nav, router } from "./app/router.svelte.js";
  import { t } from "./i18n/i18n.svelte.js";
  import { prefs, chooseClassic } from "./state/prefs.svelte.js";
  import { auth, logout } from "./state/auth.svelte.js";
  import { system, forgetSessionErrors } from "./state/system.svelte.js";
  import { resumeJobs } from "./state/backupJobs.svelte.js";
  import { forgetSessionError } from "./state/diagnostics.svelte.js";
  import { forgetSessionRefusal } from "./state/services.svelte.js";
  import { resumeUpgrade } from "./state/upgrade.svelte.js";
  import { dashboardHref } from "./lib/links.js";

  /**
   * Every page but the overview loads when it is first opened, so no single
   * file the console ships outgrows what the repository tracks unasked.
   */
  const PAGES = {
    updates: () => import("./pages/Updates.svelte"),
    settings: () => import("./pages/Settings.svelte"),
    services: () => import("./pages/Services.svelte"),
    backups: () => import("./pages/Backups.svelte"),
    diagnostics: () => import("./pages/Diagnostics.svelte"),
    system: () => import("./pages/System.svelte"),
  };
  const loaded = $state({ overview: Overview });
  const Page = $derived(loaded[router.page] || null);
  let unloadable = $state("");

  /** A page whose file does not arrive, as while the Admin console restarts, says so and offers a reload. */
  $effect(() => {
    const id = router.page;
    if (loaded[id] || !PAGES[id]) return;
    unloadable = "";
    PAGES[id]().then(
      (module) => {
        loaded[id] = module.default;
      },
      () => {
        if (router.page === id) unloadable = id;
      },
    );
  });
  const dashboard = $derived(dashboardHref(system.overview.data?.links?.dashboard_url));

  $effect(() => {
    if (auth.authenticated && auth.reachable) {
      untrack(() => {
        resumeJobs();
        forgetSessionErrors();
        forgetSessionError();
        forgetSessionRefusal();
        resumeUpgrade();
      });
    }
  });

  provideShell({
    pageSet,
    nav,
    short: "ADMIN",
    locked: () => false,
    session: {
      get authenticated() {
        return auth.authenticated;
      },
      get label() {
        return t("auth.signedIn");
      },
      icon: "lock-open",
      pill: false,
      signIn: () => {},
      signOut: logout,
    },
    classic: (pageId) => chooseClassic(pageId ? pageById(pageId).classic : "maintenance"),
    get links() {
      return dashboard ? [{ href: dashboard, icon: "layout-dashboard", label: "nav.dashboard", testid: "open-dashboard" }] : [];
    },
  });
</script>

{#if !auth.reachable && auth.authenticated}<Reconnecting />{/if}
{#if !auth.authenticated}
  <SignIn />
{:else}
  <a
    class="skip-link"
    href="#content"
    onclick={(event) => {
      event.preventDefault();
      document.getElementById("content")?.focus();
    }}>{t("app.skip")}</a>
  <div class="app" class:rail={prefs.navIconsOnly}>
    <Sidebar />
    <div class="main">
      <Topbar>
        {#snippet status()}<TopbarStatus />{/snippet}
      </Topbar>
      <main class="content" id="content" tabindex="-1">
        <WorkflowBanner />
        {#key router.page}
          {#if Page}<Page />{:else if !PAGES[router.page]}<Overview />{:else if unloadable === router.page}<div class="span-12"><Empty error text={t("app.pageFailed")}><button class="btn secondary small" type="button" onclick={() => globalThis.location.reload()}>{t("app.reload")}</button></Empty></div>{/if}
        {/key}
      </main>
    </div>
    <BottomNav />
  </div>
  <Appearance />
{/if}
