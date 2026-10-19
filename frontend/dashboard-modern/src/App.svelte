<script>
  import Sidebar from "./app/Sidebar.svelte";
  import Topbar from "./app/Topbar.svelte";
  import BottomNav from "./app/BottomNav.svelte";
  import Appearance from "./app/Appearance.svelte";
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
  import { router } from "./app/router.svelte.js";
  import { t } from "./i18n/i18n.svelte.js";

  const COMPONENTS = { overview: Overview, devices: Devices, energy: Energy, analytics: Analytics, control: Control, schedule: Schedule, diagnose: Diagnose, logs: Logs, maintenance: Maintenance };
  const Page = $derived(COMPONENTS[router.page] || Overview);
</script>

<a
  class="skip-link"
  href="#content"
  onclick={(event) => {
    event.preventDefault();
    document.getElementById("content")?.focus();
  }}>{t("app.skip")}</a>
<div class="app">
  <Sidebar />
  <div class="main">
    <Topbar />
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
