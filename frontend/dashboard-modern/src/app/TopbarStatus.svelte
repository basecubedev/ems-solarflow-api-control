<script>
  import { t } from "../i18n/i18n.svelte.js";
  import { live } from "../state/live.svelte.js";
  import { systemStatus } from "../state/derived.svelte.js";

  const status = $derived(systemStatus());
  const ageSeconds = $derived(live.changedAt ? Math.max(0, Math.round((live.now - live.changedAt) / 1000)) : null);
  const transportKey = $derived(status.level === "stale" ? "transport.stale" : `transport.${live.transport}`);
  const transportTone = $derived(
    status.level === "stale" || live.transport === "offline" ? "fault" : live.transport === "live" ? "ok" : "warn",
  );
  const emsKey = $derived(
    status.level === "waiting" ? "top.waiting" : live.snapshot?.controller?.enabled === false ? "top.emsPaused" : "top.emsActive",
  );
  const emsTone = $derived(status.level === "waiting" ? "idle" : live.snapshot?.controller?.enabled === false ? "warn" : "ok");

  function ageText(seconds) {
    if (seconds === null) return "";
    if (seconds >= 120) return t("top.updatedMinutes", { n: Math.round(seconds / 60) });
    return t("top.updatedAgo", { n: seconds });
  }
</script>

<span class="pill" data-testid="ems-state"><span class="dot {emsTone}"></span><strong>{t(emsKey)}</strong></span>
<span class="pill hide-sm" data-testid="transport" aria-live="off">
  <span class="dot {transportTone}"></span><span class="num">{t(transportKey)}{ageSeconds !== null ? ` · ${ageText(ageSeconds)}` : ""}</span>
</span>
