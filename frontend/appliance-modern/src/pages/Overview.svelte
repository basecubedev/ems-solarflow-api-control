<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import Bar from "../../../shared/ui/Bar.svelte";
  import KeyValues from "../widgets/KeyValues.svelte";
  import FindingList from "../widgets/FindingList.svelte";
  import PlanButton from "../widgets/PlanButton.svelte";
  import { t, tn, fmt, say, langOf } from "../i18n/i18n.svelte.js";
  import { href } from "../app/router.svelte.js";
  import { host } from "../state/host.svelte.js";
  import { session } from "../state/session.svelte.js";
  import { detail } from "../state/prefs.svelte.js";
  import { findingWords, overviewVerdict, rankedFindings, sessionFindings } from "../lib/findings.js";
  import { adminContainer, adminHealth, adminTransitionLive, adminVersion, daemonState, dockerCondition, dockerTone, emsContainer, gigabytes, indexAge, osTiles, securityUpdatesOffered, systemVerdict, updatesInDoubt } from "../lib/host.js";
  import { adminRepairPlan, lifecyclePlan, powerPlan, securityUpdatesPlan } from "../lib/plans.js";
  import { CONNECTIVITY, CONTAINER_STATES, containerTone, word } from "../lib/words.js";

  const status = $derived(host.status);
  const extra = $derived(sessionFindings(session.audit, session.confirmed));
  const verdict = $derived(overviewVerdict(status, extra));
  const findings = $derived(status === undefined ? [] : rankedFindings(status, extra));
  const system = $derived(status?.system || {});
  const admin = $derived(status?.admin || {});
  const updates = $derived(status?.updates || {});
  const tiles = $derived(osTiles(updates));
  const network = $derived(status?.network || {});
  const daemon = $derived(daemonState(status));
  const ems = $derived(emsContainer(status));
  const health = $derived(adminHealth(status));
  const version = $derived(adminVersion(status));
  const root = $derived(system.storage?.root || {});
  const free = $derived(gigabytes(root.free_mb));
  const age = $derived(indexAge(updates));
  const osVerdict = $derived(systemVerdict(updates, findings));
  const osWords = $derived(osVerdict.finding ? findingWords(osVerdict.finding, status).title : { key: osVerdict.key });
  const bootstrap = $derived(admin.bootstrap_required === true);
  const HERO = { ok: "circle-check", warn: "triangle-alert", fault: "circle-x", idle: "loader" };
  const TONE = { ok: "c-ok", warn: "c-warn", fault: "c-fault", idle: "c-idle" };
  const hostLevel = $derived(status?.health?.level);
  const levelTone = $derived(hostLevel === "healthy" ? "c-ok" : hostLevel === "degraded" ? "c-fault" : hostLevel ? "c-warn" : "c-idle");
  const emsTone = $derived(ems ? containerTone(ems.state) : "c-idle");
  const container = (state) => say(word("container", CONTAINER_STATES, state));
  const containerLang = (state) => langOf(word("container", CONTAINER_STATES, state));
  const yesNo = (value) => (value === true ? t("common.yes") : value === false ? t("common.no") : null);
</script>

