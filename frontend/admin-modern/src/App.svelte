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
  import Services from "./pages/Services.svelte";
  import Settings from "./pages/Settings.svelte";
  import Backups from "./pages/Backups.svelte";
  import Updates from "./pages/Updates.svelte";
  import Diagnostics from "./pages/Diagnostics.svelte";
  import System from "./pages/System.svelte";
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

  const COMPONENTS = { overview: Overview, updates: Updates, settings: Settings, services: Services, backups: Backups, diagnostics: Diagnostics, system: System };
  const Page = $derived(COMPONENTS[router.page] || Overview);
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
          <Page />
        {/key}
      </main>
    </div>
    <BottomNav />
  </div>
  <Appearance />
{/if}
