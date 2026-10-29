<script>
  import { untrack } from "svelte";
  import Card from "../../../shared/ui/Card.svelte";
  import Icon from "../../../shared/ui/Icon.svelte";
  import Empty from "../../../shared/ui/Empty.svelte";
  import Status from "../../../shared/ui/Status.svelte";
  import Segmented from "../../../shared/ui/Segmented.svelte";
  import { t, tn, fmt } from "../i18n/i18n.svelte.js";
  import { system, ensure, reload, keepWithOverview } from "../state/system.svelte.js";
  import { upgrade, chooseRelease, resumeUpgrade, noteStatus } from "../state/upgrade.svelte.js";
  import { CHANNEL_FILTERS, CHANNEL_FILTER_KEY, CHANNEL_TONES, channelChoices, groupedReleases, noteView, pairView, releaseList, releasesInChannel, shownChannel } from "../lib/updates.js";
  import { readStored, writeStored } from "../state/prefs.svelte.js";
  import { STAGE_TONES, activeUpgrade, lastRun } from "../lib/upgrade.js";
  import { activeWorkflow } from "../lib/workflow.js";
  import { emsFact } from "../lib/health.js";
  import { errorView } from "../lib/errors.js";
  import StaleNote from "../widgets/StaleNote.svelte";
  import UpgradePanel from "../widgets/UpgradePanel.svelte";
  import AlignmentCard from "../widgets/AlignmentCard.svelte";
  import Spinner from "../widgets/Spinner.svelte";

  ensure("overview", "alignment", "releases", "lifecycle");
  keepWithOverview("alignment");

  const STEPS = ["backup", "config", "admin", "deploy", "check"];
  const STEP_LOOKS = {
    backup: { icon: "archive", tone: "c-soc" },
    config: { icon: "file-check", tone: "c-pv" },
    admin: { icon: "server", tone: "c-inv" },
    deploy: { icon: "package-plus", tone: "c-out" },
    check: { icon: "circle-check", tone: "c-bat" },
  };

  const FILTER_LABELS = { all: "updates.filter.all", other: "updates.channel.unknown" };

  const f = $derived(fmt());
  const overview = $derived(system.overview.data);
  const ems = $derived(emsFact(overview));
  const pair = $derived(system.alignment.data?.running_pair || null);
  const pairState = $derived(pairView(pair));
  const releases = $derived(releaseList(system.releases.data));
  const recommended = $derived(releases.find((release) => release.recommended) || null);
  const runsStable = $derived(releases.some((release) => release.running && release.channel === "stable"));
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
  let channel = $state(readStored(CHANNEL_FILTER_KEY, CHANNEL_FILTERS, "all"));
  const installable = $derived(releases.filter((release) => release.upgrade));
  const listed = $derived(showAll ? releases : installable);
  const choices = $derived(channelChoices(releases, listed));
  const filter = $derived(shownChannel(choices, channel));
  const shown = $derived(releasesInChannel(listed, filter, upgrade.stage !== "idle" ? upgrade.target : null));
  const groups = $derived(groupedReleases(shown));
  const choosable = $derived(shown.filter((release) => release.upgrade));
  const channelOptions = $derived(
    choices.map((choice) => ({ value: choice.id, tone: choice.tone || undefined, dot: Boolean(choice.tone), label: `${t(FILTER_LABELS[choice.id] || `updates.channel.${choice.id}`)} (${choice.count})` })),
  );

  function pickChannel(value) {
    channel = value;
    writeStored(CHANNEL_FILTER_KEY, value);
  }

  function choose(release) {
    if (held) return;
    chooseRelease(release);
  }
</script>

