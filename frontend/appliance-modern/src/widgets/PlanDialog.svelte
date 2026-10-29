<script>
  import Dialog from "../../../shared/ui/Dialog.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import KeyValues from "./KeyValues.svelte";
  import PackageTable from "./PackageTable.svelte";
  import RepairChecks from "./RepairChecks.svelte";
  import { t, tn, say, langOf, fmt } from "../i18n/i18n.svelte.js";
  import { detail } from "../state/prefs.svelte.js";
  import { actions as flow, confirmPlan, dismissPlan } from "../state/actions.svelte.js";
  import { planBlocked, planFields, vocabularyWords } from "../lib/plan.js";
  import { OPERATION_TYPES } from "../lib/operations.js";
  import { exactDuration, gigabytes } from "../lib/host.js";

  /**
   * The plan the agent made, read before anything on this appliance changes.
   * Confirming sends the plan's own token, so only the plan shown here can run;
   * closing the dialog in any way cancels it.
   */
  const pending = $derived(flow.pending);
  const plan = $derived(pending?.payload?.plan || {});
  const options = $derived(pending?.options || {});
  const blocked = $derived(planBlocked(plan));

  function value(field) {
    if (field.kind === "bool") return field.value ? t("common.yes") : t("common.no");
    const vocabulary = vocabularyWords(field.key, field.value);
    if (vocabulary) return say(vocabulary);
    if (field.key === "type" && OPERATION_TYPES.includes(field.value)) return t(`op.${field.value}`);
    if (field.kind === "bytes") return fmt().bytes(field.value);
    if (field.kind === "percent") return fmt().percent(field.value);
    if (field.kind === "seconds") {
      const duration = exactDuration(field.value);
      return duration ? tn(`unit.${duration.unit}`, duration.count) : String(field.value);
    }
    if (field.kind === "megabytes") {
      const size = gigabytes(field.value);
      return size ? `${fmt().number(size.value, size.digits)} GB` : String(field.value);
    }
    return String(field.value);
  }

  const rows = $derived(
    planFields(plan, detail.expert).map((field) => ({
      label: field.known ? t(`plan.field.${field.key}`) : field.label,
      value: value(field),
      mono: field.mono,
      labelLang: field.known ? undefined : "en",
      lang: langOf(vocabularyWords(field.key, field.value)),
    })),
  );
  const created = $derived(plan.creates_deployment || null);

  /**
   * Esc while the confirmation is out must not lose the dialog: its answer,
   * an error included, belongs here. The dialog comes back once it arrives.
   */
  let hidden = $state(false);
  $effect(() => {
    if (!flow.confirming && hidden) hidden = false;
  });

  function closed() {
    if (flow.confirming) {
      hidden = true;
      return;
    }
    dismissPlan();
  }
</script>

<Dialog open={Boolean(pending) && !hidden} title={pending ? say(options.title) : ""} wide onclose={closed}>
  {#if pending}
    <div class="plan" data-testid="plan-dialog">
      <p class="lead">{blocked ? t("plan.blockedLead") : t("plan.lead")}</p>
      <KeyValues {rows} />

      {#if created}
        <div class="block" data-testid="plan-creates-deployment">
          <p class="sub">{t("plan.createsDeployment")}</p>
          <KeyValues
            rows={[
              { label: t("plan.composeFile"), value: created.compose_file, mono: true },
              { label: t("plan.environmentFile"), value: created.environment_file, mono: true },
              detail.expert && { label: t("plan.installer"), value: created.installer, mono: true },
              detail.expert && { label: t("plan.owner"), value: `${created.owner_uid}:${created.owner_gid}` },
            ]}
          />
        </div>
      {/if}

      {#if blocked}
        <ul class="blockers" data-testid="plan-blockers">
          {#each plan.blockers as blocker, index (index)}
            <li><Icon name="ban" size="sm" /><span><strong class="mono">{blocker.code}</strong> <span lang="en">{blocker.message}</span></span></li>
          {/each}
        </ul>
      {/if}

      {#if (plan.findings || []).length}<div class="block"><RepairChecks findings={plan.findings} /></div>{/if}
      {#if (plan.packages || []).length}<div class="block"><PackageTable packages={plan.packages} limit={12} /></div>{/if}

      {#if plan.key}
        <div class="block" data-testid="plan-key">
          <KeyValues
            rows={[
              { label: t("plan.keyType"), value: plan.key.key_type },
              { label: t("plan.keyComment"), value: plan.key.comment },
              { label: t("plan.keyFingerprint"), value: plan.key.fingerprint, mono: true },
            ]}
          />
        </div>
      {/if}

      {#if plan.target?.admin_version}
        <div class="block">
          <KeyValues
            rows={[
              { label: t("plan.targetVersion"), value: plan.target.admin_version },
              detail.expert && { label: t("plan.targetDigest"), value: plan.target.admin_digest, mono: true },
            ]}
          />
        </div>
      {/if}

      {#if plan.warning}<p class="warning" lang="en"><Icon name="triangle-alert" size="sm" />{plan.warning}</p>{/if}
      {#if (plan.preserves || []).length}<p class="note"><Icon name="shield-check" /><span>{t("plan.preserves")} <span lang="en">{plan.preserves.join(", ")}</span></span></p>{/if}
      {#if flow.dialogError}<p class="form-error" role="alert" lang={langOf(flow.dialogError)} data-testid="plan-error">{say(flow.dialogError)}</p>{/if}
    </div>
  {/if}
  {#snippet actions()}
    <button class="btn secondary" type="button" onclick={dismissPlan} disabled={flow.confirming} data-testid="plan-cancel">{t("auth.cancel")}</button>
    <button
      class="btn"
      class:danger={options.danger}
      type="button"
      onclick={confirmPlan}
      disabled={blocked || flow.confirming}
      aria-busy={flow.confirming ? "true" : undefined}
      data-testid="plan-confirm"
    >
      {#if flow.confirming}<Icon name="loader" />{/if}{flow.confirming ? t("plan.starting") : t(options.confirm || "plan.confirm")}
    </button>
  {/snippet}
</Dialog>

<style>
  .plan { display: flex; flex-direction: column; gap: var(--s3); }
  .lead { margin: 0; color: var(--m-muted); font-size: 13px; }
  .block { margin-top: var(--s1); }
  .sub { margin: 0 0 var(--s2); font-size: 12.5px; color: var(--m-muted); font-weight: 560; }
  .blockers { list-style: none; margin: 0; padding: var(--s3); display: flex; flex-direction: column; gap: 6px; border-radius: var(--m-radius-inner); background: color-mix(in srgb, var(--m-fault) 10%, var(--m-surface-1)); font-size: 13px; }
  .blockers li { display: flex; gap: 8px; align-items: flex-start; }
  .blockers :global(svg.i) { color: var(--m-fault); margin-top: 2px; }
  .warning { display: flex; gap: 8px; align-items: flex-start; margin: 0; font-size: 13px; }
  .warning :global(svg.i) { color: var(--m-warn); margin-top: 2px; }
</style>
