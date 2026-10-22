<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import Empty from "../../../shared/ui/Empty.svelte";
  import FindingList from "../widgets/FindingList.svelte";
  import ControlState from "../widgets/ControlState.svelte";
  import LinkTile from "../widgets/LinkTile.svelte";
  import StaleNote from "../widgets/StaleNote.svelte";
  import { t, tn, fmt } from "../i18n/i18n.svelte.js";
  import { system, ensure, keepWithOverview, reload } from "../state/system.svelte.js";
  import { healthVerdict, emsFact } from "../lib/health.js";
  import { serviceView } from "../lib/services.js";
  import { controlView } from "../lib/control.js";
  import { latestBackup } from "../lib/backups.js";
  import { dashboardHref } from "../lib/links.js";
  import { href } from "../app/router.svelte.js";
  import { classicPage } from "../state/prefs.svelte.js";
  import { errorView } from "../lib/errors.js";

  ensure("overview", "config", "plan", "backups");
  keepWithOverview("plan");

  const HERO = {
    loading: { tone: "c-idle", icon: "loader" },
    ok: { tone: "c-ok", icon: "circle-check" },
    info: { tone: "c-acc", icon: "info" },
    warning: { tone: "c-warn", icon: "triangle-alert" },
    error: { tone: "c-fault", icon: "circle-x" },
    none: { tone: "c-acc", icon: "package-plus" },
    unreadable: { tone: "c-fault", icon: "circle-help" },
  };

  const f = $derived(fmt());
  const overview = $derived(system.overview.data);
  const verdict = $derived(healthVerdict(overview, Boolean(system.overview.error)));
  const ems = $derived(emsFact(overview));
  const emsService = $derived(serviceView(system.plan.data, "ems"));
  const analytics = $derived(serviceView(system.plan.data, "influxdb"));
  const control = $derived(controlView(system.config.data?.summary?.control, system.config.data?.overrides));
  const lastBackup = $derived(latestBackup(system.backups.data));
  const dashboard = $derived(dashboardHref(overview?.links?.dashboard_url));
  const hero = $derived(HERO[verdict.level] || HERO.warning);
  const refreshing = $derived(system.overview.loading || system.plan.loading);

  function headline() {
    if (verdict.level === "warning" || verdict.level === "error") return tn("overview.attention", verdict.findings.length);
    if (verdict.level === "info") return tn("overview.notes", verdict.findings.length);
    return t(`overview.verdict.${verdict.level}`);
  }

  function emsLine() {
    if (!overview) return "";
    if (!overview.docker?.available) return t("overview.emsUnknown");
    if (!ems.found) return t("overview.emsMissing");
    if (!ems.running) return t("overview.emsStopped");
    const parts = [t("overview.emsRunning")];
    if (ems.version) parts.push(ems.version);
    if (ems.startedAt) parts.push(t("overview.since", { when: f.relative(ems.startedAt) }));
    return parts.join(" · ");
  }

  function refresh() {
    reload("overview", "plan", "config", "backups", "lifecycle");
  }
</script>

