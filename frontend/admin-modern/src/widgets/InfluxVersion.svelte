<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script>
  import { untrack } from "svelte";
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import Empty from "../../../shared/ui/Empty.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import ConfirmDialog from "../../../shared/ui/ConfirmDialog.svelte";
  import StaleNote from "./StaleNote.svelte";
  import StepList from "./StepList.svelte";
  import { t, fmt } from "../i18n/i18n.svelte.js";
  import { system, ensure, reload } from "../state/system.svelte.js";
  import { askInflux, askStopWaiting, cancelInflux, confirmInflux, influxJob, recheckInflux } from "../state/influxUpgrade.svelte.js";
  import { STEP_NOTES, influxCanRecheck, influxCardShown, influxOutcome, influxSettled, influxView, stepView, stateWords } from "../lib/influxdb.js";
  import { errorView } from "../lib/errors.js";

  const BUSY_POLL_MS = 15_000;

  ensure("influxdb");
  $effect(() => () => {
    cancelInflux();
    influxJob.recheck = null;
    influxJob.stopFailed = false;
  });

  const f = $derived(fmt());
  const view = $derived(influxView(system.influxdb.data));
  const steps = $derived(influxJob.steps.map((step) => stepView(influxJob.kind, step)));
  const stale = $derived(Boolean(system.influxdb.error));
  const shown = $derived(
    influxCardShown(view, { running: influxJob.running, finished: Boolean(influxJob.result || influxJob.error), unreadable: stale }),
  );
  const outcome = $derived(outcomeOf(influxJob));
  /** A confirmation to stop is withdrawn once the wait it was asked for is over, and a stop under way is done then. */
  $effect(() => {
    if (slow) return;
    untrack(() => {
      if (influxJob.confirming === "stop") cancelInflux();
      influxJob.stopping = false;
      influxJob.stopFailed = false;
    });
  });
  /** A run this page lost hold of is followed until the plan says it is over. */
  $effect(() => {
    if (influxJob.detached && view && view.state !== "busy") untrack(() => (influxJob.detached = false));
  });
  /** A run another tab or an earlier page started is followed by reading the plan again. */
  $effect(() => {
    if (view?.state !== "busy" || influxJob.running) return;
    const timer = setInterval(() => reload("influxdb"), BUSY_POLL_MS);
    return () => clearInterval(timer);
  });
  const recheckable = $derived(influxCanRecheck(view));
  const stopKind = $derived(influxJob.running ? influxJob.kind : view?.last?.state === "rolling_back" ? "rollback" : "upgrade");
  /** A start that takes long, in a run this browser follows or one the server says runs elsewhere. */
  const slow = $derived(
    (influxJob.running && steps.some((step) => step.state === "running" && step.detailKey === STEP_NOTES.still_starting)) || (view?.state === "busy" && view.slow),
  );

  function outcomeOf(job) {
    const ended = influxOutcome(job);
    if (!ended) return null;
    const text = t(`influx.outcome.${ended.kind}.${ended.done ? "done" : "failed"}`);
    if (job.result) return { tone: ended.done ? "ok" : "warn", text, detail: job.result.message || "" };
    return { tone: "warn", text, ...detailOf(errorView(job.error, t)) };
  }

  function detailOf(error) {
    return { detail: error.text, lang: error.lang };
  }

  function lastText(last) {
    const vars = { from: last.from, to: last.to, backup: last.backup, aside: last.aside, when: last.at ? f.dateTime(last.at) : "—" };
    const now = view?.state === "busy" && view.live && ["running", "rolling_back"].includes(last.state);
    return t(now ? `influx.last.${last.state}.now` : `influx.last.${last.state}`, vars);
  }

</script>

