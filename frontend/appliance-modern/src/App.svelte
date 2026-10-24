<script>
  import { tick, untrack } from "svelte";
  import Sidebar from "../../shared/shell/Sidebar.svelte";
  import Topbar from "../../shared/shell/Topbar.svelte";
  import BottomNav from "../../shared/shell/BottomNav.svelte";
  import Appearance from "../../shared/shell/Appearance.svelte";
  import { provideShell } from "../../shared/shell/context.js";
  import SignIn from "./app/SignIn.svelte";
  import Reconnect from "./app/Reconnect.svelte";
  import TopbarStatus from "./app/TopbarStatus.svelte";
  import OperationBanner from "./widgets/OperationBanner.svelte";
  import ActionNotice from "./widgets/ActionNotice.svelte";
  import PlanDialog from "./widgets/PlanDialog.svelte";
  import Overview from "./pages/Overview.svelte";
  import Updates from "./pages/Updates.svelte";
  import Admin from "./pages/Admin.svelte";
  import Network from "./pages/Network.svelte";
  import Access from "./pages/Access.svelte";
  import Diagnostics from "./pages/Diagnostics.svelte";
  import Settings from "./pages/Settings.svelte";
  import { pageSet } from "./app/pages.js";
  import { nav, router } from "./app/router.svelte.js";
  import { t } from "./i18n/i18n.svelte.js";
  import { prefs, chooseClassic } from "./state/prefs.svelte.js";
  import { session, logout } from "./state/session.svelte.js";
  import { host, ensure, pollWhile, refresh, resetHost, stopPolling } from "./state/host.svelte.js";
  import { actions } from "./state/actions.svelte.js";
  import { adminLink } from "./lib/links.js";
  import { attentionBySection, sessionFindings } from "./lib/findings.js";

  /** The whole console is one bundle: a Manager update replaces every file at once, and a page split across two versions is worse than one more kilobyte. */
  const PAGES = { overview: Overview, updates: Updates, admin: Admin, network: Network, access: Access, diagnostics: Diagnostics, settings: Settings };
  const Page = $derived(PAGES[router.page] || Overview);

  let signedIn = false;
  pollWhile(() => session.authenticated && !actions.reconnect);

  /** A new session reads the host from nothing; the tick runs only while signed in and while the host answers. */
  $effect(() => {
    const live = session.authenticated && !actions.reconnect;
    if (!live) {
      if (!session.authenticated) {
        signedIn = false;
        lastAdmin = "";
      }
      return;
    }
    untrack(() => {
      if (!signedIn) {
        signedIn = true;
        resetHost();
      }
      refresh();
    });
    return () => stopPolling();
  });

  /** The settings carry the Admin port the menu links to; a settled operation drops them, so they are asked for again. */
  $effect(() => {
    if (session.authenticated && !actions.reconnect && !host.rereading && host.settings === undefined) untrack(() => ensure("settings"));
  });

  /**
   * A page change moves focus to the new page, as Classic's moves it to the
   * page heading: the link that was pressed is gone with the page it was on.
   * A page picked from the phone's More sheet waits for the sheet to close,
   * because the page behind a modal cannot take focus and the closing sheet
   * hands focus back to the More button.
   */
  let shownPage = router.page;
  $effect(() => {
    const page = router.page;
    if (page === shownPage) return;
    shownPage = page;
    tick().then(() => {
      const focus = () => document.getElementById("content")?.focus({ preventScroll: true });
      const sheet = document.querySelector("dialog.sheet[open]");
      if (sheet) sheet.addEventListener("close", focus, { once: true });
      else focus();
    });
  });

  /**
   * Another Manager now answers -- an update, or a step back to the kept
   * package, possibly one without this console: the page loads again from its
   * address, and what answers there is that version's to decide, instead of
   * this page running against an API it was not built for. A reload, not a
   * move: the console lives at the address itself, so going there would only
   * change the hash. Each source is compared with itself: the session's version
   * from the first answer, before any sign-in, and the status's from the first
   * status read.
   */
  function versionWatch(read) {
    let first = "";
    return () => {
      const running = read() || "";
      if (!running) return;
      if (!first) first = running;
      else if (running !== first) globalThis.location.reload();
    };
  }
  $effect(versionWatch(() => session.version));
  $effect(versionWatch(() => host.status?.appliance_version));

  /**
   * The link to EMS Admin keeps the address it last had while the settings are
   * read again (a settled operation or a Refresh drops them) or that read
   * fails, so the menu entry -- and a focus on it -- stays where it is.
   */
  let lastAdmin = "";
  const admin = $derived.by(() => {
    if (!host.settings || host.settings.error) return host.status?.admin?.installed === false ? "" : lastAdmin;
    lastAdmin = adminLink(globalThis.location, host.settings, host.status);
    return lastAdmin;
  });
  const version = $derived(host.status?.appliance_version || session.version);
  const marks = $derived(attentionBySection(host.status, sessionFindings(session.audit, session.confirmed)));

  provideShell({
    pageSet,
    nav,
    short: "HOST",
    locked: () => false,
    mark: (id) => marks[id] || "",
    session: {
      get authenticated() {
        return session.authenticated;
      },
      get label() {
        return t("auth.signedIn");
      },
      icon: "lock-open",
      pill: false,
      signIn: () => {},
      signOut: logout,
    },
    classic: () => chooseClassic(),
    get links() {
      return admin ? [{ href: admin, icon: "external-link", label: "nav.openAdmin", testid: "open-admin" }] : [];
    },
  });
</script>

{#if actions.reconnect}
  <Reconnect />
{:else if !session.authenticated}
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
    <Sidebar version={version ? t("app.version", { version }) : ""} />
    <div class="main">
      <Topbar>
        {#snippet status()}<TopbarStatus />{/snippet}
      </Topbar>
      <main class="content" id="content" tabindex="-1">
        <OperationBanner />
        <ActionNotice />
        {#key router.page}<Page />{/key}
      </main>
    </div>
    <BottomNav />
  </div>
  <Appearance />
  <PlanDialog />
{/if}