{#snippet heroActions()}
  <button class="icon-btn" type="button" onclick={refresh} aria-label={t("common.refresh")} title={t("common.refresh")} disabled={refreshing} data-testid="refresh">
    <Icon name="refresh-cw" />
  </button>
  {#if dashboard && ems.running}<a class="btn secondary small" href={dashboard} data-testid="hero-dashboard"><Icon name="arrow-up-right" size="sm" />{t("nav.dashboard")}</a>{/if}
{/snippet}

<section class="card hero span-12 {hero.tone}" aria-labelledby="overview-verdict" data-level={verdict.level} data-testid="overview-hero">
  <div class="hero-head">
    <span class="icon-tile"><Icon name={hero.icon} /></span>
    <div class="hero-words">
      <h2 class="hero-title" id="overview-verdict">{headline()}</h2>
      {#if verdict.level === "none"}
        <p class="muted">{t("overview.noneLead")}</p>
      {:else if verdict.level !== "unreadable" && emsLine()}
        <p class="muted num">{emsLine()}</p>
      {/if}
    </div>
    <div class="card-actions">{@render heroActions()}</div>
  </div>
  {#if verdict.level === "none"}
    <div class="row-wrap start">
      <a class="btn" href={classicPage("setup")} data-testid="start-setup"><Icon name="arrow-up-right" size="sm" />{t("overview.startSetup")}</a>
      <a class="btn secondary" href={href("backups")}>{t("overview.restoreBackup")}</a>
    </div>
    <p class="note"><Icon name="info" />{t("overview.setupClassic")}</p>
  {:else if verdict.level === "unreadable"}
    <Empty error {...(system.overview.error ? errorView(system.overview.error, t) : { text: t("overview.unreadable") })} />
    {#if overview && system.overview.loadedAt}
      <p class="note" data-testid="stale-note"><Icon name="clock" />{t("overview.stale", { when: f.when(system.overview.loadedAt) })}</p>
    {/if}
  {/if}
  {#if verdict.findings.length}
    <FindingList findings={verdict.findings} onrefresh={refresh} />
  {/if}
</section>

{#if overview && verdict.level !== "none"}
  <div class="tiles span-12" data-testid="overview-tiles">
    <LinkTile
      href={href("services")}
      icon="server"
      tone={emsService.tone}
      label={t("overview.tile.ems")}
      value={emsService.label || t(`service.state.${emsService.state}`)}
      sub={ems.version || ""}
      testid="tile-ems"
    />
    <LinkTile
      href={href("services")}
      icon="database"
      tone={analytics.tone}
      label={t("overview.tile.analytics")}
      value={analytics.label || t(`service.state.${analytics.state}`)}
      testid="tile-analytics"
    />
    <LinkTile
      href={href("settings")}
      icon={control.status === "may_control" ? "shield-check" : "shield-alert"}
      tone={control.tone}
      label={t("overview.tile.control")}
      value={t(`control.short.${control.status}`)}
      sub={control.live ? t("settings.overridden") : ""}
      testid="tile-control"
    />
    <LinkTile
      href={href("backups")}
      icon="archive"
      tone={!system.backups.data ? "c-idle" : lastBackup ? "c-acc" : "c-warn"}
      label={t("overview.tile.backup")}
      value={!system.backups.data ? (system.backups.error ? t("overview.backupUnreadable") : "—") : lastBackup ? f.relative(lastBackup) : t("overview.noBackup")}
      sub={!system.backups.data ? "" : lastBackup ? f.dateTime(lastBackup) : t("overview.noBackupHint")}
      testid="tile-backup"
    />
  </div>

  <Card title={t("control.title")} subtitle={t("control.sub")} span="span-6">
    {#snippet actions()}<a class="btn secondary small" href={`${href("settings")}/safety`}>{t("control.change")}</a>{/snippet}
    <StaleNote resource={system.config} />
    {#if system.config.data?.status === "ok"}
      <ControlState control={system.config.data.summary?.control} overrides={system.config.data.overrides} />
    {:else if system.config.error}
      <Empty error {...errorView(system.config.error, t)} />
    {:else if system.config.data}
      <Empty text={t(`settings.config.${system.config.data.status === "missing" ? "missing" : "invalid"}`)} icon="file-warning" />
    {:else}
      <Empty text={t("common.loading")} icon="loader" />
    {/if}
  </Card>

  <Card title={t("overview.versions")} subtitle={t("overview.versionsSub")} span="span-6">
    {#snippet actions()}<a class="btn secondary small" href={href("updates")}>{t("overview.openUpdates")}</a>{/snippet}
    <div class="facts two">
      <div class="fact"><div class="fact-label">{t("version.admin")}</div><div class="fact-value num" class:na={!overview.components?.admin?.tag}>{overview.components?.admin?.tag || t("version.unknown")}</div></div>
      <div class="fact"><div class="fact-label">{t("version.ems")}</div><div class="fact-value num" class:na={!ems.version}>{ems.version || t("version.unknown")}</div></div>
      <div class="fact"><div class="fact-label">{t("version.analytics")}</div><div class="fact-value num" class:na={!overview.containers?.influxdb?.tag}>{overview.containers?.influxdb?.tag || "—"}</div></div>
      <div class="fact"><div class="fact-label">{t("version.docker")}</div><div class="fact-value num" class:na={!overview.docker?.server_version}>{overview.docker?.server_version || "—"}</div></div>
    </div>
    {#if dashboard}
      <a class="dash-link inner" href={dashboard}><Icon name="layout-dashboard" /><span><strong>{t("overview.dashboard")}</strong><span class="muted">{dashboard}</span></span><Icon name="arrow-up-right" size="sm" /></a>
    {/if}
  </Card>
{/if}

<style>
  .hero { display: flex; flex-direction: column; gap: var(--s4); border-color: color-mix(in srgb, var(--c) 30%, var(--m-line)); }
  .hero-head { display: flex; align-items: center; gap: var(--s4); flex-wrap: wrap; }
  .hero-head > .icon-tile { width: 48px; height: 48px; border-radius: 14px; }
  .hero-head > .icon-tile :global(svg.i) { width: 24px; height: 24px; }
  .hero-words { flex: 1 1 200px; min-width: 0; }
  .hero-title { margin: 0; font-size: 19px; font-weight: 650; letter-spacing: -0.01em; }
  .hero-words p { margin: 3px 0 0; }
  .start { gap: var(--s2); }
  .tiles { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--s3); }
  .dash-link { display: flex; align-items: center; gap: var(--s3); margin-top: var(--s3); text-decoration: none; border: 1px solid var(--m-line); }
  .dash-link:hover { border-color: var(--m-line-strong); }
  .dash-link > span { display: flex; flex-direction: column; min-width: 0; flex: 1; }
  .dash-link .muted { font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  @container content (max-width: 900px) { .tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
  @container content (max-width: 340px) { .tiles { grid-template-columns: minmax(0, 1fr); } }
  @container content (max-width: 520px) {
    .hero-head { gap: var(--s3); }
    .hero-head > .icon-tile { width: 40px; height: 40px; border-radius: 12px; }
    .hero-head > .card-actions { margin-left: 0; width: 100%; }
  }
</style>
