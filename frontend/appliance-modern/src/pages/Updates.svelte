<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import KeyValues from "../widgets/KeyValues.svelte";
  import PlanButton from "../widgets/PlanButton.svelte";
  import UnconfirmedNote from "../widgets/UnconfirmedNote.svelte";
  import { session } from "../state/session.svelte.js";
  import InstallForm from "../widgets/InstallForm.svelte";
  import TrackHelp from "../widgets/TrackHelp.svelte";
  import PackageTable from "../widgets/PackageTable.svelte";
  import { t, tn, say, langOf } from "../i18n/i18n.svelte.js";
  import { href } from "../app/router.svelte.js";
  import { host, ensure, held } from "../state/host.svelte.js";
  import { forms } from "../state/forms.svelte.js";
  import { detail } from "../state/prefs.svelte.js";
  import { rankedFindings, findingWords } from "../lib/findings.js";
  import { adminGate, adminVersion, daemonState, dockerCondition, groupByTrack, latestVerdict, osTiles, securityUpdatesOffered, systemVerdict, updatesInDoubt } from "../lib/host.js";
  import { MANAGER_OUTCOMES, MANAGER_VERDICTS, applianceNow, latestStableVersion, managerActions, managerOptions, riskyDirection } from "../lib/manager.js";
  import { REPAIR_ACTIONS, adminInstallPlan, allUpdatesPlan, managerInstallPlan, managerRevertPlan, repairPlan, securityUpdatesPlan } from "../lib/plans.js";

  /**
   * Everything on this appliance that has a version, in one place. Nothing
   * updates on its own: every install shows its plan first and waits for the
   * owner's confirmation.
   */
  const status = $derived(host.status);
  const updates = $derived(status?.updates || {});
  const tiles = $derived(osTiles(updates));
  const manager = $derived(host.manager);
  const managerKnown = $derived(Boolean(manager) && !manager.error);
  const sources = $derived(held("managerSources"));
  const releases = $derived(held("releases"));
  const gate = $derived(adminGate(status));
  const now = $derived(applianceNow(status));
  const rights = $derived(managerActions(manager, now));
  const unconfirmed = $derived(session.confirmed === false);
  const docker = $derived(dockerCondition(daemonState(status)));

  $effect(() => {
    if (managerKnown && host.managerSources === undefined) ensure("managerSources");
  });
  $effect(() => {
    if (["missing", "live", "ready"].includes(gate) && host.releases === undefined) ensure("releases");
  });

  const managerLatest = $derived(sources?.latest_stable || null);
  const managerLatestVersion = $derived(latestStableVersion(sources));
  const managerSummary = $derived(
    manager && manager.error
      ? { key: "updates.manager.stateUnavailable", tone: "c-idle" }
      : !sources
        ? { key: "updates.checking", tone: "c-idle" }
        : sources.error
          ? { key: "updates.manager.indexUnreadable", tone: "c-fault" }
          : !sources.configured
            ? { key: "updates.manager.noIndex", tone: "c-idle" }
            : !managerLatest
              ? { key: "updates.manager.noStable", tone: "c-idle" }
              : latestVerdict(managerLatest),
  );

  const adminLatest = $derived(releases?.latest_stable || null);
  const adminSummary = $derived(
    gate === "unread"
      ? { key: "updates.admin.unavailable", tone: "c-idle" }
      : gate === "bootstrap"
        ? { key: "updates.admin.notInstalled", tone: "c-warn" }
        : gate === "docker"
          ? { key: docker === "unavailable" ? "docker.notInstalled" : "docker.notRunning", tone: "c-fault" }
          : gate === "missing"
            ? { key: "updates.admin.containerMissing", tone: "c-fault" }
            : !releases
              ? { key: "updates.checking", tone: "c-idle" }
              : releases.error
                ? { key: "updates.admin.listUnavailable", tone: "c-fault" }
                : !adminLatest
                  ? { key: "updates.admin.noStable", tone: "c-idle" }
                  : latestVerdict(adminLatest),
  );

  const findings = $derived(rankedFindings(status));
  const osVerdict = $derived(systemVerdict(updates, findings));
  const osWords = $derived(osVerdict.finding ? findingWords(osVerdict.finding, status).title : { key: osVerdict.key });
  const version = $derived(adminVersion(status));

  const kept = $derived(manager?.retention?.previous || {});
  const verify = $derived(manager?.verify || {});
  const verdict = $derived(manager?.verdict || {});
  const lastInstall = $derived(
    rights.inFlight
      ? { key: "manager.last.waiting", tone: "c-warn" }
      : rights.expiredUnjudged
        ? { key: "manager.last.expired", tone: "c-fault" }
        : rights.unreadable
          ? { key: "manager.last.unreadable", tone: "c-warn" }
          : verdict.settled
            ? MANAGER_VERDICTS[verdict.verdict]
              ? { key: `manager.verdict.${verdict.verdict}.short`, tone: MANAGER_VERDICTS[verdict.verdict] }
              : { text: String(verdict.verdict || "—"), lang: "en", tone: "c-warn" }
            : { key: "manager.last.nothing", tone: "c-ok" },
  );

  const offered = $derived(managerOptions(sources?.releases || []));
  const offeredGroups = $derived(groupByTrack(offered.map((item) => ({ ...item, track: item.entry.track }))));
  const chosen = $derived(
    offered.find((item) => item.id === forms.managerVersion)
      || offered.find((item) => item.id === managerLatest?.release_id)
      || offeredGroups[0]?.items[0]
      || null,
  );
  function optionLabel(item) {
    if (!item.version) return item.id;
    const parts = [item.version];
    if (item.stamp) parts.push(item.stamp);
    if (item.direction) parts.push(t(`manager.direction.${item.direction}`));
    if (item.ambiguous) parts.push(item.id);
    return parts.join(" · ");
  }

  const yesNo = (value) => (value === true ? t("common.yes") : value === false ? t("common.no") : null);
