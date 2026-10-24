<script>
  import { untrack } from "svelte";
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import Empty from "../../../shared/ui/Empty.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import { t, tn, fmt } from "../i18n/i18n.svelte.js";
  import { system, ensure, reload, keepWithOverview } from "../state/system.svelte.js";
  import { upgrade, chooseRelease, resumeUpgrade, noteStatus } from "../state/upgrade.svelte.js";
  import { noteView, pairView, releaseList, RELEASES_SHOWN } from "../lib/updates.js";
  import { activeUpgrade, lastRun } from "../lib/upgrade.js";
  import { activeWorkflow } from "../lib/workflow.js";
  import { emsFact } from "../lib/health.js";
  import { errorView } from "../lib/errors.js";
  import StaleNote from "../widgets/StaleNote.svelte";
  import UpgradePanel from "../widgets/UpgradePanel.svelte";
  import AlignmentCard from "../widgets/AlignmentCard.svelte";

  ensure("overview", "alignment", "releases", "lifecycle");
  keepWithOverview("alignment");

  const STEPS = ["backup", "config", "admin", "deploy", "check"];

  const f = $derived(fmt());
  const overview = $derived(system.overview.data);
  const ems = $derived(emsFact(overview));
  const pair = $derived(system.alignment.data?.running_pair || null);
  const pairState = $derived(pairView(pair));
  const releases = $derived(releaseList(system.releases.data));
  const recommended = $derived(releases.find((release) => release.recommended) || null);
  const running = $derived(activeUpgrade(system.alignment.data?.transition) || activeWorkflow(system.lifecycle.data)?.owner === "guided_upgrade");
  const held = $derived(upgrade.busy || upgrade.holding || running);

  /** A run the server holds is shown here even when this page lost hold of it: closed, signed out, another tab's. */
  $effect(() => {
    if ((upgrade.stage === "idle" || upgrade.detached) && activeUpgrade(system.alignment.data?.transition)) untrack(resumeUpgrade);
  });
  $effect(() => {
    const transition = system.alignment.data?.transition;
    untrack(() => noteStatus(transition));
  });
  const last = $derived(lastRun(system.alignment.data?.transition));
  let showAll = $state(false);
  const shown = $derived(showAll ? releases : releases.slice(0, RELEASES_SHOWN));

  function choose(release) {
    if (held) return;
    globalThis.scrollTo?.(0, 0);
    chooseRelease(release);
  }
</script>

