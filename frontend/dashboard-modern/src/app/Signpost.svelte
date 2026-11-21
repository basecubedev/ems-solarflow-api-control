<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import { getJson } from "../../../shared/lib/api.js";
  import MovedNotice from "./MovedNotice.svelte";

  /**
   * The whole page on the EMS's own address while the Admin console answers:
   * only the way to the app, with nothing that reads the EMS's data. It asks
   * now and then whether the console still runs, and once it does not, the
   * page starts over as the dashboard itself.
   */
  const RECHECK_MS = 15000;

  $effect(() => {
    const timer = setInterval(async () => {
      if (document.hidden) return;
      const config = await getJson("/api/ui-config").catch(() => null);
      if (config && !config.signpost) globalThis.location.reload();
    }, RECHECK_MS);
    return () => clearInterval(timer);
  });
</script>

<main class="signpost" data-testid="signpost">
  <div class="signpost-card">
    <MovedNotice />
  </div>
</main>

<style>
  .signpost { min-height: 100vh; min-height: 100dvh; display: grid; place-items: center; padding: var(--s4); box-sizing: border-box; }
  .signpost-card { width: 100%; max-width: 36rem; }
</style>
