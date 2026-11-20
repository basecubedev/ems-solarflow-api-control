<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
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
  import EmsPage from "./app/EmsPage.svelte";
  import WorkflowBanner from "./widgets/WorkflowBanner.svelte";
  import Overview from "../../dashboard-modern/src/pages/Overview.svelte";
  import LoginDialog from "../../dashboard-modern/src/app/LoginDialog.svelte";
  import Empty from "../../shared/ui/Empty.svelte";
  import { pageSet, pageById, needsConsole } from "./app/pages.js";
  import { nav, router } from "./app/router.svelte.js";
  import { t } from "./i18n/i18n.svelte.js";
  import { prefs } from "./state/prefs.svelte.js";
  import { auth, logout } from "./state/auth.svelte.js";
  import { auth as emsAuth } from "../../dashboard-modern/src/state/auth.svelte.js";
  import { signOutOfEms } from "./state/ems.svelte.js";
  import { system, ensure, forgetSessionErrors } from "./state/system.svelte.js";
  import { resumeJobs } from "./state/backupJobs.svelte.js";
  import { forgetSessionError } from "./state/diagnostics.svelte.js";
  import { forgetSessionRefusal } from "./state/services.svelte.js";
  import { resumeUpgrade } from "./state/upgrade.svelte.js";
  import { setupTransitionActive } from "./lib/setup.js";
  import { updateMark } from "./lib/updates.js";
  import { URGENT_STEPS, assistantMark, assistantSteps, bareAddress, installKind, installed } from "./lib/assistant.js";

  /**
   * Every page but the dashboard's overview, the home, loads when it is first
   * opened, so no single file the console ships outgrows what the repository
   * tracks unasked.
   */
  const PAGES = {
    devices: () => import("../../dashboard-modern/src/pages/Devices.svelte"),
    consumers: () => import("../../dashboard-modern/src/pages/Consumers.svelte"),
    energy: () => import("../../dashboard-modern/src/pages/Energy.svelte"),
    analytics: () => import("../../dashboard-modern/src/pages/Analytics.svelte"),
    control: () => import("../../dashboard-modern/src/pages/Control.svelte"),
    schedule: () => import("../../dashboard-modern/src/pages/Schedule.svelte"),
    logs: () => import("../../dashboard-modern/src/pages/Logs.svelte"),
    status: () => import("./pages/Overview.svelte"),
    assistant: () => import("./pages/Assistant.svelte"),
    updates: () => import("./pages/Updates.svelte"),
    settings: () => import("./pages/Settings.svelte"),
    services: () => import("./pages/Services.svelte"),
    backups: () => import("./pages/Backups.svelte"),
    diagnostics: () => import("./pages/Diagnostics.svelte"),
    system: () => import("./pages/System.svelte"),
    setup: () => import("./pages/Setup.svelte"),
    about: () => import("../../shared/widgets/About.svelte"),
  };
  const loaded = $state({ overview: Overview });
  const Page = $derived(loaded[router.page] || null);
  const page = $derived(pageById(router.page));
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

  /**
   * The dashboard's pages are open to anyone who reaches the address, as the
   * dashboard always was, and a write still needs the EMS's own session. The
   * console's pages need the console's: opening one, or asking to sign in,
   * shows the sign-in in their place, and so does a console with no password
   * yet or a password file it cannot read.
   */
  let signingIn = $state(false);
  const signInShown = $derived(
    auth.loaded && !auth.authenticated && (signingIn || needsConsole(page) || auth.requiresInitialPassword || auth.recoveryRequired || !auth.reachable),
  );
  $effect(() => {
    if (auth.authenticated) signingIn = false;
  });

  /**
   * The console's session leads, the EMS's follows it. A dashboard page that
   * asks for the sign-in while the console is signed out asks for the one
   * sign-in for both; signed in to the console, only the EMS's session can
   * have ended, and the dashboard's own dialog renews it. An EMS session the
   * console does not share (one signed in on the EMS's address, one the
   * console outlived after a restart, or one the EMS keeps longer) is ended,
   * so the menu's Sign out ends every session this browser holds.
   */
  $effect(() => {
    if (emsAuth.loginOpen && auth.loaded && !auth.authenticated) {
      emsAuth.loginOpen = false;
      signingIn = true;
    }
  });
  $effect(() => {
    if (auth.loaded && auth.reachable && !auth.authenticated && emsAuth.authenticated) untrack(() => signOutOfEms());
  });

  /**
   * The menu marks the assistant while a step is left. Its later steps read
   * the settings and the backups, which only an installation has; they are
   * read once it is there, and again where they change.
   */
  const isInstalled = $derived(installed(system.overview.data));
  $effect(() => {
    if (auth.authenticated && isInstalled) untrack(() => ensure("config", "backups"));
  });
  const marked = $derived(assistantMark(assistantSteps({ overview: system.overview.data, config: system.config.data, backups: system.backups.data }), URGENT_STEPS));
  /** The menu's Updates entry carries a dot while an update waits; reading it needs no network. */
  $effect(() => {
    if (auth.authenticated && isInstalled) untrack(() => ensure("updates", "influxdb"));
  });
  const updatesMark = $derived(updateMark(system.updates.data, system.influxdb.data));

  $effect(() => {
    if (auth.authenticated && auth.reachable) {
      untrack(() => {
        resumeJobs();
        forgetSessionErrors();
        forgetSessionError();
        forgetSessionRefusal();
        resumeUpgrade();
        resumeSetup();
      });
    }
  });

  const LANDING_PAGES = ["overview", "status", "assistant"];
  const openedBare = bareAddress(globalThis.location?.hash);
  let welcomed = false;
  let landed = $state(false);

  /**
   * After a sign-in, a reload or an Admin restart, a setup the server still
   * runs opens again in place of the landing pages (the dashboard's overview,
   * the console's status and the assistant); a page the reader asked for by
   * its address stays. A console opened at its bare address on a host with
   * nothing installed shows the assistant, once per load; with an
   * installation, the dashboard. Either replaces the address rather than
   * adding one, so Back leaves the app. ``data-landed`` on the app says the
   * choice is made.
   */
  async function resumeSetup() {
    try {
      const status = await system.alignment.load();
      if (setupTransitionActive(status?.transition) && LANDING_PAGES.includes(router.page)) {
        globalThis.location.replace(nav.href("setup"));
        return;
      }
      if (welcomed || !openedBare) return;
      welcomed = true;
      const overview = await system.overview.ensure();
      if (bareAddress(globalThis.location.hash) && installKind(overview) === "none") globalThis.location.replace(nav.href("assistant"));
    } finally {
      landed = true;
    }
  }

  async function signOut() {
    await logout();
    await signOutOfEms();
  }

  provideShell({
    pageSet,
    nav,
    short: "EMS",
    locked: (item) => (needsConsole(item) ? !auth.authenticated : Boolean(item.operator) && !emsAuth.authenticated),
    mark: (id) => (id === "assistant" ? marked : id === "updates" ? updatesMark : ""),
    session: {
      get authenticated() {
        return auth.authenticated;
      },
      get label() {
        return auth.authenticated ? t("auth.signedIn") : t("auth.readOnly");
      },
      get icon() {
        return auth.authenticated ? "lock-open" : "lock";
      },
      pill: false,
      signIn: () => (signingIn = true),
      signOut,
    },
    links: [],
    about: { notices: [{ name: "uPlot", license: "MIT", file: "/uPlot.LICENSE" }, { name: "Vite", license: "MIT", file: "/Vite.LICENSE" }] },
  });