{#snippet about(release)}
  <span class="words">
    <span class="tag">
      <span class="num">{release.tag}</span>
      <span class="badge channel {CHANNEL_TONES[release.channel]}">{t(`updates.channel.${release.channel}`)}</span>
      {#if release.recommended}<span class="badge">{t("updates.recommended")}</span>{/if}
      {#if release.running}<span class="badge">{t("updates.running")}</span>{/if}
    </span>
    <span class="muted small">
      {#if release.publishedAt}<span class="num">{f.dateTime(release.publishedAt)}</span>{/if}{#if release.publishedAt && release.note}{" "}<span aria-hidden="true">·</span>{" "}{/if}{#if release.note?.key}{t(release.note.key)}{:else if release.note}<span lang="en">{release.note.text}</span>{/if}
    </span>
  </span>
  {#if release.state}<Status tone={release.tone} text={t(`updates.state.${release.state}`)} />{/if}
{/snippet}

<Card icon="server" tone="c-inv" title={t("updates.current")} subtitle={t("updates.currentSub")} span="span-12">
  <div class="facts two">
    <div class="fact"><div class="fact-label">{t("version.admin")}</div><div class="fact-value num" class:na={!overview?.components?.admin?.tag && !pair?.admin?.release}>{overview?.components?.admin?.tag || pair?.admin?.release || t("version.unknown")}</div></div>
    <div class="fact"><div class="fact-label">{t("version.ems")}</div><div class="fact-value num" class:na={!ems.version}>{ems.version || t("version.unknown")}</div></div>
  </div>
  {#if pairState}
    <p class="pair"><Status tone={pairState.tone} text={t(`updates.pair.${pairState.state}`)} /><span class="muted">{t("updates.pairHint")}</span></p>
  {/if}
  {#if last}
    <p class="last muted {last.outcome === "completed" ? "c-ok" : "c-fault"}" data-testid="last-upgrade"><Icon name={last.outcome === "completed" ? "circle-check" : "ban"} size="sm" /><span>{t(`updates.last.${last.outcome}`, { tag: last.tag })}{#if last.at}{" "}<span aria-hidden="true">·</span>{" "}<span class="num">{f.when(last.at)}</span>{/if}</span></p>
  {/if}
  {#if upgrade.stage !== "idle" && upgrade.target}
    <p class="now {STAGE_TONES[upgrade.stage]}" data-testid="upgrade-now">
      {#if upgrade.busy}<Spinner size="sm" />{:else}<span class="dot"></span>{/if}
      <span><span class="now-title">{t("upgrade.title", { tag: upgrade.target })}</span>{" "}<span aria-hidden="true">·</span>{" "}{t(`upgrade.sub.${upgrade.stage}`)}</span>
    </p>
  {/if}
</Card>

{#if upgrade.stage !== "idle"}<UpgradePanel />{/if}
<AlignmentCard />

<Card icon="route" tone="c-out" title={t("updates.guidedTitle")} subtitle={t("updates.guidedSub")} span="span-12">
  <ol class="how">
    {#each STEPS as step (step)}<li><span class="icon-tile sm {STEP_LOOKS[step].tone}"><Icon name={STEP_LOOKS[step].icon} /></span><span>{t(`updates.step.${step}`)}</span></li>{/each}
  </ol>
  {#if upgrade.stage === "idle"}
    <div class="row-wrap actions">
      {#if recommended?.selectable && !recommended.running}
        <button class="btn" type="button" onclick={() => choose(recommended)} disabled={held} data-testid="start-upgrade"><Icon name="circle-arrow-up" size="sm" />{t("upgrade.start", { tag: recommended.tag })}</button>
      {:else if !recommended && runsStable}
        <p class="note" data-testid="no-recommendation"><Icon name="circle-check" />{t("updates.noRecommendation")}</p>
      {:else}
        <p class="muted small">{t("updates.chooseBelow")}</p>
      {/if}
    </div>
  {/if}
</Card>

<Card icon="boxes" tone="c-out" title={t("updates.releases")} subtitle={recommended ? t("updates.recommendedIs", { tag: recommended.tag }) : t("updates.releasesSub")} span="span-12">
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
    {#if choices.length}
      <div class="filter" data-testid="release-filter"><Segmented options={channelOptions} value={filter} label={t("updates.filter.label")} onchange={pickChannel} /></div>
    {/if}
    {#if choosable.length}<p class="muted hint">{t("updates.chooseHint")}</p>{:else if !showAll && !installable.length}<p class="muted hint" data-testid="none-installable">{t("updates.noneInstallable")}</p>{:else if filter !== "all"}<p class="muted hint" data-testid="none-in-channel">{t("updates.noneInChannel")}</p>{/if}
    <div class="groups" data-testid="releases">
      {#each groups as group (group.id)}
        <section class="group {group.tone}" data-group={group.id} aria-labelledby={`release-group-${group.id}`}>
          <h3 class="group-title" id={`release-group-${group.id}`}><span class="dot"></span>{t(`updates.group.${group.id}`)}</h3>
          <ul class="releases">
            {#each group.releases as release (release.tag)}
              <li data-tag={release.tag}>
                {#if release.upgrade}
                  {@const chosen = upgrade.target === release.tag && upgrade.stage !== "idle"}
                  {@const checking = chosen && upgrade.stage === "verifying"}
                  <button class="release inner choice {checking ? STAGE_TONES.verifying : ''}" class:chosen class:checking class:recommended={release.recommended} type="button" onclick={() => choose(release)} disabled={held} data-testid="choose-release">
                    {@render about(release)}
                    {#if checking}
                      <span class="go"><Spinner size="sm" />{t("updates.checking")}</span>
                    {:else}
                      <span class="go">{chosen ? t("updates.chosen") : t("updates.choose")}<Icon name="chevron-right" size="sm" /></span>
                    {/if}
                  </button>
                {:else}
                  <div class="release inner off">{@render about(release)}</div>
                {/if}
              </li>
            {/each}
          </ul>
        </section>
      {/each}
    </div>
    {#if releases.length > installable.length}
      <button class="btn secondary small more-btn" type="button" onclick={() => (showAll = !showAll)} data-testid="show-all-releases">{showAll ? t("updates.onlyInstallable") : tn("updates.showAll", releases.length)}</button>
    {/if}
    {#each (system.releases.data?.warnings || []).map(noteView).filter(Boolean) as warning, index (index)}<p class="note warn"><Icon name="triangle-alert" />{#if warning.key}{t(warning.key)}{:else}<span lang="en">{warning.text}</span>{/if}</p>{/each}
  {/if}
</Card>

<style>
  .pair { display: flex; align-items: center; gap: var(--s2); flex-wrap: wrap; margin: var(--s3) 0 0; font-size: 12.5px; }
  .last { display: flex; align-items: center; gap: 6px; margin: var(--s3) 0 0; font-size: 12.5px; }
  .last :global(svg.i) { color: var(--c); }
  .now {
    display: flex; align-items: center; gap: 10px; margin: var(--s3) 0 0; padding: 10px 12px; font-size: 13px;
    border-radius: var(--m-radius-inner); background: color-mix(in srgb, var(--c) 9%, var(--m-surface-2));
    border: 1px solid color-mix(in srgb, var(--c) 32%, transparent);
  }
  .now :global(svg.i) { color: var(--c); }
  .now-title { font-weight: 620; }
  .how { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; font-size: 13.5px; }
  .how li { display: flex; gap: 10px; align-items: center; }
  @container content (min-width: 820px) {
    .how { flex-direction: row; gap: var(--s4); }
    .how li { flex: 1 1 0; min-width: 0; flex-direction: column; align-items: flex-start; }
  }
  .actions { margin-top: var(--s4); gap: var(--s2); align-items: center; }
  .releases { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--s2); }
  .release { display: flex; align-items: center; gap: var(--s3); flex-wrap: wrap; width: 100%; }
  .release.off { opacity: 0.6; }
  .choice { color: inherit; font: inherit; text-align: left; cursor: pointer; border: 1px solid var(--m-line); transition: border-color 0.15s; }
  .choice:hover:not(:disabled) { border-color: var(--m-line-strong); background: var(--m-surface-3); }
  .choice:disabled { cursor: default; }
  .choice.recommended { border-color: color-mix(in srgb, var(--m-ok) 45%, var(--m-line)); background: color-mix(in srgb, var(--m-ok) 6%, var(--m-surface-2)); }
  .choice.chosen { border-color: var(--m-accent); }
  .choice.checking { border-color: color-mix(in srgb, var(--c) 60%, transparent); background: color-mix(in srgb, var(--c) 8%, var(--m-surface-2)); }
  .go { display: inline-flex; align-items: center; gap: 4px; margin-left: auto; font-size: 13px; font-weight: 620; color: var(--m-accent); }
  .choice:disabled .go { color: var(--m-muted); }
  .choice.checking .go { color: var(--m-text); }
  .choice.checking .go :global(svg.i) { color: var(--c); }
  .hint { font-size: 13px; margin: 0 0 var(--s3); }
  .filter { margin: 0 0 var(--s3); }
  .release .words { flex: 1 1 280px; min-width: 0; display: flex; flex-direction: column; }
  .tag { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-weight: 620; }
  .badge.channel { display: inline-flex; align-items: center; gap: 5px; background: var(--m-surface-3); color: var(--m-muted); }
  .badge.channel::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: var(--c, var(--m-muted-2)); }
  .groups { display: flex; flex-direction: column; gap: var(--s4); }
  .group-title { display: flex; align-items: center; gap: 8px; margin: 0 0 var(--s2); font-size: 12.5px; font-weight: 640; color: var(--m-muted); }
  .small { display: block; font-size: 12.5px; margin-top: 2px; overflow-wrap: anywhere; }
  .more-btn { margin-top: var(--s3); }
</style>