{#snippet about(release)}
  <span class="words">
    <span class="tag">
      <span class="num">{release.tag}</span>
      <span class="badge channel">{t(`updates.channel.${release.channel}`)}</span>
      {#if release.recommended}<span class="badge">{t("updates.recommended")}</span>{/if}
      {#if release.running}<span class="badge">{t("updates.running")}</span>{/if}
    </span>
    <span class="muted small">
      {#if release.publishedAt}<span class="num">{f.dateTime(release.publishedAt)}</span>{/if}{#if release.publishedAt && release.note}<span aria-hidden="true"> · </span>{/if}{#if release.note?.key}{t(release.note.key)}{:else if release.note}<span lang="en">{release.note.text}</span>{/if}
    </span>
  </span>
  {#if release.state}<Status tone={release.tone} text={t(`updates.state.${release.state}`)} />{/if}
{/snippet}

{#if upgrade.stage !== "idle"}<UpgradePanel />{/if}
<AlignmentCard />

<Card title={t("updates.current")} subtitle={t("updates.currentSub")} span="span-6">
  <div class="facts two">
    <div class="fact"><div class="fact-label">{t("version.admin")}</div><div class="fact-value num" class:na={!overview?.components?.admin?.tag && !pair?.admin?.release}>{overview?.components?.admin?.tag || pair?.admin?.release || t("version.unknown")}</div></div>
    <div class="fact"><div class="fact-label">{t("version.ems")}</div><div class="fact-value num" class:na={!ems.version}>{ems.version || t("version.unknown")}</div></div>
  </div>
  {#if pairState}
    <p class="pair"><Status tone={pairState.tone} text={t(`updates.pair.${pairState.state}`)} /><span class="muted">{t("updates.pairHint")}</span></p>
  {/if}
  {#if last}
    <p class="last muted" data-testid="last-upgrade"><Icon name={last.outcome === "completed" ? "circle-check" : "ban"} size="sm" /><span>{t(`updates.last.${last.outcome}`, { tag: last.tag })}{#if last.at}<span class="num"> · {f.when(last.at)}</span>{/if}</span></p>
  {/if}
</Card>

<Card title={t("updates.guidedTitle")} subtitle={t("updates.guidedSub")} span="span-6">
  <ol class="how">
    {#each STEPS as step, index (step)}<li><span class="n num">{index + 1}</span>{t(`updates.step.${step}`)}</li>{/each}
  </ol>
  <div class="row-wrap actions">
    {#if recommended?.selectable && !recommended.running}
      <button class="btn" type="button" onclick={() => choose(recommended)} disabled={held || upgrade.stage !== "idle"} data-testid="start-upgrade"><Icon name="circle-arrow-up" size="sm" />{t("upgrade.start", { tag: recommended.tag })}</button>
    {:else}
      <p class="muted small">{t("updates.chooseBelow")}</p>
    {/if}
  </div>
</Card>

<Card title={t("updates.releases")} subtitle={recommended ? t("updates.recommendedIs", { tag: recommended.tag }) : t("updates.releasesSub")} span="span-12">
  {#snippet actions()}
    <button class="icon-btn" type="button" onclick={() => reload("releases")} disabled={system.releases.loading} aria-label={t("common.refresh")} title={t("common.refresh")}><Icon name="refresh-cw" /></button>
  {/snippet}
  <StaleNote resource={system.releases} />
  {#if system.releases.loading && !system.releases.data}
    <Empty text={t("updates.loadingReleases")} icon="loader" />
  {:else if system.releases.error && !system.releases.data}
    <Empty error {...errorView(system.releases.error, t)} />
  {:else if !releases.length}
    <Empty text={t("updates.noReleases")} />
  {:else}
    {#if releases.some((release) => release.upgrade)}<p class="muted hint">{t("updates.chooseHint")}</p>{/if}
    <ul class="releases" data-testid="releases">
      {#each shown as release (release.tag)}
        <li data-tag={release.tag}>
          {#if release.upgrade}
            <button class="release inner choice" class:chosen={upgrade.target === release.tag && upgrade.stage !== "idle"} type="button" onclick={() => choose(release)} disabled={held} data-testid="choose-release">
              {@render about(release)}
              <span class="go">{upgrade.target === release.tag && upgrade.stage !== "idle" ? t("updates.chosen") : t("updates.choose")}<Icon name="chevron-right" size="sm" /></span>
            </button>
          {:else}
            <div class="release inner off">{@render about(release)}</div>
          {/if}
        </li>
      {/each}
    </ul>
    {#if releases.length > RELEASES_SHOWN}
      <button class="btn secondary small more-btn" type="button" onclick={() => (showAll = !showAll)}>{showAll ? t("updates.showFewer") : tn("updates.showAll", releases.length)}</button>
    {/if}
    {#each (system.releases.data?.warnings || []).map(noteView).filter(Boolean) as warning, index (index)}<p class="note warn"><Icon name="triangle-alert" />{#if warning.key}{t(warning.key)}{:else}<span lang="en">{warning.text}</span>{/if}</p>{/each}
  {/if}
</Card>

<style>
  .pair { display: flex; align-items: center; gap: var(--s2); flex-wrap: wrap; margin: var(--s3) 0 0; font-size: 12.5px; }
  .last { display: flex; align-items: center; gap: 6px; margin: var(--s3) 0 0; font-size: 12.5px; }
  .how { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; font-size: 13.5px; }
  .how li { display: flex; gap: 10px; align-items: flex-start; }
  .n { flex: none; width: 22px; height: 22px; border-radius: 50%; display: grid; place-items: center; font-size: 11.5px; font-weight: 650; background: var(--m-surface-3); color: var(--m-muted); }
  .actions { margin-top: var(--s4); gap: var(--s2); align-items: center; }
  .releases { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--s2); }
  .release { display: flex; align-items: center; gap: var(--s3); flex-wrap: wrap; width: 100%; }
  .release.off { opacity: 0.6; }
  .choice { color: inherit; font: inherit; text-align: left; cursor: pointer; border: 1px solid var(--m-line); transition: border-color 0.15s; }
  .choice:hover:not(:disabled) { border-color: var(--m-line-strong); background: var(--m-surface-3); }
  .choice:disabled { cursor: default; }
  .choice.chosen { border-color: var(--m-accent); }
  .go { display: inline-flex; align-items: center; gap: 4px; margin-left: auto; font-size: 13px; font-weight: 620; color: var(--m-accent); }
  .choice:disabled .go { color: var(--m-muted); }
  .hint { font-size: 13px; margin: 0 0 var(--s3); }
  .release .words { flex: 1 1 280px; min-width: 0; display: flex; flex-direction: column; }
  .tag { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-weight: 620; }
  .badge.channel { background: var(--m-surface-3); color: var(--m-muted); }
  .small { display: block; font-size: 12.5px; margin-top: 2px; overflow-wrap: anywhere; }
  .more-btn { margin-top: var(--s3); }
</style>
