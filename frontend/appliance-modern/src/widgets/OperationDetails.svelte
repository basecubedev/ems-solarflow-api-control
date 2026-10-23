<script>
  import Icon from "../../../shared/ui/Icon.svelte";
  import KeyValues from "./KeyValues.svelte";
  import RepairChecks from "./RepairChecks.svelte";
  import { t, tn } from "../i18n/i18n.svelte.js";
  import { detail } from "../state/prefs.svelte.js";
  import { VERIFICATION_FAILURES, operationDetails } from "../lib/operations.js";

  /** What an operation's result says beyond "done": a preflight that changed nothing, a failed verification, what is left to do by hand. */
  let { operation } = $props();
  const info = $derived(operationDetails(operation));
  const reason = (code) => (VERIFICATION_FAILURES.includes(code) ? t(`verify.${code}`) : String(code));
  const reasonLang = (code) => (VERIFICATION_FAILURES.includes(code) ? undefined : "en");
</script>

{#if info.any}
  <div class="details" data-testid="operation-details">
    {#if info.untouched}<p class="line" data-testid="admin-untouched"><Icon name="shield-check" size="sm" />{t("op.detail.untouched")}</p>{/if}
    {#if info.bootstrapFailed}
      <p class="line warn" data-testid="admin-bootstrap-failed"><Icon name="triangle-alert" size="sm" />{info.deploymentCreated ? t("op.detail.bootstrapCreated") : t("op.detail.bootstrapNothing")}</p>
    {/if}
    {#if info.replan}<p class="line warn" data-testid="replan-required"><Icon name="triangle-alert" size="sm" />{t("op.detail.replan")}</p>{/if}
    {#if info.verification}
      <div data-testid="verification-failure">
        <p class="sub">{t("op.detail.verifyFailed")}</p>
        <ul class="reasons" data-testid="verification-reasons">
          {#each info.verification.failures || [] as code, index (index)}<li lang={reasonLang(code)}>{reason(code)}</li>{/each}
        </ul>
        <KeyValues
          rows={[
            { label: t("op.detail.container"), value: info.verification.container_state },
            { label: t("op.detail.endpoint"), value: info.verification.api_reachable ? t("op.detail.answered") : t("op.detail.noAnswer") },
            { label: t("op.detail.reportedVersion"), value: info.verification.version },
            detail.expert && { label: t("op.detail.runningDigest"), value: info.verification.active_digest, mono: true },
            detail.expert && { label: t("op.detail.expectedDigest"), value: info.verification.expected_digest, mono: true },
          ]}
        />
      </div>
    {/if}
    {#if info.unverified.length}
      <p class="sub">{t("op.detail.unverified")}</p>
      <ul class="reasons" data-testid="unverified-actions">
        {#each info.unverified as item, index (index)}<li><strong lang="en">{item.action}</strong>: <span lang={reasonLang(item.result)}>{reason(item.result)}</span></li>{/each}
      </ul>
    {/if}
    {#if info.manual.length}
      <p class="sub">{t("op.detail.manual")}</p>
      <ul class="reasons" data-testid="manual-actions">
        {#each info.manual as item, index (index)}<li lang="en">{item}</li>{/each}
      </ul>
    {/if}
    {#if info.remaining.length && detail.expert}
      <p class="sub">{t("op.detail.stillFailing")}</p>
      <RepairChecks findings={info.remaining} />
    {:else if info.remaining.length}
      <p class="line warn" data-testid="remaining-summary"><Icon name="triangle-alert" size="sm" />{tn("op.detail.remaining", info.remaining.length)}</p>
    {/if}
  </div>
{/if}

<style>
  .details { display: flex; flex-direction: column; gap: var(--s2); margin-top: var(--s3); }
  .line { display: flex; gap: 8px; align-items: flex-start; margin: 0; font-size: 13px; }
  .line :global(svg.i) { margin-top: 2px; color: var(--m-ok); }
  .line.warn :global(svg.i) { color: var(--m-warn); }
  .sub { margin: var(--s2) 0 0; font-size: 12.5px; color: var(--m-muted); font-weight: 560; }
  .reasons { margin: 0; padding-left: 20px; font-size: 13px; display: flex; flex-direction: column; gap: 4px; }
</style>