<section class="card span-12 hero {TONE[verdict.tone]}" aria-labelledby="verdict" data-testid="overview-hero">
  <div class="hero-head">
    <span class="icon-tile"><Icon name={HERO[verdict.tone]} /></span>
    <div class="hero-text">
      <h2 class="verdict" id="verdict" data-testid="overview-verdict" data-tone={verdict.tone}>{t(verdict.key)}</h2>
      <p class="muted">
        {#if findings.length}{tn("overview.attention", findings.length)}{:else if status}{t("overview.nothing")}{:else}{t("overview.reading")}{/if}
        {#if host.readAt}<span class="num">{" "}· {t("overview.readAt", { time: fmt().time(host.readAt, { seconds: true }) })}</span>{/if}
      </p>
    </div>
  </div>
  {#if findings.length}
    <div class="hero-findings"><FindingList {findings} {status} page="overview" /></div>
  {/if}
</section>

<Card icon="cpu" tone="c-inv" title={t("overview.pi")} subtitle={system.hardware?.model || t("common.unknown")} span="span-4">
  {#snippet actions()}<Status tone={levelTone} text={hostLevel ? t(`level.${hostLevel}`) : t("common.unknown")} />{/snippet}
  <div data-testid="card-host">
    <KeyValues
      rows={[
        { label: t("overview.os"), value: system.operating_system?.name },
        { label: t("overview.uptime"), value: system.uptime?.days !== undefined ? tn("unit.day", system.uptime.days) : null },
        { label: t("overview.temperature"), value: system.temperature?.celsius ? `${fmt().number(system.temperature.celsius, 1)} °C` : null },
        { label: t("overview.freeStorage"), value: free ? `${fmt().number(free.value, free.digits)} GB` : null },
      ]}
    />
    {#if root.used_percent !== undefined}<div class="usage"><Bar value={root.used_percent} label={t("overview.storageUsed")} /></div>{/if}
  </div>
</Card>

<Card icon="zap" tone="c-out" title={t("overview.ems")} subtitle={t("overview.emsSub")} span="span-4">
  {#snippet actions()}<Status tone={emsTone} text={ems ? container(ems.state) || t("common.unknown") : t("common.unknown")} lang={ems ? containerLang(ems.state) : undefined} />{/snippet}
  <div data-testid="card-ems">
    <KeyValues rows={[{ label: t("overview.container"), value: ems?.name }]} />
    {#if ems && ems.state !== "running" && ems.exists !== false}<p class="note warn"><Icon name="triangle-alert" />{t("overview.emsStopped")}</p>{/if}
    {#if ems?.exists === false}<p class="note"><Icon name="info" />{t("overview.emsMissing")}</p>{/if}
  </div>
</Card>

<Card icon="server" tone="c-inv" title={t("overview.admin")} subtitle={t("overview.adminSub")} span="span-4">
  {#snippet actions()}<Status tone={health.tone} text={t(health.key)} />{/snippet}
  <div data-testid="card-admin">
    <p class="big num" data-testid="admin-version">{version.key ? t(version.key) : version.text}</p>
    <KeyValues
      rows={[
        { label: t("overview.container"), value: container(adminContainer(status).state), lang: containerLang(adminContainer(status).state) },
        detail.expert && { label: t("overview.digest"), value: admin.digest, mono: true },
      ]}
    />
  </div>
</Card>

<Card icon="circle-arrow-up" tone="c-out" title={t("overview.updates")} subtitle={t("overview.updatesSub")} span="span-4">
  {#snippet actions()}
    {#if securityUpdatesOffered(updates)}
      <Status tone="c-warn" text={tn("overview.securityWaiting", Number(updates.security_count))} />
    {:else if updates.security_count !== undefined}
      <span data-testid="overview-updates-state"><Status tone={osVerdict.tone} text={say(osWords)} lang={langOf(osWords)} /></span>
    {/if}
  {/snippet}
  <div data-testid="card-updates">
    <KeyValues
      rows={[
        { label: t("overview.securityUpdates"), value: tiles.securityCount },
        { label: t("overview.otherUpdates"), value: tiles.normalCount },
        { label: t("overview.index"), value: age ? tn(`unit.${age.unit}Old`, age.count) : updates.security_count === undefined ? null : t("common.unknown") },
        { label: t("overview.rebootRequired"), value: yesNo(updates.reboot_required) },
      ]}
    />
  </div>
</Card>

<Card icon="package" tone="c-inv" title={t("overview.docker")} subtitle={t("overview.dockerSub")} span="span-4">
  {#snippet actions()}<Status tone={daemon ? dockerTone(daemon) : "c-idle"} text={daemon ? t(`docker.${dockerCondition(daemon)}`) : t("common.unknown")} />{/snippet}
  <div data-testid="card-docker">
    {#if daemon === "unavailable"}<p class="note"><Icon name="info" />{t("overview.dockerMissing")}</p>{/if}
    <KeyValues rows={[detail.expert && { label: t("overview.engine"), value: status?.docker?.daemon?.version }]} />
  </div>
</Card>

<Card icon="wifi" tone="c-out" title={t("overview.network")} subtitle={network.hostname || t("common.unknown")} span="span-4">
  {#snippet actions()}{#if network.connectivity}<Status tone={network.connectivity === "full" ? "c-ok" : "c-warn"} text={say(word("connectivity", CONNECTIVITY, network.connectivity))} lang={langOf(word("connectivity", CONNECTIVITY, network.connectivity))} />{/if}{/snippet}
  <div data-testid="card-network">
    <KeyValues
      rows={[
        { label: t("overview.mdns"), value: network.mdns },
        { label: t("overview.activeConnection"), value: network.active_connection },
        { label: t("overview.addresses"), value: (network.interfaces || []).flatMap((item) => item.addresses || []).filter((value, index, all) => all.indexOf(value) === index).join(", ") || null },
      ]}
    />
  </div>
</Card>

<Card icon="wrench" tone="c-soc" title={t("overview.actions")} subtitle={t("overview.actionsSub")} span="span-12">
  <div class="quick">
    <div class="group" data-testid="quick-admin">
      <div class="group-head"><span class="icon-tile sm c-inv"><Icon name="server" /></span><div><strong>{bootstrap ? t("overview.installAdmin") : t("overview.adminRecovery")}</strong><p class="muted">{bootstrap ? t("overview.installAdminSub") : t("overview.adminRecoverySub")}</p></div></div>
      <div class="row-wrap">
        {#if bootstrap}
          <a class="btn small" href={href("admin")} data-testid="quick-install-admin"><Icon name="download" />{t("overview.installAdmin")}</a>
        {:else}
          <PlanButton plan={lifecyclePlan("restart", "quick-restart-admin")} label={t("overview.restartAdmin")} icon="rotate-cw" disabled={adminTransitionLive(status)} testid="quick-restart-admin" />
          <PlanButton plan={adminRepairPlan("quick-repair-admin")} label={t("overview.repairAdmin")} icon="wrench" variant="secondary" disabled={adminTransitionLive(status)} testid="quick-repair-admin" />
          <a class="btn secondary small" href={href("admin")}>{t("updates.openAdmin")}</a>
        {/if}
      </div>
    </div>
    <div class="group" data-testid="quick-updates">
      <div class="group-head"><span class="icon-tile sm c-out"><Icon name="circle-arrow-up" /></span><div><strong>{t("overview.updates")}</strong><p class="muted">{t("overview.updatesAll")}</p></div></div>
      <div class="row-wrap">
        <a class="btn small" href={href("updates")} data-testid="quick-open-updates">{t("overview.openUpdates")}</a>
        <PlanButton plan={securityUpdatesPlan("quick-security-updates")} label={t("overview.installSecurity")} variant="secondary" disabled={!securityUpdatesOffered(updates)} testid="quick-security-updates" />
      </div>
      {#if !securityUpdatesOffered(updates) && status}
        <p class="note" data-testid="quick-security-reason"><Icon name="info" />{updatesInDoubt(updates) ? t("overview.securityDoubt") : t("overview.securityNone")}</p>
      {/if}
    </div>
    <div class="group" data-testid="quick-power">
      <div class="group-head"><span class="icon-tile sm c-pv"><Icon name="power" /></span><div><strong>{t("overview.power")}</strong><p class="muted">{t("overview.powerSub")}</p></div></div>
      <div class="row-wrap">
        <PlanButton plan={powerPlan("reboot")} label={t("overview.reboot")} icon="rotate-ccw" variant="secondary" testid="quick-reboot" />
        <PlanButton plan={powerPlan("shutdown")} label={t("overview.shutdown")} icon="power" variant="secondary" testid="quick-shutdown" />
      </div>
    </div>
  </div>
</Card>

<style>
  .hero { border-color: color-mix(in srgb, var(--c) 35%, var(--m-line)); background: linear-gradient(180deg, color-mix(in srgb, var(--c) 9%, var(--m-surface-1)), var(--m-surface-1)); }
  .hero-head { display: flex; gap: var(--s4); align-items: center; }
  .hero .icon-tile { width: 52px; height: 52px; border-radius: 15px; }
  .hero .icon-tile :global(svg.i) { width: 26px; height: 26px; }
  .verdict { margin: 0; font-size: 20px; font-weight: 650; letter-spacing: -0.015em; }
  .hero-text p { margin: 4px 0 0; font-size: 13px; }
  .hero-findings { margin-top: var(--s4); }
  .big { font-size: 22px; font-weight: 650; margin: 0 0 var(--s3); }
  .usage { margin-top: var(--s3); }
  .quick { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--s4); }
  .group { display: flex; flex-direction: column; gap: var(--s3); padding: var(--s4); border-radius: var(--m-radius-inner); background: var(--m-surface-2); }
  .group-head { display: flex; gap: var(--s3); align-items: flex-start; }
  .group-head p { margin: 2px 0 0; font-size: 12.5px; }
  .group .note { margin: 0; }
  @container content (max-width: 900px) {
    .quick { grid-template-columns: minmax(0, 1fr); }
  }
</style>