</script>

<section class="span-12 summary" data-testid="update-summary" aria-label={t("updates.summary")}>
  <Card icon="package" tone="c-out" title={t("updates.manager.title")} subtitle={t("updates.manager.sub")}>
    <div class="summary-body" data-testid="update-summary-manager">
      <span data-testid="update-summary-manager-state"><Status tone={managerSummary.tone} text={t(managerSummary.key)} /></span>
      <KeyValues rows={[{ label: t("updates.installed"), value: manager?.installed_version }, { label: t("updates.latestStable"), value: managerLatestVersion }]} />
      {#if managerLatest?.direction === "upgrade"}
        <PlanButton plan={managerInstallPlan(managerLatest, false, "update-manager-latest")} label={t("updates.updateTo", { version: managerLatestVersion || "" })} icon="download" disabled={!managerKnown || !rights.canUpdate || unconfirmed} testid="update-manager-latest" />
        <UnconfirmedNote testid="update-manager-latest-unconfirmed" />
      {/if}
    </div>
  </Card>
  <Card icon="server" tone="c-inv" title={t("updates.admin.title")} subtitle={t("updates.admin.sub")}>
    <div class="summary-body" data-testid="update-summary-admin">
      <span data-testid="update-summary-admin-state"><Status tone={adminSummary.tone} text={t(adminSummary.key)} /></span>
      <KeyValues rows={[{ label: t("updates.installed"), value: version.key ? t(version.key) : version.text }, { label: t("updates.latestStable"), value: adminLatest?.tag }]} />
      {#if gate === "bootstrap"}
        <a class="btn small" href={href("admin")} data-testid="update-admin-install"><Icon name="download" />{t("install.installAdmin")}</a>
      {:else if gate === "missing"}
        <a class="btn small" href={href("admin")} data-testid="update-admin-repair">{t("updates.openAdmin")}</a>
      {:else if adminLatest?.direction === "upgrade" && ["ready", "live"].includes(gate)}
        <PlanButton plan={adminInstallPlan("exact", adminLatest.tag, false, "update-admin-latest")} label={t("updates.updateTo", { version: adminLatest.tag })} icon="download" disabled={gate === "live"} testid="update-admin-latest" />
      {/if}
    </div>
  </Card>
  <Card icon="hard-drive" tone="c-pv" title={t("updates.os.title")} subtitle={t("updates.os.sub")}>
    <div class="summary-body" data-testid="update-summary-os">
      <span data-testid="update-summary-os-state"><Status tone={osVerdict.tone} text={say(osWords)} lang={langOf(osWords)} /></span>
      <KeyValues
        rows={[
          { label: t("overview.securityUpdates"), value: tiles.securityCount },
          { label: t("overview.otherUpdates"), value: tiles.normalCount },
          { label: t("updates.os.restart"), value: yesNo(updates.reboot_required) },
        ]}
      />
      {#if securityUpdatesOffered(updates)}
        <PlanButton plan={securityUpdatesPlan("update-os-security")} label={t("overview.installSecurity")} icon="shield-check" testid="update-os-security" />
      {/if}
    </div>
  </Card>
</section>

<Card icon="package" tone="c-out" title={t("manager.title")} subtitle={t("manager.sub")} span="span-12">
  <div data-testid="manager-section">
    {#if manager === undefined || manager === null}
      <p class="muted"><Icon name="loader" size="sm" /> {t("manager.reading")}</p>
    {:else if manager.error}
      <div class="empty error" data-testid="manager-unavailable"><Icon name="triangle-alert" /><span>{t("manager.unavailable", { code: manager.error })}</span></div>
    {:else}
      <div class="tiles">
        <div class="tile" data-testid="manager-installed">
          <div class="tile-label">{t("manager.installed")}</div>
          <div class="tile-value num">{manager.installed_version || "—"}</div>
          <div class="tile-sub muted">{manager.configured ? t("manager.indexConfigured") : t("manager.indexMissing")}</div>
        </div>
        <div class="tile" data-testid="manager-kept">
          <div class="tile-label">{t("manager.kept")}</div>
          <div class="tile-value"><Status tone={manager.can_revert ? "c-ok" : "c-warn"} text={manager.can_revert ? kept.version || "—" : t("manager.noneKept")} /></div>
          <div class="tile-sub muted">{kept.build_id ? t("manager.build", { build: kept.build_id }) : ""}</div>
          {#if detail.expert && kept.sha256}<div class="tile-sub mono">{kept.sha256}</div>{/if}
        </div>
        <div class="tile" data-testid="manager-verify">
          <div class="tile-label">{t("manager.lastInstall")}</div>
          <div class="tile-value"><Status tone={lastInstall.tone} text={say(lastInstall)} lang={langOf(lastInstall)} /></div>
          <div class="tile-sub muted">
            {#if manager.outcome?.outcome}{#if MANAGER_OUTCOMES.includes(manager.outcome.outcome)}{t(`manager.outcome.${manager.outcome.outcome}`)}{:else}<span lang="en">{manager.outcome.outcome}</span>{/if}{/if}
            {#if verify.armed}{" "}· {t("manager.expecting", { version: verify.expected_version || "—" })}{/if}
          </div>
        </div>
      </div>

      {#if rights.inFlight}
        <p class="callout c-warn" data-testid="manager-deadline"><Icon name="clock" /><span><strong>{t("manager.deadline.title")}</strong> {t("manager.deadline.text", { version: verify.expected_version || "—" })}</span></p>
      {:else if rights.unreadable}
        <p class="callout c-warn" data-testid="manager-deadline-unreadable"><Icon name="triangle-alert" /><span><strong>{t("manager.unreadable.title")}</strong> {t("manager.unreadable.text")} <span class="mono" lang="en">({verify.unreadable})</span></span></p>
      {:else if rights.expiredUnjudged}
        <p class="callout c-fault" data-testid="manager-deadline-expired"><Icon name="triangle-alert" /><span><strong>{t("manager.expired.title")}</strong> {t("manager.expired.text", { version: verify.expected_version || "—" })}</span></p>
      {:else if verdict.settled && verdict.verdict !== "confirmed"}
        <p class="callout {MANAGER_VERDICTS[verdict.verdict] || 'c-warn'}" data-testid="manager-verdict"><Icon name="info" /><span>{MANAGER_VERDICTS[verdict.verdict] ? t(`manager.verdict.${verdict.verdict}`) : t("manager.verdict.other", { verdict: String(verdict.verdict || "—") })}</span></p>
      {/if}

      <div class="split">
        <div class="pane" data-testid="manager-stage-update">
          <h3 class="pane-title"><span class="icon-tile sm c-out"><Icon name="download" /></span>{t("manager.choose")}</h3>
          <p class="muted pane-sub">{t("manager.chooseSub")}</p>
          {#if sources === undefined || sources === null}
            <p class="muted"><Icon name="loader" size="sm" /> {t("manager.readingIndex")}</p>
          {:else if sources.error}
            <p class="note warn" data-testid="manager-sources-error"><Icon name="triangle-alert" />{t("manager.indexError", { code: sources.error })}</p>
          {:else if !sources.configured}
            <p class="note" data-testid="manager-sources-unconfigured"><Icon name="info" />{t("manager.indexUnconfigured")}</p>
          {:else if !offered.length}
            <p class="note" data-testid="manager-sources-empty"><Icon name="info" />{t("manager.indexEmpty")}</p>
          {:else}
            <label class="field">
              <span>{t("install.version")}</span>
              <select class="input" value={chosen?.id || ""} onchange={(event) => (forms.managerVersion = event.currentTarget.value)} data-testid="manager-version">
                {#each offeredGroups as group (group.track)}
                  <optgroup label={t(`track.${group.track}`)}>
                    {#each group.items as item, index (index)}<option value={item.id} title={item.id}>{optionLabel(item)}</option>{/each}
                  </optgroup>
                {/each}
              </select>
            </label>
            <div class="row-wrap">
              <PlanButton plan={managerInstallPlan(chosen.entry, riskyDirection(chosen.entry))} label={t("install.installSelected")} icon="download" disabled={!chosen || !rights.canUpdate || unconfirmed} testid="manager-plan-update" />
            </div>
            <UnconfirmedNote testid="manager-update-unconfirmed" />
            <TrackHelp />
            <p class="note"><Icon name="shield-check" />{t("manager.signed")}</p>
          {/if}
        </div>
        <div class="pane" data-testid="manager-stage-revert">
          <h3 class="pane-title"><span class="icon-tile sm c-soc"><Icon name="history" /></span>{t("manager.revert")}</h3>
          <p class="muted pane-sub">{t("manager.revertSub")}</p>
          <div class="row-wrap">
            <PlanButton plan={managerRevertPlan(kept.version || "—")} label={t("manager.reinstall", { version: manager.can_revert ? kept.version || "—" : "—" })} icon="history" variant="secondary" disabled={!rights.canRevert || unconfirmed} testid="manager-plan-revert" />
          </div>
          <UnconfirmedNote testid="manager-revert-unconfirmed" />
          <p class="note"><Icon name="info" />{t("manager.revertNote")}</p>
        </div>
      </div>
    {/if}
  </div>
</Card>

<Card icon="server" tone="c-inv" title={t("adminVersions.title")} subtitle={t("adminVersions.sub")} span="span-12">
  <div data-testid="admin-versions">
    {#if gate === "unread"}
      <div class="empty" data-testid="admin-versions-unavailable"><Icon name="info" /><span><strong>{t("adminVersions.unread")}</strong> {t("adminVersions.unreadText")}</span></div>
    {:else if gate === "bootstrap"}
      <div class="empty" data-testid="admin-versions-bootstrap"><Icon name="info" /><span><strong>{t("adminVersions.bootstrap")}</strong> {t("adminVersions.bootstrapText")}</span><a class="btn small" href={href("admin")}>{t("updates.openAdmin")}</a></div>
    {:else if gate === "docker"}
      <div class="empty error" data-testid="admin-versions-docker"><Icon name="triangle-alert" /><span><strong>{docker === "unavailable" ? t("docker.notInstalled") : t("docker.notRunning")}.</strong> {docker === "unavailable" ? t("adminVersions.noDocker") : t("adminVersions.dockerDown")}</span></div>
    {:else}
      {#if gate === "missing"}
        <p class="callout c-fault" data-testid="admin-versions-container-missing"><Icon name="triangle-alert" /><span><strong>{t("adminVersions.missing")}</strong> {t("adminVersions.missingText")}</span></p>
      {/if}
      {#if gate === "live"}
        <p class="callout c-warn" data-testid="admin-versions-transition-live"><Icon name="loader" /><span><strong>{t("admin.transition.title")}</strong> {t("admin.transition.text", { stage: status?.admin?.transition?.stage || t("admin.transition.unnamed") })}</span></p>
      {/if}
      <div class="split">
        <div class="pane" data-testid="admin-install-version">
          <h3 class="pane-title"><span class="icon-tile sm c-out"><Icon name="download" /></span>{t("adminVersions.choose")}</h3>
          <p class="muted pane-sub">{t("adminVersions.chooseSub")}</p>
          <InstallForm {releases} blocked={gate === "live"} />
        </div>
        <div class="pane" data-testid="admin-versions-recovery">
          <h3 class="pane-title"><span class="icon-tile sm c-pv"><Icon name="wrench" /></span>{t("adminVersions.recovery")}</h3>
          <p class="muted pane-sub">{t("adminVersions.recoverySub")}</p>
          <div class="row-wrap"><a class="btn secondary small" href={href("admin")} data-testid="updates-open-admin">{t("updates.openAdmin")}</a></div>
        </div>
      </div>
    {/if}
  </div>
</Card>

<Card icon="hard-drive" tone="c-pv" title={t("os.title")} subtitle={t("os.sub")} span="span-12">
  <div data-testid="os-section">
    <div class="tiles">
      <div class="tile" data-testid="updates-security">
        <div class="tile-label">{t("overview.securityUpdates")}</div>
        <div class="tile-value num">{tiles.securityCount ?? "—"}</div>
        <Status tone={tiles.security.tone} text={t(tiles.security.key)} />
      </div>
      <div class="tile" data-testid="updates-normal">
        <div class="tile-label">{t("overview.otherUpdates")}</div>
        <div class="tile-value num">{tiles.normalCount ?? "—"}</div>
        <KeyValues
          rows={[
            { label: t("os.kernel"), value: yesNo(tiles.kernel) },
            { label: t("os.firmware"), value: yesNo(tiles.firmware) },
            { label: t("os.held"), value: (updates.held || []).length },
          ]}
        />
      </div>
      <div class="tile" data-testid="updates-reboot">
        <div class="tile-label">{t("os.reboot")}</div>
        <div class="tile-value"><Status tone={tiles.reboot.tone} text={t(tiles.reboot.key)} /></div>
        {#if detail.expert && (updates.reboot_packages || []).length}<div class="tile-sub mono">{t("os.triggeredBy", { packages: updates.reboot_packages.join(", ") })}</div>{/if}
      </div>
      <div class="tile" data-testid="updates-manager">
        <div class="tile-label">{t("os.packageManager")}</div>
        <div class="tile-value"><Status tone={tiles.manager.tone} text={t(tiles.manager.key)} /></div>
        <KeyValues rows={[{ label: t("os.lock"), value: updates.package_manager?.lock_state, lang: "en" }, detail.expert && { label: t("os.dpkgIssues"), value: (updates.package_manager?.dpkg_issues || []).length }]} />
      </div>
    </div>

    <div class="row-wrap actions">
      <PlanButton plan={securityUpdatesPlan("updates-install-security")} label={t("os.planSecurity")} icon="shield-check" small={false} disabled={!securityUpdatesOffered(updates)} testid="updates-install-security" />
      {#if detail.expert}
        <PlanButton plan={allUpdatesPlan()} label={t("os.planAll")} icon="package" variant="secondary" small={false} testid="updates-install-all" />
      {/if}
    </div>

    {#if detail.expert || updatesInDoubt(updates)}
      <div class="repair" data-testid="updates-stage-repair">
        <h3 class="pane-title"><span class="icon-tile sm c-pv"><Icon name="wrench" /></span>{t("os.repair")}</h3>
        <p class="muted pane-sub">{t("os.repairSub")}</p>
        <div class="row-wrap">
          {#each REPAIR_ACTIONS as action (action)}
            <PlanButton plan={repairPlan(action)} label={t(`repair.${action}`)} variant="secondary" testid="updates-repair-{action}" />
          {/each}
        </div>
      </div>
    {/if}
    {#if detail.expert}
      {#if (updates.security_updates || []).length}
        <h3 class="pane-title table-title">{t("os.pendingSecurity")}</h3>
        <PackageTable packages={updates.security_updates} limit={100} />
      {/if}
    {/if}

    <p class="note" data-testid="updates-recovery"><Icon name="info" />{t("os.recovery")}</p>
    <p class="note"><Icon name="info" />{t("os.major")}</p>
  </div>
</Card>

<style>
  .summary { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--s5); }
  .summary-body { display: flex; flex-direction: column; gap: var(--s3); align-items: flex-start; }
  .summary-body :global(.kv) { align-self: stretch; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: var(--s3); }
  .tile { background: var(--m-surface-2); border-radius: var(--m-radius-inner); padding: var(--s3) var(--s4); display: flex; flex-direction: column; gap: 6px; min-width: 0; }
  .tile-label { font-size: 12px; color: var(--m-muted); }
  .tile-value { font-size: 18px; font-weight: 650; }
  .tile-sub { font-size: 12px; overflow-wrap: anywhere; }
  .split { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--s4); margin-top: var(--s4); }
  .pane { display: flex; flex-direction: column; gap: var(--s3); padding: var(--s4); border-radius: var(--m-radius-inner); border: 1px solid var(--m-line); min-width: 0; }
  .pane-title { margin: 0; font-size: 14px; font-weight: 620; display: flex; align-items: center; gap: var(--s2); }
  .pane-sub { margin: -6px 0 0; font-size: 12.5px; }
  .actions { margin-top: var(--s4); }
  .repair { margin-top: var(--s4); display: flex; flex-direction: column; gap: var(--s3); padding: var(--s4); border-radius: var(--m-radius-inner); border: 1px solid var(--m-line); }
  .table-title { margin-top: var(--s4); margin-bottom: var(--s2); }
  @container content (max-width: 1000px) {
    .summary, .split { grid-template-columns: minmax(0, 1fr); }
  }
</style>
