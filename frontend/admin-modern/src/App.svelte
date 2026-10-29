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
  import { prefs, chooseClassic, welcome } from "./state/prefs.svelte.js";
  import { auth, logout } from "./state/auth.svelte.js";
  import { system, ensure, forgetSessionErrors } from "./state/system.svelte.js";
  import { resumeJobs } from "./state/backupJobs.svelte.js";
  import { forgetSessionError } from "./state/diagnostics.svelte.js";
  import { forgetSessionRefusal } from "./state/services.svelte.js";
  import { resumeUpgrade } from "./state/upgrade.svelte.js";
  import { setupTransitionActive } from "./lib/setup.js";
  import { dashboardHref } from "./lib/links.js";
  import { URGENT_STEPS, assistantMark, assistantSteps, bareAddress, installed, opensOnAssistant } from "./lib/assistant.js";

  /**
   * Every page but the overview loads when it is first opened, so no single
   * file the console ships outgrows what the repository tracks unasked.
   */
  const PAGES = {
    assistant: () => import("./pages/Assistant.svelte"),
    updates: () => import("./pages/Updates.svelte"),
    settings: () => import("./pages/Settings.svelte"),
    services: () => import("./pages/Services.svelte"),
    backups: () => import("./pages/Backups.svelte"),
    diagnostics: () => import("./pages/Diagnostics.svelte"),
    system: () => import("./pages/System.svelte"),
    setup: () => import("./pages/Setup.svelte"),
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

  const LANDING_PAGES = ["overview", "assistant"];
  const openedBare = bareAddress(globalThis.location?.hash);
  let welcomed = false;
  let leaving = false;
  let landed = $state(false);

  /**
   * After a sign-in, a reload or an Admin restart, a setup the server still
   * runs opens again in place of the landing pages, the overview and the
   * assistant, as Classic reopens it; a page the reader asked for by its
   * address stays. Otherwise a console opened at its bare address shows the
   * assistant, once per load: always while nothing is installed, else while
   * the reader leaves it on. Either replaces the address rather than adding
   * one, so Back leaves the console, and neither runs once the reader left
   * for Classic during this run; a later run, after a sign-in, finds the
   * reader still here. ``data-landed`` on the app says the choice is made.
   */
  async function resumeSetup() {
    leaving = false;
    try {
      const status = await system.alignment.load();
      if (leaving) return;
      if (setupTransitionActive(status?.transition) && LANDING_PAGES.includes(router.page)) {
        globalThis.location.replace(nav.href("setup"));
        return;
      }
      if (welcomed || !openedBare) return;
      welcomed = true;
      const overview = welcome.choice === "off" ? await system.overview.ensure() : null;
      if (!leaving && bareAddress(globalThis.location.hash) && opensOnAssistant(overview, welcome.choice)) globalThis.location.replace(nav.href("assistant"));
    } finally {
      landed = true;
    }
  }

  provideShell({
    pageSet,
    nav,
    short: "ADMIN",
    locked: () => false,
    mark: (id) => (id === "assistant" ? marked : ""),
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
    classic: (pageId) => {
      leaving = true;
      chooseClassic(pageId ? pageById(pageId).classic : "maintenance");
    },
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
  <div class="app" class:rail={prefs.navIconsOnly} data-landed={landed || undefined}>
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
