<script>
  import Sidebar from "../../shared/shell/Sidebar.svelte";
  import Topbar from "../../shared/shell/Topbar.svelte";
  import BottomNav from "../../shared/shell/BottomNav.svelte";
  import Appearance from "../../shared/shell/Appearance.svelte";
  import { provideShell } from "../../shared/shell/context.js";
  import TopbarStatus from "./app/TopbarStatus.svelte";
  import LoginDialog from "./app/LoginDialog.svelte";
  import Overview from "./pages/Overview.svelte";
  import Devices from "./pages/Devices.svelte";
  import Energy from "./pages/Energy.svelte";
  import Analytics from "./pages/Analytics.svelte";
  import Control from "./pages/Control.svelte";
  import Diagnose from "./pages/Diagnose.svelte";
  import Logs from "./pages/Logs.svelte";
  import Maintenance from "./pages/Maintenance.svelte";
  import Schedule from "./pages/Schedule.svelte";
  import { pageSet, pageById } from "./app/pages.js";
  import { nav, router } from "./app/router.svelte.js";
  import { t } from "./i18n/i18n.svelte.js";
  import { prefs, chooseClassic } from "./state/prefs.svelte.js";
  import { auth, logout } from "./state/auth.svelte.js";

  const COMPONENTS = { overview: Overview, devices: Devices, energy: Energy, analytics: Analytics, control: Control, schedule: Schedule, diagnose: Diagnose, logs: Logs, maintenance: Maintenance };
  const Page = $derived(COMPONENTS[router.page] || Overview);

  provideShell({
    pageSet,
    nav,
    short: "EMS",
    locked: (page) => Boolean(page.operator) && !auth.authenticated,
    session: {
      get authenticated() {
        return auth.authenticated;
      },
      get label() {
        return auth.authenticated ? t("auth.writeMode") : t("auth.readOnly");
      },
      get icon() {
        return auth.authenticated ? "lock-open" : "lock";
      },
      signIn: () => (auth.loginOpen = true),
      signOut: logout,
    },
    classic: (pageId) => chooseClassic(pageId ? pageById(pageId).classicView : undefined),
    links: [],
  });
</script>

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
      {#key router.page}
        <Page />
      {/key}
    </main>
  </div>
  <BottomNav />
</div>
<Appearance />
<LoginDialog />
