<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
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
  import Consumers from "./pages/Consumers.svelte";
  import Energy from "./pages/Energy.svelte";
  import Diagnose from "./pages/Diagnose.svelte";
  import Logs from "./pages/Logs.svelte";
  import Maintenance from "./pages/Maintenance.svelte";
  import Schedule from "./pages/Schedule.svelte";
  import About from "../../shared/widgets/About.svelte";
  import Empty from "../../shared/ui/Empty.svelte";
  import { pageSet } from "./app/pages.js";
  import { nav, router } from "./app/router.svelte.js";
  import { t } from "./i18n/i18n.svelte.js";
  import { prefs } from "./state/prefs.svelte.js";
  import { auth, logout } from "./state/auth.svelte.js";
  import { live } from "./state/live.svelte.js";
  import { consoleAddress } from "./state/console.svelte.js";
  import { INSTALL_CONSOLE, consoleTarget } from "./lib/console.js";

  const COMPONENTS = { overview: Overview, devices: Devices, consumers: Consumers, energy: Energy, schedule: Schedule, diagnose: Diagnose, logs: Logs, maintenance: Maintenance, about: About };

  /**
   * Analytics, the largest page and one only InfluxDB fills, and Control, whose
   * runtime cards carry the settings controls, load when first opened, as the
   * console loads its pages, so the dashboard's own file stays within what the
   * repository tracks unasked.
   */
  const LAZY = { analytics: () => import("./pages/Analytics.svelte"), control: () => import("./pages/Control.svelte") };
  const loaded = $state({});
  let unloadable = $state("");
  const Page = $derived(COMPONENTS[router.page] || loaded[router.page] || (LAZY[router.page] ? null : Overview));

  /** A page whose file does not arrive, as while the EMS restarts, says so and offers a reload. */
  $effect(() => {
    const id = router.page;
    if (loaded[id] || !LAZY[id]) return;
    unloadable = "";
    LAZY[id]().then(
      (module) => {
        loaded[id] = module.default;
      },
      () => {
        if (router.page === id) unloadable = id;
      },
    );
  });

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
        if (!auth.authenticated) return t("auth.readOnly");
        return live.runtimeUnavailable ? t("auth.writeModeRetrying") : t("auth.writeMode");
      },
      get icon() {
        return auth.authenticated ? "lock-open" : "lock";
      },
      signIn: () => (auth.loginOpen = true),
      signOut: logout,
    },
    get links() {
      if (!live.uiConfigLoaded) return [];
      const target = consoleTarget(consoleAddress.record);
      return target
        ? [{ href: target.href, icon: "layout-dashboard", label: "nav.console", testid: "open-console" }]
        : [{ href: INSTALL_CONSOLE, icon: "arrow-up-right", label: "nav.installConsole", testid: "install-console", external: true }];
    },
    about: { notices: [{ name: "uPlot", license: "MIT", file: "/uPlot.LICENSE" }, { name: "Vite", license: "MIT", file: "/Vite.LICENSE" }] },
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
        {#if Page}
          <Page />
        {:else if unloadable === router.page}
          <div class="span-12"><Empty error text={t("app.pageFailed")}><button class="btn secondary small" type="button" onclick={() => globalThis.location.reload()}>{t("app.reload")}</button></Empty></div>
        {/if}
      {/key}
    </main>
  </div>
  <BottomNav />
</div>
<Appearance />
<LoginDialog />
