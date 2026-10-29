<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import Segmented from "../../../shared/ui/Segmented.svelte";
  import { t } from "../i18n/i18n.svelte.js";
  import { host, refreshEverything } from "../state/host.svelte.js";
  import { session } from "../state/session.svelte.js";
  import { detail, setDetail, DETAILS } from "../state/prefs.svelte.js";
  import { overviewVerdict, sessionFindings } from "../lib/findings.js";

  const TONES = { ok: "ok", warn: "warn", fault: "fault", idle: "idle" };
  const verdict = $derived(overviewVerdict(host.status, sessionFindings(session.audit, session.confirmed)));
  let refreshing = $state(false);

  async function again() {
    if (refreshing) return;
    refreshing = true;
    try {
      await refreshEverything();
    } finally {
      refreshing = false;
    }
  }
</script>

<a class="pill" href="#/overview" data-testid="health-state" data-tone={verdict.tone}><span class="dot {TONES[verdict.tone]}"></span><strong>{t(`${verdict.key}.short`)}</strong></a>
<span class="sr-only" role="status" aria-live="polite">{verdict.key === "verdict.loading" ? "" : t(verdict.key)}</span>
<span class="hide-sm">
  <Segmented options={DETAILS.map((value) => ({ value, label: t(`detail.${value}`) }))} value={detail.level} label={t("detail.label")} onchange={setDetail} />
</span>
<button class="icon-btn" type="button" onclick={again} disabled={refreshing} aria-busy={refreshing ? "true" : undefined} aria-label={t("common.refresh")} title={t("common.refresh")} data-testid="refresh">
  <Icon name={refreshing ? "loader" : "refresh-cw"} />
</button>

<style>
  a.pill { text-decoration: none; }
  a.pill:hover { border-color: var(--m-line-strong); }
</style>
