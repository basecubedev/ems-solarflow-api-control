<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import { untrack } from "svelte";
  import DashboardStatus from "../../../dashboard-modern/src/app/TopbarStatus.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { auth } from "../state/auth.svelte.js";
  import { ems } from "../state/ems.svelte.js";
  import { system, ensure } from "../state/system.svelte.js";
  import { healthVerdict } from "../lib/health.js";

  /**
   * The EMS's live state while it answers, as the dashboard shows it, and,
   * signed in, the installation's verdict, which leads to the status page.
   */
  $effect(() => {
    if (auth.authenticated) untrack(() => ensure("overview"));
  });
  const TONES = { ok: "ok", info: "acc", warning: "warn", error: "fault", none: "idle", unreadable: "fault", loading: "idle" };
  const verdict = $derived(healthVerdict(system.overview.data, Boolean(system.overview.error)));
</script>

{#if ems.state === "running"}<DashboardStatus />{/if}
{#if auth.authenticated}
  <a class="pill" href="#/status" data-testid="health-state"><span class="dot {TONES[verdict.alert || verdict.level]}"></span><strong>{t(`health.pill.${verdict.alert || verdict.level}`)}</strong></a>
{/if}

<style>
  a.pill { text-decoration: none; }
  a.pill:hover { border-color: var(--m-line-strong); }
</style>