{#if shown}
  <Card icon="database" tone="c-inv" title={t("influx.title")} subtitle={t("influx.sub")} span="span-12">
    {#snippet actions()}
      <button class="icon-btn" type="button" onclick={() => reload("influxdb")} aria-label={t("common.refresh")} title={t("common.refresh")} disabled={system.influxdb.loading}><Icon name="refresh-cw" /></button>
    {/snippet}
    <StaleNote resource={system.influxdb} />
    {#if view}
      <div class="facts two">
        <div class="fact"><div class="fact-label">{t("influx.runs")}</div><div class="fact-value with-status" class:na={!view.current}><span class="num" data-testid="influx-current">{view.current || "—"}</span>{#if !influxJob.running && influxSettled(view)}<Status tone="c-ok" text={t("updates.upToDate")} />{/if}</div></div>
        <div class="fact"><div class="fact-label">{t("influx.ships")}</div><div class="fact-value num" class:na={!view.target} data-testid="influx-target">{view.target || "—"}</div></div>
      </div>
      <p class="state" data-testid="influx-state" data-state={view.state}>
        {t(`influx.state.${view.state}`, stateWords(view))}
        {#if view.message && ["unavailable", "unmanaged", "blocked", "mismatch", "no_way_back"].includes(view.state)}<span class="muted" lang="en">{" "}{view.message}</span>{/if}
      </p>
      {#if view.last && (view.state === "busy" ? view.live : !influxJob.running)}
        <p class="muted small" data-testid="influx-last">
          {lastText(view.last)}
          {#if view.last.message && ["failed", "rollback_failed"].includes(view.last.state)}<span lang="en">{" "}{view.last.message}</span>{/if}
        </p>
        {#if view.last.kept.length}
          <p class="muted small" data-testid="influx-kept">{t("influx.kept", { dirs: view.last.kept.map((name) => `data/${name}`).join(", ") })}</p>
        {/if}
      {/if}
      {#if recheckable || view.rollback?.available}
        <div class="row-wrap actions">
          {#if recheckable}
            <button class="btn" type="button" disabled={stale || influxJob.checking || influxJob.running} onclick={recheckInflux} data-testid="influx-recheck">
              <Icon name={influxJob.checking ? "loader" : "refresh-cw"} size="sm" />{t("influx.recheck")}
            </button>
          {/if}
          {#if view.rollback?.available}
          <button class="btn secondary" type="button" disabled={stale || influxJob.asking || influxJob.running} onclick={() => askInflux("rollback")} data-testid="influx-rollback">
            <Icon name={influxJob.running && influxJob.kind === "rollback" ? "loader" : "history"} size="sm" />{t("influx.rollback", { version: view.rollback.to })}
          </button>
          {/if}
        </div>
      {/if}
      {#if view.rollback && !view.rollback.available && view.rollback.reason}
        <p class="muted small">{t("influx.noRollback")}<span lang="en">{" "}{view.rollback.reason}</span></p>
      {/if}
    {:else if system.influxdb.error}
      <Empty error {...errorView(system.influxdb.error, t)} />
    {:else}
      <Empty text={t("common.loading")} icon="loader" />
    {/if}
    <div role="status" aria-live="polite">
      {#if influxJob.lost}<p class="note warn" data-testid="influx-lost"><Icon name="wifi-off" />{t("influx.lost")}</p>{/if}
      {#if influxJob.detached && view?.state === "busy"}<p class="note" data-testid="influx-detached"><Icon name="info" />{t("influx.detached")}</p>{/if}
      {#if influxJob.stopFailed}<p class="note warn" data-testid="influx-stop-failed"><Icon name="triangle-alert" />{t("influx.stopFailed")}</p>{/if}
      {#if influxJob.recheck}
        <p class="note {influxJob.recheck.ok ? 'ok' : 'warn'}" data-testid="influx-recheck-outcome"><Icon name={influxJob.recheck.ok ? "circle-check" : "triangle-alert"} /><span>{t(influxJob.recheck.ok ? "influx.recheck.done" : "influx.recheck.failed")}{#if influxJob.recheck.message && !influxJob.recheck.ok}<span lang="en">{" "}{influxJob.recheck.message}</span>{/if}</span></p>
      {/if}
      {#if outcome}
        <p class="note {outcome.tone}" data-testid="influx-outcome"><Icon name={outcome.tone === "ok" ? "circle-check" : "triangle-alert"} /><span>{outcome.text}{#if outcome.detail}<span lang={outcome.lang ?? "en"}>{" "}{outcome.detail}</span>{/if}</span></p>
      {/if}
    </div>
    {#if steps.length}<StepList {steps} label={t(`influx.progress.${influxJob.kind}`)} />{/if}
    {#if slow}
      <div class="row-wrap actions">
        <button class="btn secondary" type="button" onclick={askStopWaiting} disabled={influxJob.stopping} data-testid="influx-stop-waiting"><Icon name={influxJob.stopping ? "loader" : "circle-x"} size="sm" />{t(influxJob.stopping ? "influx.stopping" : "influx.stopWaiting")}</button>
      </div>
    {/if}
  </Card>
{/if}

<ConfirmDialog
  open={influxJob.confirming === "upgrade"}
  title={t("influx.confirmUpgradeTitle", { version: view?.target || "—" })}
  lines={[
    t("influx.confirmUpgrade.backup"),
    t("influx.confirmUpgrade.convert", { version: view?.target || "—" }),
    t("influx.longRun"),
    t("influx.confirmUpgrade.oneWay", { previous: view?.current || "—" }),
  ]}
  confirmLabel={t("influx.upgrade", { version: view?.target || "—" })}
  onconfirm={confirmInflux}
  oncancel={cancelInflux}
/>
<ConfirmDialog
  open={influxJob.confirming === "stop"}
  danger
  title={t("influx.confirmStopTitle")}
  lines={stopKind === "rollback" ? [t("influx.confirmStop.endsBack"), t("influx.confirmStop.putBack")] : [t("influx.confirmStop.ends"), t("influx.confirmStop.back")]}
  confirmLabel={t("influx.stopWaiting")}
  onconfirm={confirmInflux}
  oncancel={cancelInflux}
/>
<ConfirmDialog
  open={influxJob.confirming === "rollback"}
  danger
  title={t("influx.confirmRollbackTitle", { version: view?.rollback?.to || "—" })}
  lines={[
    t("influx.confirmRollback.setAside", { version: view?.rollback?.to || "—" }),
    t("influx.confirmRollback.restore", { backup: view?.rollback?.backup || "—", when: view?.rollback?.since ? f.dateTime(view.rollback.since) : "—" }),
  ]}
  confirmLabel={t("influx.rollback", { version: view?.rollback?.to || "—" })}
  onconfirm={confirmInflux}
  oncancel={cancelInflux}
/>

<style>
  .state { margin: var(--s3) 0 0; font-size: 13.5px; }
  .small { font-size: 12.5px; overflow-wrap: anywhere; }
  .actions { margin-top: var(--s4); gap: var(--s2); }
  .with-status { display: flex; align-items: center; gap: var(--s2); flex-wrap: wrap; }
</style>
