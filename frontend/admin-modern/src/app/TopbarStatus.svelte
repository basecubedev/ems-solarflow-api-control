<script>
  import { t } from "../i18n/i18n.svelte.js";
  import { system, ensure } from "../state/system.svelte.js";
  import { healthVerdict } from "../lib/health.js";

  ensure("overview");
  const TONES = { ok: "ok", info: "acc", warning: "warn", error: "fault", none: "idle", unreadable: "fault", loading: "idle" };
  const verdict = $derived(healthVerdict(system.overview.data, Boolean(system.overview.error)));
</script>

<a class="pill" href="#/overview" data-testid="health-state"><span class="dot {TONES[verdict.alert || verdict.level]}"></span><strong>{t(`health.pill.${verdict.alert || verdict.level}`)}</strong></a>

<style>
  a.pill { text-decoration: none; }
  a.pill:hover { border-color: var(--m-line-strong); }
</style>
