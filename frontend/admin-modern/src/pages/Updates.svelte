<!-- SPDX-FileCopyrightText: 2026 basecubedev -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
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
  import { CHANNEL_FILTERS, CHANNEL_FILTER_KEY, CHANNEL_TONES, channelChoices, groupedReleases, noteView, pairView, recommendationNote, releaseList, releasesInChannel, shownChannel } from "../lib/updates.js";
  import { readStored, writeStored } from "../state/prefs.svelte.js";
  import { STAGE_TONES, activeUpgrade, lastRun } from "../lib/upgrade.js";
  import { activeWorkflow } from "../lib/workflow.js";
  import { emsFact } from "../lib/health.js";
  import { errorView } from "../lib/errors.js";
  import StaleNote from "../widgets/StaleNote.svelte";
  import UpgradePanel from "../widgets/UpgradePanel.svelte";
  import AlignmentCard from "../widgets/AlignmentCard.svelte";
  import Spinner from "../widgets/Spinner.svelte";
  import InfluxVersion from "../widgets/InfluxVersion.svelte";
  import { askInflux, influxJob } from "../state/influxUpgrade.svelte.js";
  import { influxNeedsAttention, influxSettled, influxView, stateWords } from "../lib/influxdb.js";

  ensure("overview", "alignment", "releases", "lifecycle", "influxdb");
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
  const note = $derived(recommendationNote(system.releases.data, releases));
  const running = $derived(activeUpgrade(system.alignment.data?.transition) || activeWorkflow(system.lifecycle.data)?.owner === "guided_upgrade");
  const influx = $derived(influxView(system.influxdb.data));
  const influxStale = $derived(Boolean(system.influxdb.error));
  const influxDone = $derived(influxSettled(influx));
  const influxUnfinished = $derived(influxNeedsAttention(influx));
  const upgradeHeld = $derived(upgrade.busy || upgrade.holding || running);
  /** The Guided Upgrade and an InfluxDB run hold off each other; the server refuses either while the other runs. */
  const held = $derived(upgradeHeld || influxJob.running || influx?.state === "busy");
  /** The release row offers the recommendation, never the version EMS already runs, even from a list read before the update. */
  const offered = $derived(recommended?.selectable && !recommended.running && recommended.tag !== ems.version ? recommended : null);
  const adminTag = $derived(overview?.components?.admin?.tag || pair?.admin?.release || "");

  /** A listing just read is what the menu's hint follows; it is told so here. */
  $effect(() => {
    if (system.releases.loadedAt) untrack(() => reload("updates"));
  });

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

<Card icon="circle-arrow-up" tone="c-out" title={t("updates.versions")} subtitle={t("updates.versionsSub")} span="span-12">
  <ol class="updates" data-testid="update-list">
    <li class="update inner" data-testid="update-release">
      <span class="icon-tile sm {offered ? 'c-out' : 'c-ok'}"><Icon name={offered ? "circle-arrow-up" : "circle-check"} /></span>
      <div class="words">
        <div class="name">
          <strong>{t("updates.release.name")}</strong>{" "}<span class="num">{ems.version || t("version.unknown")}{#if offered}{" "}→ {offered.tag}{/if}</span>
          {#if adminTag && ems.version && adminTag !== ems.version}<span class="muted num">{" "}· {t("version.admin")} {adminTag}</span>{/if}
        </div>
        {#if upgrade.stage !== "idle" || offered}
          <p class="muted small">{t("updates.release.how")}</p>
        {:else if note === "stable"}
          <p class="muted small" data-testid="no-recommendation">{t("updates.noRecommendation")}</p>
        {:else if note === "rolling"}
          <p class="muted small" data-testid="rolling-no-recommendation">{t("updates.rollingNoRecommendation")}</p>
        {:else}
          <p class="muted small">{t("updates.chooseBelow")}</p>
        {/if}
      </div>
      {#if upgrade.stage === "idle" && offered}
        <button class="btn" type="button" onclick={() => choose(offered)} disabled={held} data-testid="start-upgrade"><Icon name="circle-arrow-up" size="sm" />{t("upgrade.start", { tag: offered.tag })}</button>
      {:else if upgrade.stage === "idle" && note === "stable"}
        <Status tone="c-ok" text={t("updates.upToDate")} />
      {/if}
    </li>
    {#if influx?.shown}
      <li class="update inner" data-testid="update-influxdb" data-state={influx.state}>
        <span class="icon-tile sm {influx.canUpgrade ? 'c-out' : influxUnfinished || influx.state === 'low_memory' ? 'c-warn' : influxDone ? 'c-ok' : 'c-idle'}"><Icon name="database" /></span>
        <div class="words">
          <div class="name"><strong>{t("version.analytics")}</strong>{" "}<span class="num">{t("updates.influx.version", { version: influx.current || "—" })}{#if influx.offered}{" "}→ {influx.target}{/if}</span></div>
          <p class="muted small">
            {#if influx.canUpgrade}{t("influx.longRun")}{:else if influxUnfinished}{t("updates.influx.unfinished")}{:else}{t(`influx.state.${influx.state}`, stateWords(influx))}{/if}
          </p>
        </div>
        {#if influx.canUpgrade}
          <button class="btn" type="button" disabled={influxStale || influxJob.asking || influxJob.running || upgradeHeld} onclick={() => askInflux("upgrade")} data-testid="influx-upgrade">
            <Icon name={influxJob.running && influxJob.kind === "upgrade" ? "loader" : "database"} size="sm" />{t("influx.upgrade", { version: influx.target })}
          </button>
        {:else if influxUnfinished}
          <Status tone="c-warn" text={t("updates.attention")} />
        {:else if influxDone}
          <Status tone="c-ok" text={t("updates.upToDate")} />
        {/if}
      </li>
    {/if}
  </ol>
  <details class="more">
    <summary><Icon name="chevron-right" />{t("updates.howTitle")}</summary>
    <ol class="how">
      {#each STEPS as step (step)}<li><span class="icon-tile sm {STEP_LOOKS[step].tone}"><Icon name={STEP_LOOKS[step].icon} /></span><span>{t(`updates.step.${step}`)}</span></li>{/each}
    </ol>
  </details>
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
<InfluxVersion />
<AlignmentCard />

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
  .updates { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--s2); }
  .update { display: flex; align-items: center; gap: var(--s3); flex-wrap: wrap; }
  .update .words { flex: 1 1 260px; min-width: 0; }
  .update .name { font-size: 13.5px; }
  .update p { margin: 2px 0 0; }
  .how { list-style: none; margin: var(--s3) 0 0; padding: 0; display: flex; flex-direction: column; gap: 8px; font-size: 13.5px; }
  .how li { display: flex; gap: 10px; align-items: center; }
  @container content (min-width: 820px) {
    .how { flex-direction: row; gap: var(--s4); }
    .how li { flex: 1 1 0; min-width: 0; flex-direction: column; align-items: flex-start; }
  }
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