</script>

{#if !auth.reachable && auth.authenticated}<Reconnecting />{/if}
{#if signInShown || (!auth.loaded && needsConsole(page))}
  <SignIn onContinue={needsConsole(page) ? null : () => (signingIn = false)} />
{:else}
  <a
    class="skip-link"
    href="#content"
    onclick={(event) => {
      event.preventDefault();
      document.getElementById("content")?.focus();
    }}>{t("app.skip")}</a>
  <div class="app" class:rail={prefs.navIconsOnly} data-landed={landed || (auth.loaded && !auth.authenticated) || undefined}>
    <Sidebar />
    <div class="main">
      <Topbar>
        {#snippet status()}<TopbarStatus />{/snippet}
      </Topbar>
      <main class="content" id="content" tabindex="-1">
        {#if auth.authenticated}<WorkflowBanner />{/if}
        {#key router.page}
          {#if Page}
            {#if page.area === "ems" && page.id !== "about"}<EmsPage><Page /></EmsPage>{:else}<Page />{/if}
          {:else if unloadable === router.page}<div class="span-12"><Empty error text={t("app.pageFailed")}><button class="btn secondary small" type="button" onclick={() => globalThis.location.reload()}>{t("app.reload")}</button></Empty></div>{/if}
        {/key}
      </main>
    </div>
    <BottomNav />
  </div>
  <Appearance />
  <LoginDialog />
{/if}
