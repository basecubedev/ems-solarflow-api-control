<script>
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import KeyValues from "../widgets/KeyValues.svelte";
  import PlanButton from "../widgets/PlanButton.svelte";
  import InstallForm from "../widgets/InstallForm.svelte";
  import LogPanel from "../widgets/LogPanel.svelte";
  import { t, say, langOf } from "../i18n/i18n.svelte.js";
  import { href } from "../app/router.svelte.js";
  import { host, ensure, held } from "../state/host.svelte.js";
  import { detail } from "../state/prefs.svelte.js";
  import { adminHealth, adminTransitionLive, adminVersion } from "../lib/host.js";
  import { LIFECYCLE_ACTIONS, adminRepairPlan, adminRollbackPlan, lifecyclePlan } from "../lib/plans.js";
  import { CONTAINER_STATES, word } from "../lib/words.js";

  /**
   * Restart, repair and roll back the EMS Admin container; the version itself
   * is chosen on Updates. A freshly flashed appliance has nothing deployed, so
   * the page then offers the one thing that applies -- installing it.
   */
  const status = $derived(host.status);
  const admin = $derived(status?.admin || {});
  const bootstrap = $derived(admin.bootstrap_required === true);
  const live = $derived(adminTransitionLive(status));
  const health = $derived(adminHealth(admin));
  const version = $derived(adminVersion(status));
  const knownGood = $derived(admin.known_good || {});
  const LIFECYCLE_ICONS = { start: "play", stop: "square", restart: "rotate-cw" };

  $effect(() => {
    if (bootstrap && host.releases === undefined) ensure("releases");
  });
</script>

{#if live}
  <p class="callout c-warn span-12" data-testid="admin-transition-live"><Icon name="loader" /><span><strong>{t("admin.transition.title")}</strong> {t("admin.transition.text", { stage: admin.transition?.stage || t("admin.transition.unnamed") })}</span></p>
{/if}

{#if bootstrap}
  <Card title={t("admin.bootstrap.title")} subtitle={t("admin.bootstrap.sub")} span="span-12">
    {#snippet actions()}<Status tone="c-warn" text={t("admin.bootstrap.state")} />{/snippet}
    <div data-testid="admin-bootstrap-install">
      <p class="lead">{t("admin.bootstrap.lead")}</p>
      <InstallForm releases={held("releases")} bootstrap />
    </div>
  </Card>
{:else if status !== undefined}
  <Card title={t("admin.installed")} subtitle={t("admin.installedSub")} span="span-4">
    {#snippet actions()}<Status tone={health.tone} text={t(health.key)} />{/snippet}
    <div data-testid="admin-version">
      <p class="big num">{version.key ? t(version.key) : version.text}</p>
      <KeyValues
        rows={[
          { label: t("admin.containerState"), value: say(word("container", CONTAINER_STATES, admin.container?.state)), lang: langOf(word("container", CONTAINER_STATES, admin.container?.state)) },
          { label: t("admin.healthCheck"), value: admin.container?.health, lang: "en" },
        ]}
      />
    </div>
  </Card>

  <Card title={t("admin.knownGood")} subtitle={t("admin.knownGoodSub")} span="span-4">
    <div data-testid="admin-known-good">
      <KeyValues
        rows={[
          { label: t("admin.current"), value: knownGood.current?.admin_version },
          { label: t("admin.previous"), value: knownGood.previous?.admin_version },
          detail.expert && { label: t("admin.previousDigest"), value: knownGood.previous?.admin_digest, mono: true },
        ]}
      />
    </div>
  </Card>

  {#if detail.expert}
    <Card title={t("admin.image")} span="span-4">
      <div data-testid="admin-image">
        <KeyValues
          rows={[
            { label: t("admin.repository"), value: (admin.image?.reference || "").split(":")[0], mono: true },
            { label: t("overview.digest"), value: admin.digest, mono: true },
            { label: t("admin.revision"), value: admin.revision, mono: true },
            { label: t("admin.architecture"), value: admin.image?.architecture },
            { label: t("admin.containerId"), value: admin.container?.container_id, mono: true },
          ]}
        />
      </div>
    </Card>
  {:else}
    <Card title={t("admin.version")} subtitle={t("admin.versionSub")} span="span-4">
      <div class="stack-sm" data-testid="admin-install">
        <p class="muted small">{t("admin.versionText")}</p>
        <a class="btn small" href={href("updates")} data-testid="admin-open-updates"><Icon name="circle-arrow-up" />{t("overview.openUpdates")}</a>
      </div>
    </Card>
  {/if}

  <Card title={t("admin.actions")} subtitle={t("admin.actionsSub")} span="span-12">
    <div class="actions-grid">
      <div class="pane" data-testid="admin-lifecycle">
        <h3 class="pane-title">{t("admin.lifecycle")}</h3>
        <p class="muted pane-sub">{t("admin.lifecycleSub")}</p>
        <div class="row-wrap">
          {#each LIFECYCLE_ACTIONS as action (action)}
            <PlanButton plan={lifecyclePlan(action)} label={t(`admin.${action}`)} icon={LIFECYCLE_ICONS[action]} variant={action === "restart" ? "" : "secondary"} testid="admin-{action}" />
          {/each}
        </div>
      </div>
      <div class="pane" data-testid="admin-repair-stage">
        <h3 class="pane-title">{t("admin.repair")}</h3>
        <p class="muted pane-sub">{t("admin.repairSub")}</p>
        <div class="row-wrap"><PlanButton plan={adminRepairPlan()} label={t("admin.previewRepair")} icon="wrench" testid="admin-repair" /></div>
      </div>
      <div class="pane" data-testid="admin-rollback-stage">
        <h3 class="pane-title">{t("admin.rollback")}</h3>
        <p class="muted pane-sub">{t("admin.rollbackSub")}</p>
        <div class="row-wrap"><PlanButton plan={adminRollbackPlan()} label={t("admin.rollBack")} icon="history" variant="secondary" disabled={!knownGood.previous} testid="admin-rollback" /></div>
        <p class="note"><Icon name="info" />{knownGood.previous ? t("admin.rollbackDigest", { version: knownGood.previous.admin_version || "—" }) : t("admin.rollbackNone")}</p>
      </div>
    </div>
    {#if detail.expert}
      <p class="note"><Icon name="info" />{t("admin.versionText")} <a href={href("updates")} data-testid="admin-open-updates">{t("overview.openUpdates")}</a></p>
    {/if}
  </Card>
{:else}
  <div class="span-12 muted"><Icon name="loader" size="sm" /> {t("overview.reading")}</div>
{/if}

<LogPanel title={t("admin.log")} source="admin_container" />

<style>
  .lead { margin: 0 0 var(--s4); color: var(--m-muted); max-width: 70ch; }
  .big { font-size: 22px; font-weight: 650; margin: 0 0 var(--s3); }
  .small { font-size: 13px; margin: 0; }
  .stack-sm { display: flex; flex-direction: column; gap: var(--s3); align-items: flex-start; }
  .actions-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--s4); }
  .pane { display: flex; flex-direction: column; gap: var(--s3); padding: var(--s4); border-radius: var(--m-radius-inner); border: 1px solid var(--m-line); min-width: 0; }
  .pane .note { margin: 0; }
  .pane-title { margin: 0; font-size: 14px; font-weight: 620; }
  .pane-sub { margin: -6px 0 0; font-size: 12.5px; }
  @container content (max-width: 1000px) {
    .actions-grid { grid-template-columns: minmax(0, 1fr); }
  }
</style>
